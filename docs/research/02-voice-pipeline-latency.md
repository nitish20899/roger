# Research: real-time voice pipeline components and latency (Sept 2026)

Legend: **[V]** verified against vendor docs · **[T]** third-party benchmark · **[U]** uncertain.

## 1. ElevenLabs

### TTS models [V]
| Model | Advertised latency | Languages | Real-time? |
|---|---|---|---|
| `eleven_flash_v2_5` | ~75 ms model inference | 32 | Yes, lowest latency |
| `eleven_flash_v2` | ~75 ms | EN | legacy |
| `eleven_turbo_v2_5` | deprecated (replaced by Flash) | | |
| `eleven_v3_conversational` | ~280 ms | 70+ | Yes, most expressive real-time model, audio tags |
| `eleven_v3` | slower | 70+ | No |
| `eleven_multilingual_v2` | slower | 29 | works, slower |

- 75 ms is model inference only; add network RTT 20–200 ms; Professional Voice Clones add per-generation overhead; a 500 ms client player buffer is "common" and often the biggest hidden latency. https://elevenlabs.io/docs/eleven-api/concepts/latency
- Measured [T]: Flash v2.5 median TTFB ~135 ms over WS (us-east); real-world 130–200 ms. https://dev.to/mrzitoun/benchmarking-real-time-voice-ai-apis-cartesia-vs-deepgram-vs-elevenlabs-2026-2n8c

### WebSocket streaming API [V]
- https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input · https://elevenlabs.io/docs/developers/websockets
- Params: `model_id`, `language_code`, `output_format`, `inactivity_timeout` (20 s default; keep alive with `" "`), `sync_alignment`, `auto_mode`, `apply_text_normalization`.
- `generation_config.chunk_length_schedule` default [120,160,250,290]; lower = less latency, worse prosody.
- `auto_mode`: disables buffering; **send full sentences only**. Right mode when the orchestrator already chunks sentences.
- `flush: true` at turn end. Response: `audio` (base64), `alignment` (char timings) → know exactly what was spoken before a barge-in.
- Multi-context WS: several generation contexts on one connection; close a context on interruption. https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input
- Output formats: `pcm_16000/22050/24000/…`, `mp3_*`, `opus_48000_*`, `ulaw_8000`.

### Pricing & concurrency [V]
- API: Flash $0.05/1k chars; v3/Multilingual $0.10/1k chars. Creator $22 / Pro $99 / Scale $299 / Business $990 per month with included characters. https://elevenlabs.io/pricing/api
- Flash concurrency: Creator 10, Pro 20, Scale/Business 30. https://elevenlabs.io/docs/overview/models
- Rough TTS cost at ~800 chars/min of speech: Flash ≈ $0.04/min.

### Agents Platform [V]
- Hosted STT + turn-taking + LLM + TTS. Claude models natively supported; bring-your-own LLM via an OpenAI-compatible SSE endpoint. No official end-to-end latency figure; blog targets <700 ms. $0.08/min + LLM passthrough. https://elevenlabs.io/docs/agents-platform/overview · https://elevenlabs.io/docs/agents-platform/customization/llm/custom-llm

### Voice cloning [V]
| | Instant (IVC) | Professional (PVC) |
|---|---|---|
| Audio | 1–2 min | 30 min minimum, 2–3 h optimal |
| Training | immediate | 3–6 h |
| Fidelity | good | highest; adds per-generation latency |
| Plans | most | Creator+ (1 slot), Scale/Business more |
https://elevenlabs.io/docs/product-guides/voices/voice-cloning/professional-voice-cloning

## 2. Streaming STT

| Provider | Latency | Endpointing / turn detection | Price |
|---|---|---|---|
| **Deepgram Nova-3** [V] | 200–500 ms total; [T] median TTFS 247 ms | silence-based `endpointing` + `utterance_end_ms`; weakest for turn-taking | $0.0048–0.0058/min |
| **Deepgram Flux** [V] | EOT 100–500 ms; [T] ~260 ms p50 | model-based end-of-turn: `eot_threshold` (0.7 default), `eager_eot_threshold` (fires 150–250 ms earlier at +50–70% LLM calls), events StartOfTurn / EagerEndOfTurn / TurnResumed / EndOfTurn | $0.0065/min EN, $0.0078 multi (`flux-general-multi`) |
| **AssemblyAI Universal-Streaming** [V] | ~300 ms; [T] 256 ms | semantic + acoustic EOT built in | $0.0025/min (billed session open-to-close) |
| **Speechmatics** [V] | slower ([T] 495 ms) | Voice SDK turn detection | ~$0.0067/min [U] |
| **OpenAI realtime transcription** [V] | unpublished | `server_vad` / `semantic_vad` | $0.003–0.017/min |
| **ElevenLabs Scribe v2 Realtime** [V] | ~150 ms; [T] p95 250 | VAD built in | $0.39/hr |

