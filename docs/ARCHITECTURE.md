# Roger — Architecture Design

**Goal.** A running Claude Code session joins a Microsoft Teams or Google Meet call as a participant. It hears what people say, answers with the context of that chat session (the project, the code, the decisions), speaks in a natural human voice (ElevenLabs), and drops code snippets, links and notes into the meeting chat when speech is the wrong medium. It has to feel responsive: it may think, but it must not leave silence.

**Status.** Design written 2026-09-07 from vendor documentation and benchmarks (see `research/`), before anything was built. The implementation in this repository followed it, with these differences learned while building: the meeting bot is [Attendee](https://attendee.dev) rather than Recall.ai (Recall requires a work e-mail to sign up); transcription is ElevenLabs Scribe v2 Realtime rather than Deepgram Flux, so one vendor covers hearing and speaking; the default fast responder is OpenAI `gpt-4.1-mini` with Claude Haiku 4.5 as the alternative; the address classifier is a small model on the same provider (`gpt-4.1-mini` / `gpt-5.4-mini` score 23/24 on the bundled evaluation); the bot's tile is the open-source ElevenLabs UI orb rendered through Attendee's voice-agent mode, which also carries the voice so audio and video stay in sync. Measured in live Google Meet calls: about 1.0 to 1.5 s from the end of a question to the first spoken word on the fast path, 10 to 40 s for delegated answers behind a bridge phrase. Phases 0 and 1 below are done; phase 2 is partly done (speculative generation with gated release, attention manager, barge-in, echo filtering); think-ahead, chat mentions and phase 3 remain. Numbers marked *target* in the rest of this document are the original engineering targets. Open questions are numbered at the end; they are placeholders, not assumptions.

---

## 1. Design summary

Three decisions shape everything else.

**1. Get into the meeting through a bot-as-a-service layer, not a native API.** Neither platform offers a native way for a bot to speak and post chat. Google Meet's Media API is receive-only, still developer preview, and requires every participant to be enrolled in the preview program. Microsoft Teams' only native real-time-audio path is a C#/.NET Graph media bot that must run on Windows Server in Azure with a public IP and certificate, plus a separate Bot Framework app for chat. A vendor bot (Recall.ai primary, open-source Attendee as fallback) gives both platforms through one API: per-participant 16 kHz PCM in real time, a way for the bot to speak, a chat-send endpoint, and a video tile.

**2. Two brains, one voice.** The Claude Code session is the brain with the context and the tools, but a turn of that session takes seconds (large context, tool use, thinking). Conversation needs a first word in under a second. So a small fast responder (Claude Haiku 4.5, no thinking, prompt-cached) answers from a briefing the deep session prepared, and hands anything that needs the repo, tools or hard reasoning to the deep session (the Claude Code session resumed through the Agent SDK). The deep session answers by speaking a short summary and posting the details in chat.

**3. A meeting is not a phone call.** Five people are talking, and the bot is one voice among them. The core product logic is the floor policy: when is the bot being addressed, when may it speak, how long, and when should it stay quiet and put its answer in chat instead. Everything about latency exists to serve that policy.

Latency targets (end of a human's question to the first audible word):

| Path | Target P50 | What the listener hears |
|---|---|---|
| Fast answer | ≤ 1.0–1.2 s | The answer itself, 1–3 sentences |
| Delegated question | ≤ 0.5 s | A bridge phrase in the same voice ("Let me pull up the actual query, one moment"), then the answer 5–60 s later |
| Barge-in (human starts talking over the bot) | ≤ 150 ms | The bot stops mid-sentence |

Sub-second on the fast path is possible with overlap tricks (section 6) but is not the first milestone. Multi-party meetings tolerate 1-second gaps better than 1:1 calls because turns are naturally slower, and the bot's video tile shows a visible "about to speak" state that fills the gap.

---

## 2. System overview

```mermaid
flowchart LR
  subgraph Meeting["Teams / Google Meet"]
    P[Participants]
    C[Meeting chat]
  end
  subgraph Bot["Meeting bot (Recall.ai or Attendee)"]
    BA[Per-participant audio → WS]
    OM[Output Media tile<br/>(our avatar web page)]
    CH[Chat send / receive]
  end
  subgraph Bridge["Meeting Bridge (one process per meeting)"]
    IN[Audio ingress<br/>mix + speaker attribution]
    STT[Streaming STT + turn detector<br/>Deepgram Flux / Smart Turn]
    TS[(Transcript store)]
    FM[Floor manager<br/>addressed? may speak? how long?]
    FR[Fast responder<br/>Claude Haiku 4.5]
    SP[Speaker<br/>ElevenLabs Flash v2.5 WS + bridge clips]
    CP[Chat poster<br/>platform formatting, snippet links]
    DA[Deep agent<br/>Claude Agent SDK, resumed session<br/>Opus 5, repo + tools]
    BR[(Briefing)]
  end
  P -- speech --> BA -- PCM 16 kHz per speaker --> IN --> STT --> TS
  STT --> FM --> FR --> SP -- PCM --> OM -- audio --> P
  FR -- delegate(question) --> DA
  DA -- say() --> SP
  DA -- post_chat() --> CP --> CH
  FR -- post_chat() --> CP
  DA -- update_briefing() --> BR --> FR
  TS -- rolling window --> FR
  TS -- digests / on demand --> DA
  CH -- @mentions --> FM
```

The **Meeting Bridge** is one process per meeting. It runs where the Claude Code session and the repository live (initially the developer's Mac; later a cloud host with a session store). The **meeting bot** is the vendor's cloud browser that sits in the call; it streams audio to the bridge over a WebSocket and plays whatever the bridge sends into its camera/audio tile.

### Components

| Component | Responsibility | Technology |
|---|---|---|
| Bot controller | Create the bot for a join URL, configure name, avatar page, real-time audio endpoint; handle lifecycle webhooks (joined, recording, left); leave on command | Recall.ai API (or Attendee) |
| Audio ingress | Receive per-participant PCM frames; mix the human streams into one mono stream for STT; attribute each utterance to the participant whose stream had energy in that window (no diarization model needed) | asyncio WebSocket server, Silero VAD per stream |
| STT + turn detector | Interim transcripts, final transcripts, end-of-turn events, *eager* end-of-turn for speculative starts | Deepgram Flux (`flux-general-en`, or `flux-general-multi` for FR/EN); alternative Nova-3 / Scribe v2 Realtime + Pipecat Smart Turn v3 |
| Transcript store | Speaker-attributed, timestamped utterances, plus chat messages and the bot's own spoken text (truncated to what was actually heard); in memory + JSONL on disk | Python dataclasses; JSONL |
| Floor manager | Decide for each finished utterance: addressed to the bot? Answer now, answer when the floor is free, or stay quiet? Enforce turn rules, speech length, interjection budget, barge-in | Rules + Haiku classifier (section 5) |
| Fast responder | Produce the spoken answer or a delegation decision from persona + briefing + last few minutes of transcript | Claude Haiku 4.5, streaming, thinking omitted, prompt caching, `max_tokens` ≈ 150, tools `delegate`, `post_chat`, `stay_silent` |
| Deep agent | The user's Claude Code session, resumed (or forked) with the Agent SDK; produces the briefing at join, handles delegated questions with repo access and tools, thinks ahead while others talk, writes meeting notes at the end | `ClaudeSDKClient` (Python Agent SDK), `resume=<session_id>`, `include_partial_messages=True`, in-process MCP tools `say`, `post_chat`, `update_briefing`, `read_transcript`, `take_note` |
| Speaker | Sentence-chunk text into TTS, stream PCM to the avatar page, play pre-rendered bridge clips, stop within 150 ms on barge-in, report what was actually spoken | ElevenLabs Flash v2.5 multi-context WebSocket, `auto_mode`, `pcm_16000`, alignment data |
| Avatar page | The bot's camera tile: name, listening / thinking / speaking state, waveform; receives PCM over WebSocket and plays it with a 100–200 ms jitter buffer | Static HTML + WebAudio, rendered by Recall Output Media |
| Chat poster | Post text and code with platform limits (Meet 500 chars plain text; Teams 4096); anything longer becomes a link to a snippet page; read inbound chat for `@Claude` | Recall `send_chat_message`; snippet host (gist or local server behind the tunnel) |
| Observability | Per-turn spans (speech_end → eot → first_token → first_audio → playout), cost meter, decision log, post-meeting summary | OpenTelemetry-style spans to a local file / dashboard |

### Why the Claude Code session, concretely

Claude Code stores every session as `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The Agent SDK can open that exact session with `resume=<session_id>` (optionally `fork_session=True`) and continue it as a long-lived streaming-input session. This is what "the meeting agent has the context of the chat session" means in practice: the deep agent literally *is* that session, with the same working directory, memory files and skills. The bridge chooses the session with a picker (the SDK exposes `list_sessions()`), or the user passes the ID.

Fork or continue is an open question (Q3). Fork protects the working session from meeting noise; continue means the developer's session knows what was said in the meeting afterwards. Default proposal: fork for the meeting, then write a `meeting-notes/<date>.md` into the project so the working session can pick it up.

---

## 3. Two brains: how they share context

**Briefing.** At join time the deep agent is asked to write a *meeting briefing*, capped at roughly 2,500 tokens: who the bot is and who it works for, project status as known from the session, decisions taken and their reasons, open questions, pointers to key files and commands, a glossary of project terms, and a list of questions people are likely to ask with short answers. The fast responder's system prompt is `persona + speaking rules + briefing`, with a prompt-cache breakpoint after it. Only the transcript window changes per call, so the cache hits on every fast turn.

**Rolling transcript window.** The fast responder sees the last 2–4 minutes of speaker-attributed transcript, the last things the bot itself said, and the utterance being answered. That is enough to resolve "what she just said" and "the second option".

**Delegation.** When the fast responder decides the question needs the repo, a tool, a calculation, or more certainty than the briefing offers, it calls `delegate(question, context, why)` instead of answering. The bridge immediately plays a bridge clip and hands the question to the deep agent as a streaming-input user message that names the asker, quotes the question, and includes the last minute of transcript. The deep agent answers with `say()` (one to three spoken sentences) and `post_chat()` (details, code). If the meeting has moved on by the time the answer is ready, the floor manager downgrades to chat-only plus a one-line spoken pointer when the floor is free.

**Think-ahead.** The deep agent is idle most of the meeting. Every 3–5 minutes (or on a topic change detected by the floor manager) the bridge sends it a transcript digest with the instruction: *do not respond aloud; if anything here changes the briefing or you can anticipate a question and prepare an answer, call `update_briefing`*. This is how the bot gets smarter as the meeting goes on without paying deep-agent latency at question time. It also keeps the deep agent's turn count to roughly 15–20 per hour, which matters for cost (section 8).

**Guardrails for the deep agent during a meeting.** `permission_mode` restricted; a `PreToolUse` hook denies `git push`, deploys, deletes and anything outside the repo; edits allowed only if the user opted in (Q8). Everything it does is logged to the decision log and summarized in the meeting notes.

### Model choices

| Role | Model | Settings | Why |
|---|---|---|---|
| Fast responder | Claude Haiku 4.5 | streaming, no `thinking`, cached system prompt, `max_tokens` ≈ 150 | Lowest measured time-to-first-token of the current models (~0.75 s on 1k-token prompts, lower on short cached prompts); output speed ≈ 80 tok/s |
| Address classifier | Claude Haiku 4.5 | structured output, ~20 output tokens | Cheap, runs in parallel with the speculative answer |
| Deep agent | Claude Opus 5 via the Agent SDK | adaptive thinking on (default), `effort: low` for conversational turns, raised for real tasks | Quality where it matters; latency is hidden behind the bridge phrase |

If the fast responder's quality is not enough, the next step is Claude Sonnet 5 with `thinking: {type: "disabled"}`; measure the time-to-first-token difference before switching.

---

## 4. Turn lifecycle

### Fast path: a question the briefing can answer

1. Alice: "Claude, what did we decide about the Centreon dependency?" Speech ends at *t = 0*.
2. Flux emits `EagerEndOfTurn` at ~150 ms. The floor manager sees the bot's name in the transcript, so it is addressed; no classifier needed. The fast responder starts **speculatively** with the interim transcript.
3. Flux confirms `EndOfTurn` at ~300 ms. If Alice had resumed talking instead (`TurnResumed`), the speculative call is cancelled.
4. First token arrives at ~600–700 ms. Sentence chunker accumulates until the first sentence boundary (~900 ms).
5. The sentence goes to the ElevenLabs WebSocket in `auto_mode`; first audio bytes arrive ~150 ms later; the avatar page plays them after a 100–200 ms jitter buffer.
6. Alice hears the first word at ≈ 1.1–1.3 s. Sentences two and three follow with no gaps because generation is ahead of playback.
7. The bot's spoken text is written to the transcript store, so the fast responder and deep agent both know what was said.

Without the speculative start the same path lands at ≈ 1.3–1.5 s; that is the phase-0 baseline, and the overlap tricks are what bring it down.

### Delegated path: a question that needs the repo

1. Bob: "Claude, can you show the actual query the alert enrichment uses?"
2. Fast responder recognizes this needs the code and calls `delegate`. Total decision time ≈ 500–700 ms from end of speech (rules can short-circuit earlier for obvious cases like "show me the code").
3. The speaker plays a pre-rendered bridge clip in the bot's own voice: "Let me pull up the exact query. Give me a moment." Eight to twelve variants, chosen at random, so it never repeats the same phrase twice in a row. The avatar tile switches to *working on it*.
4. The deep agent receives the question, greps the repo, reads the file (5–30 s).
5. It calls `post_chat` with the query in a fenced block (or a snippet link if it exceeds the platform limit) and `say("Posted it in the chat. It joins alerts to the host table on host_id and filters to the last 15 minutes.")`.
6. The floor manager waits for a 600 ms silence and lets the sentence play. If the discussion has moved to another topic, it posts to chat only and speaks a shorter pointer at the next natural pause.

### Barge-in

Any human speech longer than ~400 ms (or two words) while the bot is speaking: cancel the LLM stream, close the ElevenLabs context, flush the avatar page's playout buffer, and truncate the bot's transcript entry to the characters actually played using the alignment timestamps. A half-second "mm-hm" is a backchannel and is ignored. Reaction target under 150 ms; the avatar tile returns to *listening*.

Because the bot's own audio arrives at participants through the platform, and Recall gives the bridge per-participant streams that do not include the bot's output, the bridge never hears itself and needs no echo cancellation. This is expected from the vendor architecture but must be verified in phase 0.

---

## 5. Floor policy (the meeting-specific part)

**Addressed to me** when any of these hold:

- the utterance contains the bot's name or a variant ("Claude", "hey Claude", the configured display name);
- it arrives within an 8-second *follow-up window* after the bot last spoke and is a question or an instruction;
- a chat message mentions `@Claude`;
- the organizer used a configured hotkey phrase ("Claude, jump in").

Ambiguous cases (a question with no name, right after someone mentioned the bot) go to the Haiku classifier, which runs in parallel with the speculative answer. Classification gates whether audio is *released*, not whether generation starts, so it costs no latency on the yes path.

**May speak** only when: nobody has spoken for 400–700 ms (configurable), the bot is not already speaking, and the interjection budget is not exhausted. If two humans are talking over each other, the bot waits.

**How long.** Spoken answers are one to three sentences, roughly 20 seconds maximum, unless someone asks it to go on. Lists, code, URLs, numbers with more than three digits, and anything with structure go to chat, and the bot says so ("I've put the three options in the chat").

**Interjection budget.** Unprompted contributions (a correction, a reminder of a decision) are off by default (Q10). If enabled, at most one per five minutes and only into a silence of at least one second, phrased as an offer ("If it helps, we settled that last week; want me to recap?").

**Speaking style.** No markdown in speech; contractions; numbers read as words; no filler "um"; short acknowledgement before a delegated answer only, never before a fast answer. The persona prompt carries these rules; the sentence chunker strips anything that slips through.

**Identity and disclosure (Q1).** Display name states it is an AI ("Roger · AI assistant (owner's name)"), the bot announces itself once when it joins, and it says when it is recording. Voice: an ElevenLabs stock voice or an instant clone of a *consenting* person; never a colleague's voice without consent.

---

## 6. Latency engineering

Target budget for the fast path, end of speech to first audio, through a vendor bot:

| Stage | Budget (target) | Notes |
|---|---|---|
| Platform → bot → bridge audio transport | 80–150 ms | Recall publishes no ms figure; measure in phase 0 |
| End-of-turn detection + final transcript | 200–300 ms | Flux EOT; eager EOT fires 150–250 ms earlier |
| Fast responder time-to-first-token | 350–600 ms | Haiku 4.5, short cached prompt, streaming |
| First sentence complete | +150–250 ms | ~15 tokens at 80 tok/s |
| TTS first audio | 130–200 ms | Flash v2.5 over WS, measured medians ~135 ms |
| Jitter buffer + bot → platform → listener | 150–250 ms | Keep the avatar page buffer at 100–200 ms, not 500 |
| **Sequential total** | **≈ 1.1–1.7 s** | Phase 0 expectation |
| **With overlap** | **≈ 0.9–1.2 s** | Phase 2 target |

Techniques, in order of payoff:

1. **Speculative generation at eager end-of-turn.** Start the fast responder on the interim transcript when Flux says the turn is probably over; cancel on `TurnResumed`. Costs 50–70 % more fast-path calls, which are cheap.
2. **Gate release, not generation.** Run the address classifier alongside the answer and only decide at release time.
3. **Stream sentence by sentence into TTS** with `auto_mode`; never wait for the whole answer. Keep one ElevenLabs WebSocket open per meeting (keep-alive), one *context* per utterance so barge-in closes a context rather than a socket.
4. **Prompt caching.** Persona + rules + briefing are the cached prefix; the transcript tail is the only variable part. A pre-warm call at join time populates the cache before the first question.
5. **Pre-rendered bridge clips** in the bot's voice: zero-latency acknowledgement for delegated questions. Do not use them before fast answers; it sounds robotic.
6. **Think-ahead** by the deep agent (section 3) so more questions land on the fast path.
7. **Small playout buffer** on the avatar page and PCM (not MP3) end to end; decoding MP3 adds buffering.
8. **Warm everything at join:** STT stream open, TTS socket open, LLM cache primed, deep agent session resumed and briefing produced before the bot is admitted.
9. **Co-locate.** If the bridge moves to the cloud, put it in the same region as Deepgram, ElevenLabs and Anthropic endpoints (US-East).
10. **Never enable thinking on the voice path.** Time-to-first-token with thinking is seconds to minutes.

What the listener experiences also depends on the avatar tile. A visible *listening → about to speak → speaking → working on it* state costs nothing and makes a one-second pause feel intentional rather than broken.

---

## 7. Deployment

**Phase 0–2: bridge on the developer's Mac.** It needs the repo, the Claude Code session files and the local Claude auth. The vendor bot connects *to* the bridge's WebSocket, so the bridge needs a public HTTPS/WSS URL: a Cloudflare Tunnel or ngrok in front of `localhost`. The avatar page is served from the same tunnel. Secrets (Recall, Deepgram, ElevenLabs, Anthropic) live in the local keychain or an `.env` outside the repo.

**Later: bridge in the cloud.** One container per meeting, a session store so the Agent SDK can resume the session on another host, a repo checkout, and the same tunnel-free public endpoint. Attendee self-hosted on the same host would remove the vendor hop entirely and is the natural fallback if Recall's Output Media latency measures poorly.

**Platform access to arrange before phase 3:**

- Google Meet: since April 2026 Google routes suspicious guest joins to a deny-by-default queue. Plan for an authenticated Google Workspace identity for the bot (vendors do this with a dedicated Workspace user per concurrent bot). For our own meetings, the organizer can also admit the bot manually.
- Teams: tenants default to "require approval when an external bot is detected" and may disable anonymous join. Either the organizer admits the bot from the lobby, or the bot uses a signed-in M365 account. Chat posting on Teams requires a chat-enabled tenant or the signed-in bot.

---

## 8. Cost per meeting hour (rough)

| Item | Basis | Estimate |
|---|---|---|
| Meeting bot (Recall, 4-core for per-participant audio) | $0.60/hr | $0.60 |
| Streaming STT (Deepgram Flux) | $0.0065/min continuous | $0.39 |
| TTS (ElevenLabs Flash) | bot speaks ~10 min/hr ≈ 8k chars at $0.05/1k | $0.40 |
| Fast responder + classifier (Haiku 4.5) | ~60–100 short cached calls | $0.10–0.30 |
| Deep agent (Opus 5, resumed session) | 15–25 turns/hr over a 50–150k-token cached context, tool use | $2–6 |
| **Total** | | **≈ $4–8 per meeting hour** |

The deep agent dominates and scales with how much the session already contains and how often it is asked. Prompt caching keeps re-reading the session cheap; the think-ahead cadence is the main dial.

---

## 9. Phased plan

**Phase 0 — Hear and speak (3–5 days).** Recall bot joins a Google Meet we organize; audio streams to the local bridge through a tunnel; Flux transcribes; a name-gated Haiku answer is spoken through Flash into the Output Media avatar page. Exit criteria: measured end-to-end latency distribution; barge-in stops speech; the bridge does not hear itself.

**Phase 1 — Two brains (1–2 weeks).** Agent SDK deep agent resumed from a real session; briefing at join; delegation with bridge clips; `say` / `post_chat` / `update_briefing` tools; transcript store; post-meeting notes file; chat posting with snippet links.

**Phase 2 — Human (1–2 weeks).** Speculative generation on eager end-of-turn; floor policy tuning with recordings of real meetings; interjection budget; think-ahead; avatar tile states; voice selection; FR/EN if needed (Q6).

**Phase 3 — Teams and hardening (1–2 weeks).** Teams join with a signed-in bot account or organizer admission; tenant policy checks; observability dashboard; cloud deployment option; Attendee evaluation as a fallback.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Vendor bot output-media latency is unpublished and could be high | Measure in phase 0; fallback to Attendee self-hosted next to the bridge |
| Meet threat queue or Teams external-bot policy blocks the bot | Authenticated bot identities; organizer admission for our own meetings |
| Bot talks over people or answers questions not meant for it | Floor policy; classifier gating; interjection budget off by default; kill phrase ("Claude, mute") |
| Fast responder states project facts that are wrong | Restricted to the briefing; "I'd have to check" plus delegation when unsure; deep agent's answers cite files |
| Deep agent takes an unwanted action during a meeting | Hooks deny push/deploy/delete; edits opt-in; decision log |
| Agent SDK `interrupt()` during a thinking phase surfaces as an error result (known issue) | Treat that result as a clean cancel; drain the buffer before the next query |
| ElevenLabs concurrency or character quota | One meeting uses one or two concurrent streams; Creator or Pro plan is enough |
| Meet chat is plain text, 500 characters | Snippet links for anything longer; short inline code only |

---

## 11. Open questions

1. **Identity and disclosure.** Display name, join announcement wording, recording notice, whether the bot appears as "AI assistant of <owner>" or as a team tool.
2. **Voice.** Stock ElevenLabs voice vs an instant clone of a consenting person. Accent and language.
3. **Continue or fork** the working Claude Code session for the meeting, and where meeting outcomes land afterwards (notes file, session, both).
4. **Where the bridge runs.** Developer Mac with a tunnel first; is a cloud deployment required, and when?
5. **Platform access.** Do we control the organizer side for the meetings that matter (admit the bot, adjust Teams meeting policies)? Are authenticated bot accounts acceptable?
6. **Languages.** English only, or bilingual FR/EN? This decides Flux EN vs multi and the TTS `language_code` handling.
7. **Budget and vendor.** Ceiling per meeting hour; Recall hosted vs Attendee self-hosted.
8. **Deep agent permissions during meetings.** Read-only repo? Allowed to run tests? Allowed to edit files when asked?
9. **Snippet destination** for code longer than the chat limit: private gist, an internal paste service, or a hosted page.
10. **Proactive interjections.** Only answer when addressed (default), or allowed to correct or remind within a budget?

---

## Appendix A — Agent SDK integration sketch (Python)

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, tool, create_sdk_mcp_server

@tool("say", "Speak one to three short sentences to the meeting.", {"text": str, "priority": str})
async def say(args): await speaker.enqueue(args["text"], args.get("priority", "normal")); return ok()

@tool("post_chat", "Post text or code to the meeting chat. Long content becomes a link.", {"text": str})
async def post_chat(args): await chat.post(args["text"]); return ok()

@tool("update_briefing", "Replace the fast responder's briefing (<= 2500 tokens).", {"markdown": str})
async def update_briefing(args): briefing.set(args["markdown"]); return ok()

@tool("read_transcript", "Return transcript since an ISO timestamp.", {"since": str})
async def read_transcript(args): return text(transcript.since(args["since"]))

meeting_tools = create_sdk_mcp_server("meeting", "1.0.0", [say, post_chat, update_briefing, read_transcript])

options = ClaudeAgentOptions(
    resume=SESSION_ID, fork_session=True,          # Q3: fork vs continue
    cwd=PROJECT_DIR,
    include_partial_messages=True,
    mcp_servers={"meeting": meeting_tools},
    allowed_tools=["mcp__meeting__*", "Read", "Grep", "Glob", "Bash"],
    permission_mode="acceptEdits",                  # Q8
    hooks={"PreToolUse": [deny_push_deploy_delete]},
    system_prompt={"type": "preset", "preset": "claude_code", "append": MEETING_RULES},
)

async with ClaudeSDKClient(options) as deep:
    await deep.query(BRIEFING_REQUEST)              # at join
    ...
    await deep.query(delegation_message(asker, question, recent_transcript))
    async for msg in deep.receive_response(): log(msg)
    ...
    await deep.interrupt()                          # on barge-in; then drain receive_response()
```

The fast responder is a plain Anthropic SDK call (`client.messages.stream`, model `claude-haiku-4-5`, no `thinking`, `cache_control` on the system block) with three tools: `delegate`, `post_chat`, `stay_silent`.

## Appendix B — Sources

See `research/01-meeting-join-options.md` and `research/02-voice-pipeline-latency.md` for the sourced findings behind every vendor claim and number in this document.
