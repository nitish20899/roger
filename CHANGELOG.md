# Changelog

## 0.5.0 - 2026-09-20

**Roger joins meetings by itself. One key, one service.**

- **No meeting-bot service.** Roger opens the call in a Chromium it drives on this machine and walks in
  like a person. `ATTENDEE_API_KEY` is no longer required, and neither is a public URL, a tunnel or
  `cloudflared` -- the audio socket is `127.0.0.1`. OpenAI is the only key and the only bill.
- **New `roger/meeting/` package**, and it is the point of the release. Everything above it talks to a
  `MeetingClient` -- join, leave, send_audio, send_chat -- and subscribes to an event bus, so nothing
  outside it knows what a Google Meet is:
  - `browser.py` drives Chromium; `assets/bridge.js` is injected into the meeting page, patches
    `getUserMedia` to hand out a microphone fed by Roger's voice, and taps every inbound WebRTC track.
  - `platforms/google_meet.py` and `platforms/teams.py` are *only* selectors and a join flow. Adding a
    platform is one class; adding a consumer (a recorder, a note-taker) is `meeting.on(Event.AUDIO, fn)`.
  - `attendee.py` still exists behind the same interface as an opt-in fallback: `MEETING_PROVIDER=attendee`.
- **Fixed a bug that made Roger mute in every meeting.** Chrome hands WebRTC a *silent* track from a
  `MediaStreamAudioDestinationNode` whose AudioContext is not at the browser's native sample rate --
  correctly formed, completely empty, no error anywhere. The outbound context now runs at the browser's
  rate and the inbound one at 24 kHz. `tests/test_browser_audio.py` pins it with a real WebRTC loopback
  in a real browser, and CI runs it.
- **`speaker.py` no longer knows where audio goes.** It writes to a sink the meeting client owns, which
  is what let the browser and hosted participants share every line above them.
- Settings: added `MEETING_PROVIDER`, `BROWSER_HEADLESS`, `BROWSER_DEBUG`; removed the unused
  `ATTENDEE_USE_LOGIN` and `ATTENDEE_LOGIN_GROUP`. `PUBLIC_URL` is now only read by the hosted provider.
- Zoom is no longer claimed. It was only ever the hosted provider's, and Roger does not drive it yet.

## 0.4.0 - 2026-09-20

**A clean-out. Two services, no dead weight.**

- **The orb webcam is gone**, along with the React source, the built page, the fallback shader, the
  `ORB_*` settings and Attendee's voice-agent mode. It looked good and did nothing; the voice now always
  goes over the audio WebSocket, which removes `AUDIO_OUT` and the page-microphone path with it.
- **Anthropic is gone entirely.** No SDK, no key, no `FAST_PROVIDER`. The only services Roger talks to are
  OpenAI and Attendee.
- **The attention classifier is gone.** It ran a model on every utterance to decide whether the bot was
  being addressed; across live meetings it judged 110 times and intervened **zero** times, because the
  conversation prompt already does that job. What it was genuinely good for -- holding on "hold on, Roger"
  -- is now 60 lines of regex in `roger/manners.py` with no model in the loop.
- **Dead code removed**: `text.py` (its helpers had no callers left), `tunnel.py` (folded into
  `server.py`), `attention.py`, and the `scripts/` directory. `ECHO_SUPPRESS` is a plain switch now the
  orb page cannot loop audio back.
- **Docs**: the README is for people who want to use Roger; the design notes moved to
  `docs/ARCHITECTURE.md` and the stale vendor research is deleted. Added GitHub Actions CI across
  Python 3.11-3.13.
- 18 modules to 15, and roughly a third less code, with no loss of behaviour.

## 0.3.0 - 2026-09-20

**One vendor, one key. GPT-Live now runs the backend too, and coding sessions are context rather than agents.**

- **Responses delegation replaces client delegation**, which is what the GPT-Live guide recommends. The backend
  model, its prompt and its tools are declared in `session.start`; OpenAI runs that model and *passes it the
  conversation itself*. This removes a whole class of bugs: the delegation event carries no task text, so the old
  client-side backend had to reconstruct the question from the transcript and could answer the wrong one.
