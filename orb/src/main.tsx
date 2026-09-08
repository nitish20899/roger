/**
 * The bot's face in the meeting: the ElevenLabs UI Orb (MIT) driven by the Roger server.
 *  - plays the bot's voice (this page's audio is the bot's microphone in Attendee voice-agent mode)
 *  - maps server states to the orb's agentState and feeds it real output/input volume
 *  - forwards the meeting audio it receives as its microphone to the server (transcription fallback)
 *  - reloads itself when the server reports a new build (no rejoin needed for visual changes)
 */
import { useEffect, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import { Orb, type AgentState } from "./orb"

declare global {
  interface Window { ORB_VERSION?: string; ORB_COLORS?: string; ORB_BG?: string; ORB_SIZE?: string }
}

const SR = 16000
const params = new URLSearchParams(location.search)
const MUTE = params.get("mute") === "1"
const COLORS = ((window.ORB_COLORS || "#FFC7A1,#DF5B37").split(",").map((s) => s.trim()) as [string, string])
const BG = window.ORB_BG || "#0a0d10"
const SIZE = parseInt(window.ORB_SIZE || "520", 10)

function rmsOf(analyser: AnalyserNode, buf: Uint8Array): number {
  analyser.getByteTimeDomainData(buf)
  let s = 0
  for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v }
  return Math.sqrt(s / buf.length)
}

function App() {
  const [agentState, setAgentState] = useState<AgentState>(null)
  const outLevel = useRef(0)
  const inLevel = useRef(0)

  useEffect(() => {
    // ---- audio out
    const ctx = new AudioContext()
    const gain = ctx.createGain(); gain.gain.value = MUTE ? 0 : 1
    const analyser = ctx.createAnalyser(); analyser.fftSize = 1024; analyser.smoothingTimeConstant = 0.5
    gain.connect(analyser); analyser.connect(ctx.destination)
    const kick = () => { if (ctx.state !== "running") ctx.resume().catch(() => {}) }
    const kicker = setInterval(kick, 500); document.addEventListener("click", kick)
    let nextTime = 0; const sources = new Set<AudioBufferSourceNode>(); const JITTER = 0.12
    const playPCM = (b64: string) => {
      const bin = atob(b64); const n = bin.length >> 1; if (!n) return
      const buf = ctx.createBuffer(1, n, SR); const ch = buf.getChannelData(0)
      for (let i = 0; i < n; i++) { let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8); if (v >= 0x8000) v -= 0x10000; ch[i] = v / 32768 }
      const src = ctx.createBufferSource(); src.buffer = buf; src.connect(gain)
      const start = Math.max(ctx.currentTime + JITTER, nextTime); src.start(start); nextTime = start + buf.duration
      sources.add(src); src.onended = () => sources.delete(src)
    }
    const stopAll = () => { sources.forEach((s) => { try { s.stop() } catch {} }); sources.clear(); nextTime = 0 }

    // ---- volume sampling for the orb
    const outBuf = new Uint8Array(analyser.fftSize)
    let inAnalyser: AnalyserNode | null = null; let inBuf: Uint8Array | null = null
    let speaking = false
    const meter = setInterval(() => {
      const o = rmsOf(analyser, outBuf)
      outLevel.current = speaking ? Math.min(1, o * 6) : 0
      if (inAnalyser && inBuf) inLevel.current = Math.min(1, rmsOf(inAnalyser, inBuf) * 5)
    }, 33)

    // ---- bridge connection
    let ws: WebSocket | null = null; let closed = false
    const connect = () => {
      ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/monitor")
      ;(window as any).__ws = ws
      ws.onopen = () => {
        setAgentState("listening")
        ws!.send(JSON.stringify({ type: "hello", version: window.ORB_VERSION, webgl: true, ua: navigator.userAgent, orb: "elevenlabs-ui" }))
      }
      ws.onmessage = (e) => {
        if (e.data === "pong") return
        let m: any; try { m = JSON.parse(e.data) } catch { return }
        if (m.type === "audio") playPCM(m.pcm)
        else if (m.type === "stop") { stopAll(); speaking = false; setAgentState("listening") }
        else if (m.type === "state") {
          speaking = m.state === "speaking"
          setAgentState(m.state === "speaking" ? "talking" : m.state === "thinking" ? "thinking" : m.state === "listening" ? "listening" : null)
        } else if (m.type === "orb_version" && m.v && window.ORB_VERSION && m.v !== window.ORB_VERSION) location.reload()
      }
      ws.onclose = () => { if (!closed) { setAgentState(null); setTimeout(connect, 1500) } }
      ws.onerror = () => ws?.close()
    }
    connect()

    // ---- microphone = the meeting audio (voice-agent mode)
    if (params.get("mic") === "1" && navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }).then((stream) => {
        const cap = new AudioContext({ sampleRate: SR })
        const src = cap.createMediaStreamSource(stream)
        inAnalyser = cap.createAnalyser(); inAnalyser.fftSize = 1024; inBuf = new Uint8Array(inAnalyser.fftSize)
        src.connect(inAnalyser)
        const proc = cap.createScriptProcessor(2048, 1, 1)
        src.connect(proc); proc.connect(cap.destination)
        proc.onaudioprocess = (e) => {
          const f = e.inputBuffer.getChannelData(0); const step = cap.sampleRate / SR; const n = Math.floor(f.length / step)
          const out = new Int16Array(n)
          for (let i = 0; i < n; i++) { const v = Math.max(-1, Math.min(1, f[Math.floor(i * step)])); out[i] = v < 0 ? v * 0x8000 : v * 0x7fff }
          let bin = ""; const bytes = new Uint8Array(out.buffer); for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
          if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "mic", pcm: btoa(bin) }))
        }
      }).catch((err) => console.log("mic unavailable", err))
    }

    return () => { closed = true; clearInterval(kicker); clearInterval(meter); ws?.close(); ctx.close() }
  }, [])

  return (
    <div style={{ width: 1280, height: 720, background: BG, display: "grid", placeItems: "center" }}>
      <div style={{ width: SIZE, height: SIZE }}>
        <Orb colors={COLORS} agentState={agentState} getInputVolume={() => inLevel.current} getOutputVolume={() => outLevel.current} />
      </div>
    </div>
  )
}

createRoot(document.getElementById("root")!).render(<App />)
