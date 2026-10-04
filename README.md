# clm-lib

A Python library for agents that edit their own working context and write their own context-management code, inspired by Context Language Models (CLMs).

The runtime shows the model its working transcript as an editable file (`context.json`). The model replies with Python. That Python runs in a Docker sandbox and may rewrite the file. The runtime validates the result and builds the **next request from the accepted file**. A summary-based baseline, a synthetic incident-investigation task family and a deterministic scorer support a small comparison.

Status: experimental (v0.1). See [SPEC.md](SPEC.md) for requirements, [docs/architecture.md](docs/architecture.md) for design and departures, and [docs/results.md](docs/results.md) for what has actually been measured.

## Quickstart (offline, no API calls)

Prerequisites: Python 3.11+ with [uv](https://docs.astral.sh/uv/), and a running Docker daemon.

```bash
uv sync
docker pull python:3.12-slim
uv run clm-lib doctor --sandbox
uv run pytest
uv run clm-lib demo-offline
```

`demo-offline` drives a **scripted model double** through the real sandbox and read-back path. A bulky observation is removed by model-side code, and the next request no longer contains it. It shows the mechanism only, not model behaviour.

Library use:

```python
from pathlib import Path
from clm_lib import Config, DockerExecutor, Ledger, Runner, ScriptedProvider, generate

provider = ScriptedProvider(
    [
        {
            "action": "final",
            "answer": {
                "root_cause": "DB_POOL_EXHAUSTED: ...",
                "required_value": "12",
                "remedy": "RAISE_DB_POOL_LIMIT: ...",
                "evidence_refs": [],
            },
        }
    ]
)
runner = Runner(
    Config(),
    provider,
    DockerExecutor(),
    Ledger.open(Path("runs/l.json"), 0.0),
    price=None,
    live=False,
    runs_dir=Path("runs"),
)
result = runner.run(generate("dev"), mode="clm")
print(result.status, result.score.outcome, result.run_dir)
```

## Model access

One provider is implemented: the Anthropic Messages API, through the official `anthropic` SDK. Credentials are resolved by the SDK's own chain: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile. clm-lib never reads, prints or stores credential values; `doctor` reports only whether a source exists.

```bash
uv run clm-lib doctor --probe
```

`--probe` makes one free call to the models endpoint. It checks both access and that the configured model ID exists. If it fails, either export `ANTHROPIC_API_KEY` or run `ant auth login`.

Provider, model, endpoint (`base_url`), effort and limits live in [configs/default.toml](configs/default.toml). Dated prices are in [configs/prices.toml](configs/prices.toml). A model without a dated price is blocked from live use.

## Live commands (paid, bounded)

All live calls go through a persistent ledger (`runs/ledger.json`). Before each call, the runtime reserves a conservative upper bound on its cost and refuses to dispatch if that bound doesn't fit. The default total ceiling is **USD 10**. The ledger keeps the lowest ceiling ever configured, and `--max-usd` lowers it further for one command.

```bash
uv run clm-lib smoke
uv run clm-lib guided --task dev
uv run clm-lib run --mode clm --task dev
uv run clm-lib compare --reps 2
uv run clm-lib report --out runs/report.md
uv run clm-lib budget
```

The commands are:
- `smoke`: one minimal call.
- `guided`: the prompted helper demonstration.
- `run`: a single run, with `--mode summary|clm|guided`.
- `compare`: 3 held-out tasks × 2 modes × 2 repetitions, with mode order alternating per pair.
- `report`: builds markdown from the recorded evidence.
- `budget`: prints the ledger.

These commands are implemented and covered by offline tests of their parts. They have **not yet been exercised against the real API**: provider access was unavailable when this was built (see docs/results.md).

## Sandbox prerequisites

Generated code runs only via `docker run --rm` with:
- `--network none` and a non-root uid;
- `--cap-drop ALL` and `no-new-privileges`;
- memory, CPU and pid limits, and a read-only root filesystem with a `/tmp` tmpfs;
- a 30 s in-container `timeout`.

Fixtures are mounted read-only at `/task/fixtures`, and the per-run workspace read-write at `/task/workspace`. The host environment is not forwarded, and the Docker socket, credentials, traces and ground truth are never mounted. Output capture is capped per stream. There is **no fallback** to unsandboxed execution; without Docker, runs stop with `executor_unavailable`.

## Run modes

| Mode | What the model gets | In comparison? |
| --- | --- | --- |
| `summary` | Same protocol and tools; at 70% of the budget, older entries are replaced by a model-written summary and the newest 4 are kept. | yes |
| `clm` | `context.json` is authoritative; edits are optional, with pressure reminders at 70%. | yes |
| `guided` | `clm` plus an explicit request to build a helper, use it twice and revise it if useful. | no (prompted demo) |

## Architecture (short)

```text
src/clm_lib/
  context.py    entry schema, host-side revisions, validation, read-back
  runner.py     agent loop, modes, pressure/overflow handling, accounting
  provider.py   Anthropic adapter (stateless, SDK retries off) + scripted double
  executor.py   Docker sandbox
  baseline.py   summary policy
  budget.py     dated prices, reservations, persistent ledger
  tracing.py    append-only events, saved requests, scripts, diffs, file snapshots
  tasks.py      deterministic incident fixtures + scorer
  prompts.py    frozen prompt text (PROMPT_VERSION) and request rendering
  report.py     markdown report from evidence
  cli.py
```

Each run writes `runs/<run_id>/` with the following:
- `run.json`: configuration and identity.
- `events.jsonl`: chronological events, made read-only at close.
- `requests/`: exact payloads, without headers.
- `context/`: revisions and diffs.
- `scripts/`: model code, saved before it runs.
- `files/`: helper files before and after each step.
- `workspace/`, `fixtures/`: the sandbox mounts.
- `evaluator/`: truth and score, written after the run.
- `summary.json`: metrics.

## Limitations

- One synthetic task family from a single generator. The 4 instances (1 dev, 3 held-out) differ in scenario, entities, values and evidence layout, but share structure. This is a controlled stress test, not an estimate of production savings.
- The context budget is applied to the whole request using a calibrated chars-per-token estimate. Provider-reported tokens are recorded separately.
- Structural validation can't establish that retained claims are true.
- Edits and helpers live within one run; there is no cross-run skill learning and no training.
- Not a reproduction of the CLM paper's benchmark or harness. Original code; the reference implementation (CC BY-NC 4.0) was not copied.

## References

- Paper: https://arxiv.org/html/2609.37725v1
- Reference harness docs (pinned): https://github.com/facebookresearch/context-language-models/blob/18dc11115f50f261233c5bba7937834491e307e8/clm/clm_harness/README.md

License: Apache-2.0.
