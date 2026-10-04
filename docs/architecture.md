# Architecture and implementation notes

This document describes how clm-lib implements [SPEC.md](../SPEC.md), the design choices made, and every known departure from the spec or the reference harness.

Reference consulted: `facebookresearch/context-language-models` harness README at commit `18dc11115f50f261233c5bba7937834491e307e8` (`clm/clm_harness/README.md`), read on 2026-10-04. No code from that repository was read or copied. Only the README's description of concepts was used: the mirror file, the protected prefix, the edit gate and nudge thresholds.

## Components

| Spec component | Module | Notes |
| --- | --- | --- |
| Model | (external) | Decides edits, writes code and helpers. |
| Runtime | `runner.py`, `context.py`, `baseline.py`, `budget.py`, `tracing.py` | Builds every request fresh; enforces limits. |
| Provider adapter | `provider.py` | `AnthropicProvider` (real) and `ScriptedProvider` (test double). |
| Executor | `executor.py` | `DockerExecutor` only. |
| Evaluator | `tasks.py` (`score_answer`) | Truth is generated host-side and written only after the run. |

## Request format

Each provider call is a single stateless Messages request, built fresh every time:

- `system`: the protocol, plus mode-specific instructions (`prompts.py`). These are identical across steps within a run.
- one `user` message with three sections:
  - `<task>`: the fixed task.
  - `<runtime_status>`: step, calls used, the request-size estimate against the budget, and pressure/over-limit notices.
  - `<working_context>`: the entries, one JSON object per line. `<` is escaped as `<`, so entry text can't forge section tags.
- `output_config.format` with a JSON schema for the action, and `output_config.effort`. There are no native tool calls, so editing history can't break tool-use/tool-result pairing. No assistant turns are replayed, so thinking-block replay rules don't apply.

Actions:

```json
{"action": "execute", "thought": "...", "code": "<python>"}
{"action": "final", "answer": {"root_cause": "CODE: ...", "required_value": "...", "remedy": "CODE: ...", "evidence_refs": ["path:line"]}}
```

Responses are parsed with `json.loads` only. A whole response wrapped in a single code fence is tolerated; any other prose is rejected. An invalid reply gets exactly one repair call, and a second failure ends the run as `failed_protocol`.

## Context file and revisions

`workspace/context.json` has the shape `{"format": "clm-context/v1", "entries": [{"id", "role", "body"}]}`. Roles are limited to `assistant`, `observation`, `note`, `summary` and `receipt`. `system`, `user`, `task` and anything else are rejected.

The limits are: 256 KB file, 200 entries, 24,000 chars per body, 120,000 chars in total. IDs are unique and match `[A-Za-z0-9][A-Za-z0-9_.:~-]{0,63}`.

Revision number, parent, source (`runtime | model_edit | summary | spill`), step and SHA-256 are kept host-side in `ContextStore`, never in the file. Every accepted revision is saved to `context/rev-NNNN.json` together with a unified diff.

Read-back after each execution:
- The host uses `lstat` and rejects symlinks, non-regular files, missing files, parents outside the workspace and oversize files. It then opens with `O_NOFOLLOW` and validates the JSON.
- Byte-identical to the mirror → `not_written`.
- Same canonical content → `unchanged_write`. This counts as an edit attempt, not a content change.
- Invalid → `rejected`: the previous revision is kept and the rejected bytes are saved to the trace.
- Otherwise → `accepted`, committed atomically as a new revision.

**Ordering rule.** The file holds the context up to the previous step. An accepted edit replaces that whole state. Then the runtime appends this step's `sN.act` (thought plus code), `sN.obs` (bounded output) and, if the file was written and changed or was rejected, `sN.rcpt`. So the latest command and result are always in the next request, and the model can drop them in a later step. Receipts list only IDs and counts, never removed content. If an ID collides with a model-chosen one, the runtime adds a `~N` suffix. Tests: `test_rewrite_retain_and_reorganise_matches_outgoing_payload` and `test_marker_removed_from_next_request_but_kept_in_event_log`.

## Pressure, overflow and recovery

These limits are shared by both arms:
- `B` = `context_budget_tokens` (8,000), measured over the **whole request**.
- `pressure_ratio` = 0.70.
- `recovery_reserve_tokens` = 1,000. Normal requests must stay at or below `B − 1000`.

Token counts are estimated as chars ÷ chars-per-token. The ratio starts at 3.0 and is recalibrated after each real action call from the provider-reported input tokens (exponential average). Both the estimate and the provider figures are recorded.

1. **Pressure.** If the estimate is ≥ 70% of `B`:
   - Summary arm: summarise the entries older than the 4-entry tail, if any of them aren't already summaries.
   - CLM arm: the status gets a PRESSURE notice. No edit is forced.
2. **Spill (both arms).** If the estimate is still above `B − 1000` and the newest entry is an observation of ≥ 1,500 chars, its body moves to `workspace/spill/<id>.txt`. A one-line pointer replaces it. This is the only runtime-initiated content move, and it's identical in both arms.
3. **Recovery.**
   - Summary arm: a forced summary.
   - CLM arm: one request may use the reserve (up to `B`) with an OVER LIMIT notice. If the next request is still over, the run ends as `context_overflow`.
