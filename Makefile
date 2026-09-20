# Convenience targets. Everything here can also be done by hand; see README.md.
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: setup dev run serve doctor test lint clean

setup: $(BIN)/roger .env   ## create a virtualenv, install Roger, create .env from the example
	@echo "Now edit .env with your API keys, then: make doctor"

$(BIN)/roger: pyproject.toml roger/*.py
	test -d $(VENV) || $(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip >/dev/null
	$(BIN)/pip install ".[dev]"

dev: .env              ## editable install for hacking on Roger (changes apply without reinstalling)
	test -d $(VENV) || $(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

.env:
	cp .env.example .env
	chmod 600 .env

run: $(BIN)/roger      ## join a meeting: make run URL=https://meet.google.com/xxx-xxxx-xxx
	@test -n "$(URL)" || (echo "usage: make run URL=<meeting link>"; exit 2)
	$(BIN)/roger run "$(URL)"

serve: $(BIN)/roger    ## start the server and tunnel without joining
	$(BIN)/roger serve

doctor: $(BIN)/roger   ## check keys and tools
	$(BIN)/roger doctor

test: $(BIN)/roger     ## offline tests (no API keys needed)
	$(BIN)/python -m pytest -q

lint: $(BIN)/roger     ## unused imports and obvious mistakes
	$(BIN)/python -m pyflakes roger/*.py tests/*.py

clean:
	rm -rf build dist *.egg-info roger/__pycache__ tests/__pycache__ .pytest_cache
