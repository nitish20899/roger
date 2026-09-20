<h1 align="center">Roger</h1>

<p align="center">
  A voice AI teammate that joins your Google Meet, Microsoft Teams and Zoom calls.<br>
  It listens, answers when spoken to, and knows what you have been working on.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg">
  <img alt="Status: beta" src="https://img.shields.io/badge/status-beta-orange.svg">
</p>

```bash
roger run "https://meet.google.com/xxx-xxxx-xxx"
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

## See it work

<!--
  TO ADD THE DEMO VIDEO:
  1. Open this repo on github.com and click the pencil to edit README.md
     (or open any new issue — you do not have to submit it).
  2. Drag your .mp4, .mov or .webm into the text box. GitHub uploads it and
     replaces it with a link like https://github.com/user-attachments/assets/<id>
  3. Put that bare URL on its own line below, replacing this comment block.
     A bare user-attachments URL renders as a player; a video committed to the
     repo does not, so it has to be uploaded this way. Limit is 100 MB.
-->

*Demo video coming soon.*

## Install

You need Python 3.11+, [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
(`brew install cloudflared`), and two accounts:

| Service | For | Cost |
|---|---|---|
| [Attendee](https://app.attendee.dev) | the participant that joins the meeting | free hours, then hourly |
| [OpenAI](https://platform.openai.com) | hearing, speaking, thinking and web search | per second of voice, plus tokens |

```bash
git clone https://github.com/nitish20899/roger.git
cd roger
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env     # then paste your two keys
roger doctor             # checks everything before you join a call
```

`roger doctor` should come back all `ok`. If it does not, it tells you exactly what to fix.

## Join a meeting

The same command works for all three platforms. **Quote the URL** — Teams and Zoom links contain `?`,
which your shell will otherwise try to expand:

```bash
# Google Meet
roger run "https://meet.google.com/abc-defg-hij"

# Microsoft Teams  (keep the ?p= passcode — the join fails without it)
roger run "https://teams.microsoft.com/meet/253353451530065?p=MsM6cGyY57i4H8faUq"

