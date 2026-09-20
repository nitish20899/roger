# Contributing

Thanks for looking under the hood. Roger is small enough to read in an afternoon; start with `roger/bridge.py`.

## Setup

```bash
git clone https://github.com/nitish20899/roger.git && cd roger
make dev              # venv + editable install + .env from the example
make test             # offline tests, no keys needed
```

## Ground rules

- **Never commit secrets.** `.env` is ignored; keep it that way. Use `.env.example` for new variables, with a comment.
- **Keep the voice path fast.** Anything added between hearing a question and the first spoken word needs a reason and a measurement. The log records delegation and tool timings.
- **Measure audio changes, do not guess.** `roger/speaker.py` is a jitter buffer with real constants behind it (GPT-Live delivery stalls up to ~360 ms). If you change one, say what you measured.
- **Prompts live in `prompts.py`.** No prompt text anywhere else.
- **Read-only stays read-only.** The tools in `roger/tools.py` may search and read under `PROJECT_DIR` and nothing more. No tool edits files, runs commands or pushes.
- **Settings come from the environment.** New knobs go through `Settings.from_env()` and are documented in `.env.example` and the README table if they matter to users.


## Pull requests

One change per PR, a short description of what you tried in a meeting (or with `roger ask`),
and `make test` green.