4. **Summary sizing.** A summary that would still exceed the limit is retried once with half the character target. If it's still too large, the run ends with an explicit `context_overflow`. Nothing is truncated.

## Execution isolation

`DockerExecutor.run` starts a fresh `docker run --rm -i` per execution and pipes the code to `timeout --kill-after=2 30 python -E -s -`. The container settings are:
- `--network none`, uid:gid of the invoking user (non-root), `--cap-drop ALL`, `no-new-privileges`;
- `--memory 512m` (no swap), `--cpus 1`, `--pids-limit 128`;
- `--read-only` with a 64 MB `/tmp` tmpfs and `HOME=/tmp`;
- a read-only bind of `fixtures/` and a read-write bind of `workspace/`.

The docker CLI process itself gets a scrubbed environment (PATH, HOME, DOCKER_*), and the container receives only explicit `--env` values. Output is read by bounded reader threads: the first 6,000 bytes per stream are kept and the total is counted. A host-side backstop kills the container after the time limit plus 20 s.

Image: `python:3.12-slim`, digest `sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016` at build time.

Boundary checks: `test_sandbox_cannot_see_secret_env_or_evaluator_paths` and `clm-lib doctor --sandbox`. These check the configured boundary; they are not a proof of container security.

## Accounting

- `Ledger` (`runs/ledger.json`) persists across invocations. Its ceiling is fixed at creation and can only be lowered. `--max-usd` caps one command's spend.
- Before every attempt, including actions, repairs, summaries, API retries and smoke calls, the runtime reserves a worst-case cost: request JSON chars ÷ 2 as input tokens, at the higher of the input and cache-write prices, plus `max_tokens` at the output price. If the reservation doesn't fit, nothing is sent and the run ends as `budget_exhausted`.
- Settlement:
  - provider-reported usage × dated price;
  - `0` for errors returned before generation (4xx, 429, 5xx);
  - the **full reservation** for unknown outcomes (timeouts, connection drops), flagged `reservation_assumed`.
  - A refusal is settled from its returned usage.
- SDK-internal retries are disabled (`max_retries=0`), so every attempt is visible. Runtime retries (2, with backoff) count toward the 20-call cap.
- Usage recorded: uncached input, cache-read, cache-creation, output and `thinking_tokens` (reported by the SDK as part of output; not added again). Locally estimated input tokens are recorded separately and labelled.

## Task family and scoring

`tasks.py` defines one generator (`incident-gen/1`) with four scenarios:

| Instance | Seed | Scenario |
| --- | --- | --- |
| `dev` | 101 | DB pool limit lowered by a deploy override |
| `heldout-1` | 201 | Rollback re-served an expired mTLS certificate |
| `heldout-2` | 202 | Hotfix lowered a client timeout below upstream p99 |
| `heldout-3` | 203 | A flag rollout enabled a faulty serializer |

Each instance has:
- about 250K chars of logs, config, change logs, metrics and notes, far more than an 8K-token budget;
- a stale observation (config default, on-call note, inventory or registry snapshot) superseded by a later `APPLIED` record;
- `PROPOSED` (not applied) distractors and warnings from other categories;
- heavily repeated heartbeat and access lines.

No line exceeds about 220 chars, so every required observation fits the output cap.

Scoring is deterministic:
- Cause and remedy are taken from the first code mentioned.
- The value is whitespace, quote and case normalised against the accepted variants. Matching a stale variant gives `stale_value`.
- Evidence references must parse as `path:line` or `path:a-b` (at most 20 lines), exist, and cover both the cause group and the value group.
- Strict success requires all four components. Outcomes are `correct`, `stale_value`, `unsupported`, `incorrect` and `no_answer`.

Ground truth exists only in memory until the run ends, then goes to `evaluator/`, which is never mounted.

## Design choices

- **JSON-lines transcript with escaping** instead of nonce delimiters: it's simple and can't be forged.
- **The budget covers the whole request**, which is what the provider actually sees. CLM's system text is about 1.1K chars (~350 tokens) longer than the baseline's (2,685 vs 1,622 chars), so the CLM arm has slightly less transcript room under the same budget. This is recorded, not compensated.
- **A fresh container per execution.** Only the workspace persists, so helpers persist as files and must be imported (cwd is on `sys.path` because `-E -s` is used rather than `-I`).
- **Effort `low` by default** (`claude-opus-5-5` can't disable thinking). This keeps thinking and the JSON within the 2,048-token output cap and keeps cost bounded. It's identical for both arms and recorded in `run.json`.
- **No prompt caching by default.** The protected prefix is below useful cache sizes, and cache fields are still accounted if present.
- **The scenario name is hidden from the model.** The full cause/remedy code list is shown in every instance.

## Departures from the spec

1. **Runtime spill** (§3, step 6) is an extra runtime-initiated content move. It is used only when a request would exceed the hard limit, and identically in both arms. The original text stays in the event log and in a workspace file the model can read.
2. **Code-prefixed answers.** The answer fields keep the spec's names, but `root_cause` and `remedy` must start with a code from a published list. This makes scoring deterministic without a judge model.
3. **No live validation** was performed in the build session: provider credentials were unavailable (see results.md). The live and helper completion levels are therefore not claimed.
4. **Model choice.** `claude-opus-5-5` follows current Anthropic guidance. It's configurable, and nothing was run on it.