Docs: https://developers.deepgram.com/docs/flux/configuration · https://developers.deepgram.com/docs/understanding-end-of-speech-detection · https://www.assemblyai.com/pricing

## 3. Turn-taking / VAD / barge-in
- **Silero VAD** [V]: MIT, ~2 MB, <1 ms per 32 ms chunk; speech/no-speech only. https://github.com/snakers4/silero-vad
- **LiveKit turn detector** [V]: audio model v1 / v1-mini (open weights); benchmark 9.9% false cut-offs at 300 ms budget vs Flux 12.9%. https://docs.livekit.io/agents/logic/turns/turn-detector/
- **Pipecat Smart Turn v3** [V]: 8 MB ONNX, 12–95 ms CPU, 23 languages, open weights. https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/
- Barge-in practice [V/T]: ignore backchannels (`min_interruption_duration` ~0.5 s or min words); on interrupt cancel LLM stream, close TTS context, **flush playout buffer**, truncate the assistant turn in context to what was actually heard (TTS alignment timestamps); 100–300 ms pre-roll; 2026 bar for barge-in reaction <150 ms. https://docs.livekit.io/agents/logic/turns/tuning/

## 4. Orchestration frameworks
| | Pipecat | LiveKit Agents | ElevenLabs Agents |
|---|---|---|---|
| Turn detection | Smart Turn v3, Flux integration | audio turn detector | proprietary |
| LLM→TTS | sentence aggregation → TTS per sentence | streams tokens; `preemptive_generation` | server-side |
| Latency claims | target 800 ms median (4×~200 ms) | [T] 500–650 ms p95 tuned | <700 ms target |
Fast-ack pattern: small model emits a 2–4-word acknowledgement in 200–500 ms while the big model answers; pre-rendered filler clips are the zero-latency alternative. https://webrtc.ventures/2025/06/reducing-voice-agent-latency-with-parallel-slms-and-llms/

## 5. Claude LLM latency [T]
Artificial Analysis (Sept 2026), non-reasoning TTFT on ~1k-token prompts: **Haiku 4.5 0.74–0.78 s**, **Sonnet 5 0.81 s**, Opus 4.7 1.07 s. Output ~80 tok/s. With thinking on, TTFT is seconds to minutes: **never enable thinking in the voice path**. Short cached prompts typically land lower (~400–600 ms) [U]. https://artificialanalysis.ai/providers/anthropic

## 6. Latency budget (end of speech → first audio)
| Stage | Pipecat primer | Achievable target |
|---|---|---|
| Endpointing + final STT | 300 | 200–300 (Flux / Smart Turn) |
| LLM TTFT | 650 | 350–500 (Haiku 4.5, cached, streaming) |
| Sentence aggregation | 20 | 20 |
| TTS first audio | 120 | 130–200 (Flash WS) |
| Network / codec / jitter | ~130 | 80–150 |
| **Total** | ~1,293 | **~800–1,150 sequential; <800 p50 needs overlap** |

Overlap techniques: start LLM on EagerEndOfTurn / preemptive generation; start TTS on the first sentence; keep the playout jitter buffer at 100–200 ms.

## Recommended stack
Deepgram Flux (EN) or `flux-general-multi` + Smart Turn v3 → Claude Haiku 4.5 (streaming, no thinking, cached system prompt, 1–3 sentence cap) → ElevenLabs Flash v2.5 over WS (`auto_mode`, `pcm_16000`, multi-context for barge-in). Pipecat for control, LiveKit Agents for transport + hosted turn detector, ElevenLabs Agents for fastest ship with least control. Pure-API cost ≈ $0.05–0.06/min of conversation.