- **Claude Code and Codex sessions are read for context, never run.** Their rollout files are parsed as text and
  one OpenAI call turns them, plus the repository, into the briefing. Neither client is invoked, neither is
  billed, and your real sessions are never resumed or written to. Both can be attached at once, and
  `roger sessions` lists them together. `claude-agent-sdk` is no longer a dependency.
- **The backend has three sources**: the briefing, the project's files, and the web. Repo access is Roger's own
  read-only tools (`search_repo`, `read_file`, `list_files`) executed in-process and confined to `PROJECT_DIR`;
  there is deliberately no tool that edits, runs or pushes anything. Web search is GPT-Live's hosted tool.
- **Default models are `gpt-5.6-luna`** for the backend and the briefing. `gpt-5.6-sol` and `gpt-5.6-terra` also
  work; note that models after gpt-5.5 reject `reasoning_effort: "minimal"`, so Roger uses `"none"`.
- **Default voice is `cedar`** (male) rather than `marin`.
- Removed: `roger/deep.py`, `roger/backend.py`, and the `DEEP_*` settings. Added: `roger/tools.py`,
  `roger/context.py`.

## 0.2.0 - 2026-09-19

**ElevenLabs is gone. Hearing, speaking and turn-taking are now one OpenAI GPT-Live session.**

- **GPT-Live (`gpt-live-1`) replaces ElevenLabs Scribe and Flash.** One full-duplex WebSocket
  (`wss://api.openai.com/v1/live/sessions`) carries meeting audio in and the bot's voice out, at 16 kHz PCM16 to
  match Attendee, so nothing is resampled. The model listens while it speaks, so interruptions are native rather
  than bolted on. New module `roger/live.py`; `stt.py`, `tts.py` and `elevenlabs.py` are deleted.
- **Client delegation replaces the fast-responder loop.** GPT-Live holds the conversation and raises
  `session.delegation.created` when it needs a fact; `roger/backend.py` answers it and sends the result back with
  `session.commentary.append`, which the model paraphrases in its own voice. Because the conversation continues
  while the backend works, the pre-rendered "let me pull that up" bridge clips are gone.
- **The backend is `gpt-4.1`** (was `gpt-4.1-mini`) over the briefing and transcript; `gpt-4.1-mini` is still a
  supported, faster choice. Claude Haiku remains available with `FAST_PROVIDER=anthropic`.
- **Claude Code is unchanged in role and now feeds the voice model directly.** It still writes the briefing and
  answers repository questions; the briefing also seeds the live session as conversation history, so the bot knows
  the project from its first word. Its `say` tool now hands facts to GPT-Live instead of synthesising speech.
- **Meeting manners moved into the conversation prompt**, following the Live prompting guide's structure
  (`Backchannel policy`, `Interruption policy`, `Delegation policy`). The attention classifier is no longer a gate
  on the speech path: it now runs alongside as `ATTENTION_GUARD`, cutting the bot off if it answers something that
  was not addressed to it, and driving `session.input_audio.mute` for "Roger, mute".
- **Holding works the way a meeting does.** "Hold on, Roger", "wait, Roger", "Roger, one second" and similar
  stop it mid-sentence; it then stays silent, while still following the conversation, until someone says its
  name again. A follow-up without the name does not end a hold. This deliberately does *not* use
  `session.input_audio.mute`: muting the input would make the bot deaf, and hearing its own name is the only
  way out of a hold, so a muted bot could never be called back.
- **Silence keepalive.** The GPT-Live session timeline only advances while input audio is being appended, and
  appended context is delivered against that timeline. Roger now sends silence whenever no meeting audio is
  arriving, so `roger say` and `roger ask` work with no meeting connected and a stalled audio feed cannot
  strand instructions (previously reported as `context_injection_incomplete` at close).
- **Default voice is `cedar`** (male) rather than `marin`; both are GPT-Live's flagship pair.
- **Audio runs at 24 kHz end to end** (`AUDIO_RATE`). It is GPT-Live's native rate, and Attendee's own guidance
  for an OpenAI voice is to use 24000, so samples now cross the whole path without being resampled once.
  Per-participant streams stay at 16 kHz, which is Attendee's cap; they only feed the speaker-attribution meter.
