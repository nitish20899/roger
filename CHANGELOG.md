# Changelog

## 0.1.1 - 2026-09-08

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
