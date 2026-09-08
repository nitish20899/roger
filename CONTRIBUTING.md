# Contributing

Thanks for looking under the hood. Roger is small enough to read in an afternoon; start with `roger/bridge.py`.

## Setup

```bash
git clone https://github.com/nitish20899/roger.git && cd roger
make setup            # venv + editable install + .env from the example
make test             # offline tests, no keys needed
```

## Ground rules

- **Never commit secrets.** `.env` is ignored; keep it that way. Use `.env.example` for new variables, with a comment.
- **Keep the voice path fast.** Anything added between a committed transcript and the first spoken sentence needs a reason and a measurement (`latency:` lines in the log).
- **Attention logic gets a test.** Changes to `attention.py` come with a case in `tests/test_attention.py`; prompt changes should keep `scripts/eval_attention.py` at or above 23/24 (add cases if you found a new failure mode).
- **Prompts live in `prompts.py`.** No prompt text elsewhere.
- **Settings come from the environment.** New knobs go through `Settings.from_env()` and are documented in `.env.example` and the README table if they matter to users.

## Working on the orb

```bash
cd orb && npm ci && npm run dev      # Vite dev server; the page expects a Roger server on :8787
make orb                             # rebuild into roger/static/orb (commit the output)
```

## Pull requests

One change per PR, a short description of what you tried in a meeting (or with `roger ask` / `scripts/loop_test.py`),
and `make test` green.