# Zoom
roger run "https://us02web.zoom.us/j/12345678901?pwd=abcdef"
```

Roger starts a Cloudflare tunnel, sends the bot in, and stays until you press Ctrl-C. If it lands in a
waiting room, admit it by name — it shows up as whatever `BOT_NAME` is set to.

While a meeting is running, from another terminal:

```bash
roger status                     # what it is doing right now
roger say "hello everyone"       # make it say something word for word
roger leave                      # send it home
```

No meeting handy? Run `roger serve`, open <http://localhost:8787/monitor> to listen, and talk to it with
`roger ask "Roger, what does this project do?"`.

## Give it context

This is the part worth setting up, and the reason Roger is different from a meeting bot that just
transcribes. Point it at the coding sessions you have actually been working in and it arrives already
knowing what they know — the decisions, the trade-offs, the half-finished thing you meant to come back to.

Start by seeing what is available:

```bash
roger sessions                    # sessions for the current directory
roger sessions ~/code/myproject   # or for a specific project
```

```
claude  62127f11-a046-4c2c-92f0-8a6ded918ba2   8.0 MB    0.0 h ago   2737 lines  [Meeting bot architecture]
codex   01a08d6a-ee7d-7c73-94ae-1ccc7b988c20   0.8 MB  230.5 h ago    126 lines  [Evaluate Voicebox alternative]
```

### Claude Code

Claude Code keeps a transcript per project under `~/.claude/projects/`. Attach the newest one, or name a
specific session by id, by the first few characters of the id, or by its title:

```bash
roger run "<meeting-url>" --session latest --project ~/code/myproject
roger run "<meeting-url>" --session 62127f11
roger run "<meeting-url>" --session "Meeting bot architecture"
```

### Codex

Codex stores its sessions under `~/.codex/sessions/`. Same flags, plus `--engine codex` when a session id
or title could match either client:

```bash
roger run "<meeting-url>" --session latest --engine codex
roger run "<meeting-url>" --session 01a08d6a --engine codex
```

### Both at once

Set them in `.env` and every run picks them up, no flags needed. Both clients can be attached together —
Roger reads both transcripts and writes one briefing:

```bash
CLAUDE_SESSION_ID=62127f11
CODEX_SESSION_ID=01a08d6a
PROJECT_DIR=/Users/you/code/myproject
```

### What Roger actually does with them

Those session files are **read as text**, once, to write a briefing before the call. Neither Claude Code
nor Codex is ever executed — nothing is billed to them, and your real sessions are never resumed, written
to or modified. Everything runs on your OpenAI key.

Repository access is read-only in the same way: Roger can search and read files under `PROJECT_DIR`, and
there is deliberately no tool that edits a file, runs a command, or pushes anything. A meeting bot with
your repo open should be able to answer questions about the code and nothing else.

The briefing is cached for six hours, so restarting mid-meeting is quick. Delete `~/.roger/briefing-*.txt`
to force a fresh one.

## Self-hosting Attendee

Attendee is [source-available](https://github.com/attendee-labs/attendee) and can run on your own machine,
which leaves OpenAI as the only paid API. Roger talks to it over the same REST API either way:

```bash
git clone https://github.com/attendee-labs/attendee && cd attendee
docker compose -f dev.docker-compose.yaml build
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env
docker compose -f dev.docker-compose.yaml up
docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
```

Then create an API key at <http://localhost:8000> and point Roger at it:

```bash
ATTENDEE_BASE=http://localhost:8000/api/v1
ATTENDEE_API_KEY=<the key from your own instance>
```

It is cheaper, not simpler: you are running four app containers plus PostgreSQL and Redis, and Attendee's
own docs say local Celery is not production-grade — production wants Kubernetes. Zoom additionally needs
your own Zoom OAuth credentials. The hosted service is the easier path; self-hosting is the cheaper one at
volume.

Attendee is under the **Elastic License 2.0**, not an OSI open-source licence. Running it yourself is
fine; redistributing it, or offering it to others as a service, is not. That is also why Roger integrates
with it over HTTP rather than vendoring any of its code — Roger stays MIT.

## Configure

Every setting is documented inline in [`.env.example`](.env.example). The ones people actually change:

| Variable | Default | |
|---|---|---|
| `BOT_NAME` | `Roger` | the name people say, and the name in the participant list |
| `OWNER_NAME` | empty | who it says it is assisting |
| `VOICE` | `cedar` | `cedar` male, `marin` female, plus 20 others |
| `FAST_MODEL` | `gpt-5.6-luna` | the model that does the thinking |
| `OUTPUT_BUFFER_MS` | `500` | raise it if the voice ever breaks up |
| `WEB_SEARCH`, `REPO_TOOLS` | `1` | turn either source off |
| `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID` | empty | the sessions to read for context |

## How it works, briefly

One [OpenAI GPT-Live](https://developers.openai.com/api/docs/guides/live) session does the hearing, the
speaking and the turn-taking on a single full-duplex WebSocket — it listens while it talks, so you can
genuinely interrupt it. When it needs a fact it *delegates*: OpenAI runs a backend model, hands it the
conversation, and Roger runs the tools it asks for (searching your repository, posting to the meeting
chat) while the conversation carries on out loud. [Attendee](https://attendee.dev) puts the participant in
the call and carries the audio both ways.

Longer notes are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Troubleshooting

| | |
|---|---|
| `zsh: no matches found: https://…` | quote the URL — Teams and Zoom links contain `?` |
| `roger doctor` reports a missing key | edit `.env`; it is read from the current directory, then the repo root, then `~/.roger/.env` |
| `roger` behaves like an older version | you have a stale copy installed: `pip uninstall roger-meeting-agent && pip install -e .` |
| The bot joins but never hears anything | `roger status`: `bot_audio_ws` must be true. Attendee needs to reach your public URL over `wss://` |
| It sits in the waiting room | Google Meet and Teams often hold guest bots; admit it by name |
| The voice breaks up | raise `OUTPUT_BUFFER_MS` (try 700) |
| It answers itself in a loop | set `ECHO_SUPPRESS=1` |
| It talks when it should not | say *"hold on, Roger"*; it stays silent until you use its name |
| It does not know about recent work | the briefing caches for six hours — delete `~/.roger/briefing-*.txt` |

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). `make test` runs the suite;
it needs no API keys.

## License

MIT. See [LICENSE](LICENSE).