- **Output audio has a jitter buffer.** GPT-Live streams at real time -- about a second of audio per second
  -- where a text-to-speech API returned a whole sentence far faster than it takes to say it. The old path
  therefore ran up to 0.4 s ahead and the far end always had a cushion; with GPT-Live that cushion was zero,
  and every network wobble was audible as a stutter. The first ~350 ms of each turn is now held back and
  released in one burst to rebuild the cushion, after which output is *paced* to stay exactly one cushion ahead
  of the wall clock, so bursty arrivals leave as an even 100 ms cadence. Only whole frames are sent -- deltas
  arrive at mixed sizes -- and silence is not transmitted at all. The cushion counts *speech* frames, not the
  quiet run-up: counting everything released a turn on its first voiced frame, leaving the far end with a
  cushion of silence that ran dry on reaching the real audio. Measured delivery stalls for up to ~360 ms, so
  `OUTPUT_BUFFER_MS` defaults to 500.
- **A delegation no longer answers before it has heard the question.** GPT-Live raises
  `session.delegation.created` as soon as it understands a request, often a second before the speaker has
  stopped and the transcript turn has closed, so the backend was answering from a transcript that did not yet
  contain the question ("I didn't catch a question for me there"). The delegation now waits for the utterance
  in flight to settle first.
- **The briefing verifies itself against the working tree.** A forked Claude Code session summarises its own
  memory, which can predate the code: the first briefings after this migration confidently described the bot
  as using ElevenLabs. The briefing prompt now requires reading the README, the main sources and the changelog
  before describing which services and models are in use, and treats the files as authoritative.
- **The backend does not narrate itself.** It returned holding lines like "I'll check the implementation and
  code size", which GPT-Live then read out. It now returns facts only; covering the wait is the voice model's
  job, and it does it in its own words.
- **The bot's "speaking" state is measured from audio energy**, because GPT-Live streams output continuously:
  real-time silence while it listens, speech while it talks. Treating any arriving audio as speech would have
  left the orb stuck on *speaking* and held the echo gate shut for the whole meeting.
- **New `ECHO_SUPPRESS`** (`off` | `page` | `all`, default `page`) replaces the text-level echo filter, which could
  not work any more: the model hears audio directly, so a loop has to be stopped before it reaches the session.
- **Configuration.** `OPENAI_API_KEY` is now required and `ELEVENLABS_API_KEY` is gone. `VOICE` (default `marin`)
  and `LIVE_MODEL` replace `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_MODEL` and `ELEVENLABS_STT_MODEL`;
  `STT_SILENCE_S`, `VOICE_STABILITY`, `VOICE_STYLE` and `BARGE_IN_ENERGY` are gone, since GPT-Live owns turn
  detection and interruption. `roger doctor` checks the OpenAI key and the voice name instead of credit balance.
- `/health` reports the live session: connection, voice seconds used, context window, delegations and guard hits.
- The orb webcam is unchanged. It is the MIT-licensed ElevenLabs UI *component*; no ElevenLabs account or API is
  involved any more.

## 0.1.1 - 2026-09-08

- ElevenLabs credit check in `roger doctor` and at startup; clear errors and a 60 s back-off when credits run out.
- `DEEP_MODEL` defaults to the `sonnet` alias so projects behind a gateway (Bedrock, Vertex, Databricks) resolve it.
- `roger sessions` shows session titles; `--session` accepts a title or an id prefix.
- Platform notes for Teams and Zoom; `ATTENDEE_USE_LOGIN` for signed-in bot accounts; voice falls back to the audio
  websocket when the orb page is rejected.
- `.env` is also read from `~/.roger/.env`, so `roger` works from any directory after a plain `pip install .`.
- Default install is now non-editable (`make dev` for editable); documents the macOS hidden-`.pth` pitfall.
- `roger run` and `roger serve` take `--session <id|latest>` and `--project <dir>` to attach a Claude Code session
  from the command line; `latest` picks the newest session for the project. Flags override `CLAUDE_SESSION_ID`
  and `PROJECT_DIR`.
- Sentence splitter no longer drops short sentences before a longer one.

## 0.1.0 - 2026-09-07

- First release: Attendee meeting bot, ElevenLabs Scribe/Flash, OpenAI or Anthropic fast responder, Claude Code deep
  brain through the Agent SDK, attention manager with gated release, per-participant speaker attribution, barge-in,
  echo filtering, ElevenLabs UI orb webcam, `roger` command line.
