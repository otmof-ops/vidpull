PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
VERSION ?=

.PHONY: dev test lint format check build install uninstall clean release-prepare release-tag

dev:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[dev]"
	-$(BIN)/pip install -q "yt-dlp[deno]"

test:
	$(BIN)/python -m pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

check: lint test

build:
	scripts/build.sh

install:
	./install.sh --from .

uninstall:
	./install.sh --uninstall

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache

release-prepare:
	scripts/release.sh prepare $(VERSION)

release-tag:
	scripts/release.sh tag $(VERSION)
