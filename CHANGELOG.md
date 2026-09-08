# Changelog

## 0.1.1 - 2026-09-08

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
