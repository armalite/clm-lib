# CLAUDE.md

Recurring instructions for coding assistants working in this repository.

## Before working

- Read `SPEC.md`. It is the source of requirements and acceptance criteria. Record substantive departures in `docs/architecture.md` (implementation notes) instead of silently changing the objective.
- Preserve existing files and follow any further repository instructions.

## Never

- Never commit, push, tag, publish a package, or publish any article/blog post. The maintainer reviews and does these manually.
- Never print, log, copy or commit credential values. Check only whether a credential source exists. Do not dump the environment, open credential caches, or read other repositories for credentials. A coding-assistant subscription login is not an API key.
- Never run model-generated code outside the isolated executor, and never add a silent unrestricted fallback.
- Never invent measurements, rerun until a result looks positive, or report live/helper levels from scripted doubles.

## Development

- Environment: `uv sync` (Python 3.11+).
- Verify every change with:
  - `uv run ruff check . && uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run pytest` (Docker-backed tests need a running Docker daemon and the sandbox image; see README)
- Live API calls cost money. Use only bounded commands that go through the persistent budget ledger (`clm-lib budget` shows it). Never raise the configured ceiling automatically.
- Run artefacts go under `runs/` (gitignored). Results that are reported go in `docs/results.md`, with run IDs and cost provenance, including failed, exhausted or missing runs.
- Keep scope small: no multi-agent or delegation infrastructure, web UI, database or plugin registry.
