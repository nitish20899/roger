/*
 * Roger's ears and voice inside the meeting page.
 *
 * This runs before the meeting app's own scripts, in every frame. It does three things:
 *
 *   1. Hands the meeting app a microphone that is really us. `getUserMedia` is patched to return a
 *      MediaStream fed by an AudioWorklet, so when Roger speaks, the call hears a normal participant.
 *   2. Taps every inbound audio track by wrapping `RTCPeerConnection`, mixes them for transcription and
 *      keeps each one separately so we can tell who is talking.
 *   3. Carries that audio to and from Python over one WebSocket to Roger's own local server.
 *
 * Wire format. Binary both ways, because this is the hot path:
 *   page -> python   <uint32 LE stream id><pcm16le frame>      stream 0 is the mix
 *   python -> page   <pcm16le frame>                            what Roger says
 * Everything else -- chat, participants, state -- is JSON text on the same socket.
 *
 * Configuration is substituted in by browser.py before injection: __ROGER_CFG__.
 */
(() => {
  "use strict";
  if (window.__rogerBridge) return;

  const CFG = __ROGER_CFG__;
  const RATE = CFG.rate;
  const FRAME = Math.round(RATE * CFG.frame_ms / 1000);
  const log = (...a) => { if (CFG.debug) console.log("[roger]", ...a); };

  // Two audio contexts, and the reason matters.
  //
  // Outbound runs at the browser's own hardware rate. Chrome hands WebRTC a SILENT track from a
  // MediaStreamAudioDestinationNode whose context is not at the native rate -- measured: at 24 kHz the far
  // end receives perfectly formed, perfectly empty audio, with no error anywhere. So the microphone side
  // takes whatever rate the browser wants and we resample into it.
  //
  // Inbound runs at Roger's wire rate, because `createMediaStreamSource` resamples into its context for
  // free and in better quality than we would manage by hand. Capture therefore arrives ready to send.
  const bridge = {
    ws: null, outCtx: null, inCtx: null, mic: null, micNode: null, mixer: null,
    tracks: new Map(), nextStreamId: 1, ready: false, sent: 0, recv: 0,
  };
  window.__rogerBridge = bridge;

  // ------------------------------------------------------------------ worklets
  // Out: drains a queue of Float32 frames into the fake microphone, silence when empty.
  // In:  accumulates FRAME samples and posts them up. Chrome calls process() with 128 samples.
  const WORKLET = `
    class RogerOut extends AudioWorkletProcessor {
      constructor() { super(); this.q = []; this.cur = null; this.pos = 0;
        this.port.onmessage = (e) => {
          if (e.data === "flush") { this.q.length = 0; this.cur = null; return; }
          this.q.push(e.data);
          while (this.q.length > 200) this.q.shift();   // a stall must not become unbounded latency
        };
      }
      process(_i, outputs) {
        const out = outputs[0][0]; if (!out) return true;
        let i = 0;
        while (i < out.length) {
          if (!this.cur) { if (!this.q.length) break; this.cur = this.q.shift(); this.pos = 0; }
          const n = Math.min(out.length - i, this.cur.length - this.pos);
          out.set(this.cur.subarray(this.pos, this.pos + n), i);
          i += n; this.pos += n;
          if (this.pos >= this.cur.length) this.cur = null;
        }
        while (i < out.length) out[i++] = 0;
        return true;
      }
    }
    class RogerIn extends AudioWorkletProcessor {
      constructor(opts) { super(); this.frame = opts.processorOptions.frame;
        this.buf = new Float32Array(this.frame); this.n = 0; }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0]; if (!ch) return true;
        for (let i = 0; i < ch.length; i++) {
          this.buf[this.n++] = ch[i];
          if (this.n === this.frame) { this.port.postMessage(this.buf.slice()); this.n = 0; }
        }
        return true;
      }
    }
    registerProcessor("roger-out", RogerOut);
    registerProcessor("roger-in", RogerIn);
  `;

  // ------------------------------------------------------------------ conversion
  const toPcm16 = (f32) => {
    const b = new ArrayBuffer(4 + f32.length * 2), v = new DataView(b);
    for (let i = 0; i < f32.length; i++) {
      let s = f32[i]; s = s < -1 ? -1 : s > 1 ? 1 : s;
      v.setInt16(4 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return { buf: b, view: v };
  };
  const fromPcm16 = (ab) => {
    const v = new DataView(ab), n = ab.byteLength >> 1, f = new Float32Array(n);
    for (let i = 0; i < n; i++) f[i] = v.getInt16(i * 2, true) / 0x8000;
    return f;
  };
  // Only ever used to go *up* from the wire rate to the browser's, which needs no anti-alias filter.
  const upsample = (f32, from, to) => {
    if (from === to || f32.length < 2) return f32;
    const ratio = from / to, n = Math.max(1, Math.round(f32.length / ratio)), out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const pos = i * ratio, j = pos | 0, frac = pos - j;
      const a = f32[j], b = j + 1 < f32.length ? f32[j + 1] : a;
      out[i] = a + (b - a) * frac;
    }
    return out;
  };

  // ------------------------------------------------------------------ audio graph
  const WORKLET_URL = () => URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));

  async function audio() {
    if (bridge.inCtx) return bridge;

    // Outbound, at the browser's own rate (no sampleRate option: whatever it picks is the native one).
    const outCtx = new AudioContext({ latencyHint: "interactive" });
    let url = WORKLET_URL();
    await outCtx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    bridge.micNode = new AudioWorkletNode(outCtx, "roger-out", { numberOfInputs: 0, outputChannelCount: [1] });
    const dest = outCtx.createMediaStreamDestination();
    bridge.micNode.connect(dest);
    bridge.mic = dest.stream;

    // Inbound, at Roger's wire rate, so captured frames need no conversion before they are sent.
    const inCtx = new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
    url = WORKLET_URL();
    await inCtx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    bridge.mixer = inCtx.createGain();
    const mixTap = new AudioWorkletNode(inCtx, "roger-in", { processorOptions: { frame: FRAME }, numberOfOutputs: 0 });
    mixTap.port.onmessage = (e) => sendAudio(0, e.data);
    bridge.mixer.connect(mixTap);

    for (const c of [outCtx, inCtx]) if (c.state === "suspended") c.resume().catch(() => {});
    bridge.outCtx = outCtx;
    bridge.inCtx = inCtx;
    log("audio up: out", outCtx.sampleRate, "Hz, in", inCtx.sampleRate, "Hz, frame", FRAME);
    send({ type: "audio_format", out_rate: outCtx.sampleRate, in_rate: inCtx.sampleRate, frame: FRAME });
    return bridge;
  }

  function sendAudio(streamId, f32) {
    const ws = bridge.ws;
    if (!ws || ws.readyState !== 1) return;
    const { buf, view } = toPcm16(f32);
    view.setUint32(0, streamId, true);
    ws.send(buf);
    bridge.sent++;
  }

  // A remote audio track: tap it for the mix, and keep it on its own stream id so Python can tell voices
  // apart. Chrome will not run a remote track through WebAudio unless it is also attached to an element,
  // so there is a muted sink element per track -- it produces no sound, it just keeps the track flowing.
  async function attachRemote(track, label) {
    if (bridge.tracks.has(track.id)) return;
    await audio();
    const ctx = bridge.inCtx;
    const stream = new MediaStream([track]);
    const sink = new Audio();
    sink.srcObject = stream; sink.muted = true; sink.autoplay = true;
    sink.play().catch(() => {});

    const src = ctx.createMediaStreamSource(stream);
    src.connect(bridge.mixer);

    const streamId = bridge.nextStreamId++;
    let tap = null;
    if (CFG.per_participant) {
      tap = new AudioWorkletNode(ctx, "roger-in", { processorOptions: { frame: FRAME }, numberOfOutputs: 0 });
      tap.port.onmessage = (e) => sendAudio(streamId, e.data);
      src.connect(tap);
    }
    bridge.tracks.set(track.id, { streamId, src, tap, sink, track });
    send({ type: "track", stream_id: streamId, track_id: track.id, label: label || "" });
    log("remote track", track.id, "-> stream", streamId);

    const drop = () => {
      const t = bridge.tracks.get(track.id); if (!t) return;
      try { t.src.disconnect(); t.tap && t.tap.disconnect(); t.sink.srcObject = null; } catch (_) {}
      bridge.tracks.delete(track.id);
      send({ type: "track_ended", stream_id: streamId, track_id: track.id });
    };
    track.addEventListener("ended", drop);
    track.addEventListener("mute", () => send({ type: "track_mute", stream_id: streamId, muted: true }));
    track.addEventListener("unmute", () => send({ type: "track_mute", stream_id: streamId, muted: false }));
  }

  // ------------------------------------------------------------------ the fake devices
  const FAKE_MIC = { deviceId: "roger-mic", groupId: "roger", kind: "audioinput", label: CFG.bot_name + " (virtual microphone)" };

  // A still frame with the bot's name, so the tile in the call is not a void.
  function fakeVideo() {
    const c = document.createElement("canvas"); c.width = 640; c.height = 360;
    const g = c.getContext("2d");
    const paint = () => {
      g.fillStyle = "#16181d"; g.fillRect(0, 0, c.width, c.height);
      g.fillStyle = "#8ab4f8"; g.font = "600 44px system-ui, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(CFG.bot_name, c.width / 2, c.height / 2);
    };
    paint(); setInterval(paint, 1000);  // keep the capture stream alive
    return c.captureStream(2);
  }

  async function fakeStream(constraints) {
    await audio();
    const out = new MediaStream();
    if (constraints && constraints.audio) bridge.mic.getAudioTracks().forEach((t) => out.addTrack(t));
    if (constraints && constraints.video) fakeVideo().getVideoTracks().forEach((t) => out.addTrack(t));
    return out;
  }

  const md = navigator.mediaDevices;
  if (md) {
    const realEnumerate = md.enumerateDevices && md.enumerateDevices.bind(md);
    md.getUserMedia = async (c) => fakeStream(c || { audio: true });
    md.enumerateDevices = async () => {
      let real = [];
      try { real = realEnumerate ? await realEnumerate() : []; } catch (_) {}
      return [FAKE_MIC, ...real.filter((d) => d.kind !== "audioinput")];
    };
    if (navigator.getUserMedia) navigator.getUserMedia = (c, ok, err) => fakeStream(c).then(ok, err);
  }

  // ------------------------------------------------------------------ tapping the call
  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    window.RTCPeerConnection = new Proxy(OrigPC, {
      construct(target, args) {
        const pc = new target(...args);
        pc.addEventListener("track", (ev) => {
          if (ev.track && ev.track.kind === "audio") {
            attachRemote(ev.track, (ev.streams && ev.streams[0] && ev.streams[0].id) || "").catch((e) => log("attach failed", e));
          }
        });
        return pc;
      },
    });
    window.webkitRTCPeerConnection = window.RTCPeerConnection;
  }

  // ------------------------------------------------------------------ the socket
  function send(obj) {
    const ws = bridge.ws;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }
  bridge.send = send;

  function connect() {
    let ws;
    try { ws = new WebSocket(CFG.ws_url); } catch (e) { return setTimeout(connect, 1000); }
    ws.binaryType = "arraybuffer";
    bridge.ws = ws;

    ws.onopen = () => {
      bridge.ready = true;
      send({ type: "hello", rate: RATE, frame: FRAME, url: location.href, frame_id: CFG.frame_id });
      audio().catch((e) => log("audio init failed", e));
      log("socket open");
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
        const fn = bridge.commands && bridge.commands[m.type];
        if (fn) { try { fn(m); } catch (e) { log("command " + m.type + " failed", e); } }
        return;
      }
      // Roger speaking: up to the browser's rate, then straight into the microphone worklet.
      if (!bridge.micNode) return;
      bridge.recv++;
      bridge.micNode.port.postMessage(upsample(fromPcm16(ev.data), RATE, bridge.outCtx.sampleRate));
    };
    ws.onclose = () => { bridge.ready = false; bridge.ws = null; setTimeout(connect, 800); };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }

  // Commands from Python. Platform code adds its own (chat, leave) via registerCommand.
  bridge.commands = {
    flush: () => bridge.micNode && bridge.micNode.port.postMessage("flush"),
    ping: () => send({ type: "pong", sent: bridge.sent, recv: bridge.recv, tracks: bridge.tracks.size }),
  };
  bridge.registerCommand = (name, fn) => { bridge.commands[name] = fn; };

  // Only the top frame owns the socket; sub-frames still get the patched getUserMedia above.
  if (window.top === window) connect();
  log("installed in", location.href);
})();
