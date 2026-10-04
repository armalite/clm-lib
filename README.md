# clm-lib

A Python library for agents that edit their own working context and write their own context-management code, inspired by Context Language Models (CLMs).

Status: experimental (v0.1). Requirements: [SPEC.md](SPEC.md). Design and departures: [docs/architecture.md](docs/architecture.md). Measured results: [docs/results.md](docs/results.md) and the results repository, [armalite/clm-lib-test-results](https://github.com/armalite/clm-lib-test-results).

## How it works

1. **The working context is a file.** Each step, the runtime writes the model's current working transcript to `context.json` in a sandboxed workspace. Each entry has an id, a role and text.
2. **The model can edit it with its own code.** The model replies with one JSON action. An `execute` action carries arbitrary Python, written by the model, which runs in a Docker sandbox. That code may investigate files, rewrite `context.json` however it likes (delete, shorten, merge, reorder, add notes), and create optional reusable helper modules. Helpers are allowed, not required.
3. **Accepted edits define the next request.** After each execution the runtime validates the file: valid JSON, allowed roles, unique ids, size limits, no symlinks. If it is valid, its entries become the editable part of the next request. If not, the previous version is kept and the model gets a short receipt.
4. **Without an edit, context accumulates normally.** Each step's action and output are appended.
5. **The system instructions and the task are protected.** The runtime renders them outside the file on every request, so edits cannot change them.
6. **Complete history is kept separately.** The trace keeps every request exactly as sent, every response, every accepted revision with diffs, the model's code (saved before it ran), and helper files before and after each step. Edits change what the model sees next, never the record.

The comparison baseline, the **summary** arm, uses the same model, tools and limits but cannot edit its context. When a request reaches 70% of the budget, older entries are replaced by a model-written summary and the newest 4 entries are kept.

## Three kinds of evidence

| Kind | What it establishes | How |
| --- | --- | --- |
| **Pytests** | The implementation is correct: next-request replacement, the protected task, rejection of invalid edits, the sandbox boundary, accounting, staged evidence visibility, scoring, export. | `uv run pytest`. A scripted model drives the real sandbox; no API calls. |
| **Guided demonstration** | A real model *can* edit its context and build and reuse a helper when **explicitly asked to**. | `clm-lib guided`. Prompted, so it says nothing about spontaneous behaviour or benefit. |
| **Comparative evaluation** | Whether enabling CLM editing improves task outcomes or efficiency against the summary baseline. | `clm-lib compare` on frozen settings and evaluation instances never used for tuning. |

## Tasks and scoring

The tasks are synthetic production-incident investigations over local logs, configs, change records, metrics and notes, with deterministic ground truth that is never visible to the agent.
- **Experiment 001** used single-stage incidents (`dev`, `heldout-1..3`).
- **Experiment 002** uses staged incidents (`staged-dev-*` for calibration, `staged-eval-*` for evaluation). Evidence arrives in three stages, released when the agent sends an `advance` action. Later stages supersede earlier values and an earlier hypothesis, so useful facts have to survive while the investigation continues.

The answer has four fields: `root_cause`, `required_value`, `remedy` and `evidence_refs`. The scorer checks:
- the cause code and the remedy code;
- the exact current value. The superseded "stale" values are flagged as `stale_value`, and a superseded cause as `stale_hypothesis`.
- that the `path:line` references exist and cover every required evidence group: the symptom and the current-value record; staged tasks also need the origin record from stage 1.

**Strict success** requires all of them. The scorer does **not** judge explanation prose, and it does not check that the retained context was true; it only checks the final answer against ground truth. scorer `score/2` also accepts answers written as `setting=value` (a disclosed post-hoc correction in experiment 001).

## Quickstart (offline, no API calls)

Prerequisites: Python 3.11+ with [uv](https://docs.astral.sh/uv/), and a running Docker daemon.

```bash
uv sync
docker pull python:3.12-slim
uv run clm-lib doctor --sandbox
uv run pytest
uv run clm-lib demo-offline
```

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

One provider is implemented: the Anthropic Messages API, through the official `anthropic` SDK. Credentials are resolved by the SDK's own chain (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile). clm-lib never reads, prints or stores credential values; `doctor` reports only whether a source exists. `doctor --probe` makes one free models-endpoint call to verify access and the model ID.

Model, effort and limits are in [configs/default.toml](configs/default.toml), and dated prices in [configs/prices.toml](configs/prices.toml).

## Commands

Live commands are paid. Each call goes through a persistent ledger (`runs/ledger.json`, $10 ceiling by default) that reserves a conservative cost bound before dispatch. Run live commands **one at a time**.

| Command | Purpose |
| --- | --- |
| `clm-lib doctor [--probe] [--sandbox]` | Environment, credential presence, sandbox self-test |
| `clm-lib demo-offline` | Scripted-model mechanism demo through the real sandbox |
| `clm-lib smoke` | One minimal live call with structured-output and accounting checks |
| `clm-lib guided --task dev` | Prompted helper demonstration (not comparative) |
| `clm-lib run --mode summary\|clm\|guided --task NAME` | One live run (used for calibration) |
| `clm-lib compare --reps 2 [--tasks a,b,c]` | Frozen comparison: tasks × 2 modes × reps, mode order alternating per pair |
| `clm-lib report --out runs/report.md` | Markdown report from recorded evidence |
| `clm-lib export experiments/<id> --dest ../clm-lib-test-results` | Copy an experiment's evidence to the results repo |
| `clm-lib budget` | Show the ledger |

## Results so far

Details in [docs/results.md](docs/results.md); exported evidence in [clm-lib-test-results](https://github.com/armalite/clm-lib-test-results).

- **001: short incident pilot.** Live, 12 comparison runs plus a guided demo. The guided run showed real context replacement and prompted helper use. In the comparison the model solved every task in 3–4 calls: 0 CLM edits and 0 summaries, so context management was **not exercised**. Both arms were 6/6 strictly correct after a disclosed scorer correction; CLM cost about 23% more per run.
- **002: staged incident under context pressure.** Evidence released in 3 stages. Both arms managed context in every run: CLM made 2–6 unprompted edits per run, and the baseline made 2–5 summaries.
  - CLM was 6/6 strictly correct versus the baseline's 4/6. The two baseline failures were an explicit overflow caused by its verbatim tail, and an over-long evidence range.
  - CLM cost about 38% less per run and was cheaper in every matched pair. The saving is explained by the baseline's repeated summary calls.
  - This is an exploratory pilot, n = 6 per arm: no significance claims, and one task family.

## Sandbox prerequisites

Generated code runs only via `docker run --rm` with:
- `--network none` and a non-root uid;
- `--cap-drop ALL` and `no-new-privileges`;
- memory, CPU and pid limits, and a read-only root filesystem with a `/tmp` tmpfs;
- a 30 s in-container timeout.

Fixtures are mounted read-only and the workspace read-write. The host environment, the Docker socket, credentials, traces and ground truth are never mounted. There is **no fallback** to unsandboxed execution.

## Layout

```text
src/clm_lib/
  context.py    entry schema, host-side revisions, validation, read-back
  runner.py     agent loop, modes, pressure/overflow handling, staged evidence, accounting
  provider.py   Anthropic adapter (stateless, SDK retries off) + scripted double
  executor.py   Docker sandbox + in-container execution evidence
  baseline.py   summary policy
  budget.py     dated prices, reservations, persistent ledger
  tracing.py    append-only events, saved requests, scripts, diffs, file snapshots
  tasks.py      single-stage incident fixtures + scorer
  staged.py     staged incident fixtures (experiment 002)
  prompts.py    frozen prompt text and request rendering
  report.py     markdown report from evidence
  export.py     results-repository export
  cli.py
experiments/<id>/   experiment definitions (run roles, provenance) and hand-written notes
runs/               raw run artifacts and the active ledger (gitignored)
```

## Limitations

- Synthetic task families from deterministic generators; small pilots (2 repetitions per cell), with no statistical claims.
- The context budget applies to the whole request, using a calibrated chars-per-token estimate. Provider-reported tokens are recorded separately.
- Structural validation can't establish that retained claims are true.
- Edits and helpers live within one run; there is no cross-run skill learning and no training.
- Not a reproduction of the CLM paper's benchmark or harness. Original code; the reference implementation (CC BY-NC 4.0) was not copied.

## References

- Paper: https://arxiv.org/html/2609.37725v1
- Reference harness docs (pinned): https://github.com/facebookresearch/context-language-models/blob/18dc11115f50f261233c5bba7937834491e307e8/clm/clm_harness/README.md

License: Apache-2.0.
