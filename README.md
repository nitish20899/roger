<h1 align="center">Roger</h1>

<p align="center">
  A voice AI teammate that joins your Google Meet and Microsoft Teams calls.<br>
  It listens, answers when spoken to, and knows what you have been working on.<br>
  <b>One OpenAI key. No meeting-bot service.</b>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg">
  <img alt="Status: beta" src="https://img.shields.io/badge/status-beta-orange.svg">
</p>

```bash
roger run https://meet.google.com/xxx-xxxx-xxx
```

Roger joins the call as a participant and waits. Say its name and ask something; it answers out loud.
Keep talking to it without repeating the name. Talk over it and it stops mid-word. Say *"hold on, Roger"*
and it goes quiet until you use its name again.

It can answer from three places: the coding sessions you have been working in, the project's files, and
the web.

> **You:** Roger, what did we decide about the audio buffer?
> **Roger:** Five hundred milliseconds. Anything shorter and the voice breaks up when delivery stalls.
>
> **You:** Roger, check the code — what port does the server use by default?
> **Roger:** Eight seven eight seven.
>
> **You:** Roger, look up what changed in the latest release of that library.
> **Roger:** *(searches the web)* …

## Install

Python 3.11+ and an [OpenAI](https://platform.openai.com) key. That is the whole list. Roger joins the
call itself, in a Chromium it drives on your machine, so there is no meeting-bot service to sign up for
and nothing to make reachable from the internet.

```bash
git clone https://github.com/nitish20899/roger.git
cd roger
python3 -m venv .venv && source .venv/bin/activate
pip install '.[browser]'
playwright install chromium
cp .env.example .env     # then paste your OpenAI key
roger doctor             # checks everything before you join a call
```

The first time, leave `BROWSER_HEADLESS=0` (the default) so you can watch it join. If your meetings need
a signed-in account, sign in once in that window: the profile under `~/.roger` keeps the login.

## Use it

```bash
roger run <meeting-url>          # join, stay until Ctrl-C, then leave
roger run <url> --session latest # ...knowing your newest coding session on this project
roger sessions                   # list the sessions you can attach
roger doctor                     # check configuration and keys
```

While a meeting is running, from another terminal:

```bash
roger status                     # what it is doing right now
roger say "hello everyone"       # make it say something word for word
roger leave                      # send it home
```

No meeting handy? `roger serve`, open <http://localhost:8787/monitor> to listen, and
`roger ask "Roger, what does this project do?"` to talk to it.

## Give it context

This is the part worth setting up. Point Roger at the Claude Code or Codex sessions you have been working
in, and it joins the call already knowing what they know — the decisions, the trade-offs, the half-finished
thing you meant to come back to.

```bash
roger sessions ~/code/myproject                   # see what is there
roger run <url> --session latest --project ~/code/myproject
```

Or set `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID` and `PROJECT_DIR` in `.env`. Both clients can be attached at
once.

Those session files are **read as text**, once, to write a briefing. Neither Claude Code nor Codex is ever
run, nothing is billed to them, and your real sessions are never resumed or written to. Repository access
is read-only too: Roger can search and read files under `PROJECT_DIR`, and there is deliberately no tool
that edits, runs or pushes anything.

## Configure

Every setting is documented inline in [`.env.example`](.env.example). The ones people actually change:

| Variable | Default | |
|---|---|---|
| `BOT_NAME` | `Roger` | the name people say |
| `OWNER_NAME` | empty | who it says it is assisting |
| `VOICE` | `cedar` | `cedar` male, `marin` female, plus 20 others |
| `FAST_MODEL` | `gpt-5.6-luna` | the model that does the thinking |
| `OUTPUT_BUFFER_MS` | `500` | raise it if the voice ever breaks up |
| `WEB_SEARCH`, `REPO_TOOLS` | `1` | turn either source off |
| `BROWSER_HEADLESS` | `0` | `1` hides the window once you trust it |

## How it works, briefly

One [OpenAI GPT-Live](https://developers.openai.com/api/docs/guides/live) session does the hearing, the
speaking and the turn-taking on a single full-duplex WebSocket — it listens while it talks, so you can
genuinely interrupt it. When it needs a fact it *delegates*: OpenAI runs a backend model, hands it the
conversation, and Roger runs the tools it asks for (searching your repository, posting to the meeting
chat) while the conversation carries on out loud.

Getting into the call is Roger's own doing. It opens the meeting in Chromium and walks in like a person,
having first replaced the page's microphone with one that carries GPT-Live's voice and tapped the inbound
WebRTC tracks for its ears. Adding a platform means describing which buttons to press; audio, state and
everything above are already handled.

Longer notes are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Troubleshooting

| | |
|---|---|
| `roger doctor` reports a missing key | edit `.env`; it is read from the current directory, then the repo root, then `~/.roger/.env` |
| The bot joins but never hears anything | `roger status`: `meeting.page_connected` must be true and `audio_streams` above zero |
| It sits in the waiting room | Google Meet and Teams often hold guest bots; admit it by name |
| It cannot find the join button | the page changed. Run with `BROWSER_HEADLESS=0` to watch, and see `roger/meeting/platforms/` |
| Nobody can hear it | check `roger status`: `frames_out` should be climbing while it talks |
| The voice breaks up | raise `OUTPUT_BUFFER_MS` (try 700) |
| It answers itself in a loop | set `ECHO_SUPPRESS=1` |
| It talks when it should not | say *"hold on, Roger"*; it stays silent until you use its name |
| It does not know about recent work | the briefing caches for six hours — delete `~/.roger/briefing-*.txt` |

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). `make test` runs the suite;
it needs no API keys. `ROGER_BROWSER_TESTS=1 pytest tests/test_browser_audio.py` runs the audio path in a
real browser, which is the one thing unit tests cannot cover.

Supporting another platform is the easiest useful contribution: add a
[`Platform`](roger/meeting/browser.py) subclass under `roger/meeting/platforms/` that knows which buttons
to press. Nothing else has to change.

## License

MIT. See [LICENSE](LICENSE).
