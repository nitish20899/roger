/*
 * Roger's ears and voice inside the meeting page.
 *
 * This runs before the meeting app's own scripts, in every frame. It does three things:
 *
 *   1. Puts Roger's voice on the call. Every audio track the page installs on an RTCRtpSender is
 *      swapped for one we write into, so the meeting hears an ordinary participant.
 *   2. Taps every inbound audio track by wrapping `RTCPeerConnection`, mixes them for transcription and
 *      keeps each one separately so we can tell who is talking.
 *   3. Carries that audio to and from Python.
 *
 * The transport is a Playwright binding, not a WebSocket, and that is not a detail. A page script cannot
 * open a socket back to `127.0.0.1` from inside Google Meet: its Content-Security-Policy forbids the
 * connection, and the attempt takes the renderer down with it. A binding is installed by the driver
 * itself, over the DevTools protocol, so no page policy applies to it.
 *
 *   page -> python   window.__rogerSend(json)     audio frames carry base64 PCM
 *   python -> page   window.__rogerBridge.play(b64) / .command(json)
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
    outCtx: null, inCtx: null, mic: null, micNode: null, mixer: null, video: null,
    tracks: new Map(), nextStreamId: 1, ready: false, sent: 0, recv: 0,
  };
  window.__rogerBridge = bridge;

  // ------------------------------------------------------------------ the audio graph
  //
  // ScriptProcessorNode, not AudioWorklet, and deliberately. An AudioWorklet's code has to be fetched as
  // a module, and Microsoft Teams' Content-Security-Policy refuses both `blob:` and `data:` module URLs,
  // so a worklet cannot be installed there at all. ScriptProcessorNode needs no module, is supported
  // everywhere Roger runs, and at one 4096-sample callback per ~85 ms costs nothing worth measuring.
  // It is deprecated; it is also the only thing that works in both products today.
  const OUT_BUFFER = 4096;  // at the browser's rate; underruns here are audible, so this is the safe one
  const IN_BUFFER = 2048;   // at RATE; smaller, because this is latency on Roger's ears

  // ------------------------------------------------------------------ conversion
  const toPcm16 = (f32) => {
    const b = new ArrayBuffer(f32.length * 2), v = new DataView(b);
    for (let i = 0; i < f32.length; i++) {
      let s = f32[i]; s = s < -1 ? -1 : s > 1 ? 1 : s;
      v.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Uint8Array(b);
  };
  const fromPcm16 = (bytes) => {
    const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const n = bytes.byteLength >> 1, f = new Float32Array(n);
    for (let i = 0; i < n; i++) f[i] = v.getInt16(i * 2, true) / 0x8000;
    return f;
  };
  const b64 = (bytes) => {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const unb64 = (str) => {
    const bin = atob(str), n = bin.length, out = new Uint8Array(n);
    for (let i = 0; i < n; i++) out[i] = bin.charCodeAt(i);
    return out;
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

  // A tap: hand every FRAME samples that pass through to Python.
  //
  // A ScriptProcessorNode is only pulled if its output leads somewhere, and the obvious somewhere --
  // `ctx.destination` -- is the machine's speakers. Roger has no speakers to play to, and on a headless
  // browser there is no output device at all. Each tap therefore ends in a MediaStreamAudioDestinationNode,
  // which pulls the graph exactly the same way and touches no hardware. Nothing is ever played out loud.
  function tap(ctx, onFrame) {
    const node = ctx.createScriptProcessor(IN_BUFFER, 1, 1);
    let buf = new Float32Array(FRAME), n = 0;
    node.onaudioprocess = (e) => {
      const ch = e.inputBuffer.getChannelData(0);
      for (let i = 0; i < ch.length; i++) {
        buf[n++] = ch[i];
        if (n === FRAME) { onFrame(buf); buf = new Float32Array(FRAME); n = 0; }
      }
    };
    node.connect(ctx.createMediaStreamDestination());
    return node;
  }

  async function audio() {
    if (bridge.inCtx) return bridge;

    // Outbound, at the browser's own rate (no sampleRate option: whatever it picks is the native one).
    const outCtx = new AudioContext({ latencyHint: "interactive" });
    const queue = [];
    let cur = null, pos = 0;
    const micNode = outCtx.createScriptProcessor(OUT_BUFFER, 1, 1);
    micNode.onaudioprocess = (e) => {
      const out = e.outputBuffer.getChannelData(0);
      let i = 0;
      while (i < out.length) {
        if (!cur) { if (!queue.length) break; cur = queue.shift(); pos = 0; }
        const n = Math.min(out.length - i, cur.length - pos);
        out.set(cur.subarray(pos, pos + n), i);
        i += n; pos += n;
        if (pos >= cur.length) cur = null;
      }
      while (i < out.length) out[i++] = 0;   // silence when Roger is not talking
    };
    const dest = outCtx.createMediaStreamDestination();
    micNode.connect(dest);
    bridge.micNode = micNode;
    bridge.mic = dest.stream;
    bridge.enqueue = (f32) => {
      queue.push(f32);
      while (queue.length > 200) queue.shift();   // a stall must not become unbounded latency
    };
    bridge.flushOut = () => { queue.length = 0; cur = null; };

    // Inbound, at Roger's wire rate, so captured frames need no conversion before they are sent.
    const inCtx = new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
    bridge.mixer = inCtx.createGain();
    bridge.mixer.connect(tap(inCtx, (f32) => sendAudio(0, f32)));

    bridge.outCtx = outCtx;
    bridge.inCtx = inCtx;

    // Keep both contexts running, and keep checking.
    //
    // A suspended AudioContext renders nothing, so its taps never fire and Roger is simply deaf -- with
    // no error, in a page that otherwise looks joined. It has been seen suspended on arrival even with
    // the autoplay policy relaxed, and the browser may suspend it again later. Resuming once at startup
    // is therefore not enough: this keeps at it, and tells Python what the state is so `roger status`
    // can show it rather than leaving a silent bot to be discovered in the meeting.
    let reported = "";
    bridge.wake = () => {
        for (const c of [outCtx, inCtx]) {
            if (c.state === "suspended") c.resume().catch(() => {});
        }
        const now = outCtx.state + "/" + inCtx.state;
        if (now !== reported) {
            reported = now;
            log("audio contexts:", now);
            send({ type: "audio_state", out: outCtx.state, in: inCtx.state });
        }
    };
    bridge.wake();
    setInterval(bridge.wake, 2000);
    // A real gesture lifts the autoplay restriction when nothing else will.
    for (const ev of ["click", "keydown", "pointerdown"]) {
        window.addEventListener(ev, bridge.wake, { capture: true, passive: true });
    }

    log("audio up: out", outCtx.sampleRate, "Hz, in", inCtx.sampleRate, "Hz, frame", FRAME);
    send({ type: "audio_format", out_rate: outCtx.sampleRate, in_rate: inCtx.sampleRate, frame: FRAME });
    return bridge;
  }

  function sendAudio(streamId, f32) {
    if (!send({ type: "audio", stream: streamId, pcm: b64(toPcm16(f32)) })) return;
    bridge.sent++;
  }

  // A remote audio track: tap it for the mix, and keep it on its own stream id so Python can tell voices
  // apart. Chrome will not run a remote track through WebAudio unless it is also attached to an element,
  // so there is a muted sink element per track -- it produces no sound, it just keeps the track flowing.
  async function attachRemote(track, label) {
    if (bridge.tracks.has(track.id)) return;
    await audio();
    if (bridge.wake) bridge.wake();   // someone is talking now; the context must not be asleep
    const ctx = bridge.inCtx;
    const stream = new MediaStream([track]);
    const sink = new Audio();
    sink.srcObject = stream; sink.muted = true; sink.autoplay = true;
    sink.play().catch(() => {});

    const src = ctx.createMediaStreamSource(stream);
    src.connect(bridge.mixer);

    const streamId = bridge.nextStreamId++;
    let own = null;
    if (CFG.per_participant) {
      own = tap(ctx, (f32) => sendAudio(streamId, f32));
      src.connect(own);
    }
    bridge.tracks.set(track.id, { streamId, src, tap: own, sink, track });
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
  // A still frame with the bot's name, so the tile in the call is not a void. Built once and reused:
  // a meeting app asks for the camera repeatedly, and a fresh canvas with its own repaint timer and
  // capture stream each time leaks all three.
  function fakeVideo() {
    if (bridge.video) return bridge.video;
    const c = document.createElement("canvas");
    c.width = 640;
    c.height = 360;
    const g = c.getContext("2d");
    const paint = () => {
      g.fillStyle = "#16181d"; g.fillRect(0, 0, c.width, c.height);
      g.fillStyle = "#8ab4f8"; g.font = "600 44px system-ui, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(CFG.bot_name, c.width / 2, c.height / 2);
    };
    paint();
    setInterval(paint, 1000);  // one timer for the life of the page: captureStream needs new frames
    bridge.video = c.captureStream(2);
    return bridge.video;
  }

  // ------------------------------------------------------------------ tapping the call
  //
  // Roger's voice is substituted at the *sender*, not at `getUserMedia`.
  //
  // Patching getUserMedia is the obvious way and it does not survive contact with Google Meet. Meet
  // manages its microphone properly: it enumerates devices, reads getSettings(), and calls
  // applyConstraints({deviceId: {exact: ...}}) to pin the one it wants. A MediaStreamAudioDestinationNode
  // track is not a device and cannot answer any of that, and a few seconds later the renderer dies --
  // no exception, no console message, just "Page crashed". Bisected: with the getUserMedia patch removed
  // and everything else kept, the page is stable indefinitely.
  //
  // So Chrome's own fake device answers Meet's device questions, and the only thing we change is what
  // actually leaves on the wire. Every audio track a page installs on a sender is swapped for ours, which
  // also survives Meet replacing the track itself on mute and unmute.
  async function swapSender(sender) {
    if (!sender || sender.__roger) return;
    await audio();
    const mine = bridge.mic && bridge.mic.getAudioTracks()[0];
    if (!mine) return;
    sender.__roger = true;
    try {
      await ORIG_REPLACE.call(sender, mine);
      log("sender now carries Roger's voice");
    } catch (e) {
      sender.__roger = false;
      log("replaceTrack failed", e);
    }
  }

  const ORIG_REPLACE = window.RTCRtpSender && RTCRtpSender.prototype.replaceTrack;
  if (ORIG_REPLACE) {
    RTCRtpSender.prototype.replaceTrack = function (track) {
      if (track && track.kind === "audio") {
        const mine = bridge.mic && bridge.mic.getAudioTracks()[0];
        if (mine) { this.__roger = true; return ORIG_REPLACE.call(this, mine); }
        swapSender(this);
        return ORIG_REPLACE.call(this, track);
      }
      return ORIG_REPLACE.call(this, track);
    };
  }

  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    const OrigAddTrack = OrigPC.prototype.addTrack;
    const OrigAddTransceiver = OrigPC.prototype.addTransceiver;
    OrigPC.prototype.addTrack = function (track, ...rest) {
      const sender = OrigAddTrack.call(this, track, ...rest);
      if (track && track.kind === "audio") swapSender(sender).catch(() => {});
      return sender;
    };
    OrigPC.prototype.addTransceiver = function (kind, ...rest) {
      const tr = OrigAddTransceiver.call(this, kind, ...rest);
      const isAudio = kind === "audio" || (kind && kind.kind === "audio");
      if (isAudio && tr && tr.sender) swapSender(tr.sender).catch(() => {});
      return tr;
    };

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

  // ------------------------------------------------------------------ the link to Python
  //
  // `window.__rogerSend` is installed by the driver before any page script runs. It is a plain function,
  // so Content-Security-Policy has nothing to say about it -- which is the whole reason it is not a socket.
  function send(obj) {
    const fn = window.__rogerSend;
    if (typeof fn !== "function") return false;
    try {
      fn(JSON.stringify(obj));
      return true;
    } catch (e) {
      return false;   // the page is going away mid-call; the next frame will find out
    }
  }
  bridge.send = send;

  // Called from Python. Roger speaking: up to the browser's rate, then into the microphone worklet.
  bridge.play = (payload) => {
    if (!bridge.enqueue) return false;
    bridge.recv++;
    bridge.enqueue(upsample(fromPcm16(unb64(payload)), RATE, bridge.outCtx.sampleRate));
    return true;
  };

  // Commands from Python. Platform code adds its own via registerCommand.
  bridge.commands = {
    flush: () => bridge.flushOut && bridge.flushOut(),
    stats: () => send({ type: "stats", sent: bridge.sent, recv: bridge.recv, tracks: bridge.tracks.size }),
  };
  bridge.registerCommand = (name, fn) => { bridge.commands[name] = fn; };
  bridge.command = (raw) => {
    let m;
    try { m = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (e) { return false; }
    const fn = bridge.commands[m && m.type];
    if (!fn) return false;
    try { fn(m); return true; } catch (e) { log("command " + m.type + " failed", e); return false; }
  };

  // Only the top frame talks to Python; sub-frames still get the patched RTCPeerConnection above, which
  // is all they are here for. The binding may land a tick after this script, so wait for it rather than
  // assuming -- a missed hello would mean a bot that hears nothing and never says why.
  if (window.top === window) {
    let tries = 0;
    const begin = () => {
      if (typeof window.__rogerSend !== "function") {
        if (++tries > 600) return log("no link to Python after 60s; giving up");
        return setTimeout(begin, 100);
      }
      bridge.ready = true;
      send({ type: "hello", rate: RATE, frame: FRAME, url: location.href });
      audio().catch((e) => log("audio init failed", e));
    };
    begin();
  }
  log("installed in", location.href);
})();
