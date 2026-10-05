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

**Summary policies.** The behaviour above is the default `fixed-tail/1`. `token-tail/1` (experiment 003) instead keeps a token-bounded recent tail (the newest entry is kept up to a larger cap), summarises oversized recent entries, and sizes summaries to land near 50% of the budget. Its first attempt must relieve pressure, and its retry must fit the hard limit. See SPEC §14 and `baseline.py`.

## Execution isolation

`DockerExecutor.run` starts a fresh `docker run --rm -i` per execution and runs `timeout --kill-after=2 30 python -E -s -B -c BOOTSTRAP <nonce>`. The model code is piped to it on stdin. The container settings are:
- `--network none`, uid:gid of the invoking user (non-root), `--cap-drop ALL`, `no-new-privileges`;
- `--memory 512m` (no swap), `--cpus 1`, `--pids-limit 128`;
- `--read-only` with a 64 MB `/tmp` tmpfs and `HOME=/tmp`;
- a read-only bind of `fixtures/` and a read-write bind of `workspace/`.

The docker CLI process itself gets a scrubbed environment (PATH, HOME, DOCKER_*), and the container receives only explicit `--env` values. Output is read by bounded reader threads: the first 6,000 bytes per stream are kept and the total is counted. A host-side backstop kills the container after the time limit plus 20 s.

Image: `python:3.12-slim`, digest `sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016` at build time.

Boundary checks: `test_sandbox_cannot_see_secret_env_or_evaluator_paths` and `clm-lib doctor --sandbox`. These check the configured boundary; they are not a proof of container security.

**Execution instrumentation (helper evidence).** `BOOTSTRAP` reads the code from stdin, installs a Python audit hook and a `sys.monitoring` (Python 3.12) `PY_START` callback, then `exec`s the code as `__main__` (filename `<model-code>`). The record covers:
- workspace files opened, compiled or imported, and subprocess spawns;
- how many times each code object defined in a workspace file began executing (function or module body);
- for every open-for-write or replace of `context.json`, which workspace code frames were on the call stack.

An `atexit` handler writes the record to stderr behind a per-execution random nonce. The host strips that line before building the observation and adjusts byte counts. This is tamper-evident, not tamper-proof: the nonce is visible in the container's `/proc`, `os._exit` or a kill produces no record (`runtime_trace` is `None`), and writes made via low-level `os.open` are not attributed. The Docker flags and mounts are unchanged.

Helper evidence, per step in `summary.json` under `helpers.uses[].files`:
- `candidate`: the code text names a `.py` file that existed before the step. Inferred, weak.
- `loaded`: the file was imported or compiled. Recorded, but **not** counted as execution.
- `module_body_executed`: the module's top-level code ran (for example on import). Not counted as helper-function execution.
- `functions_executed`: functions defined in the file began executing, with counts.
- `wrote_context_from`: helper functions that were on the call stack when `context.json` was written.

Summary metrics:
- `function_execution_steps`: helper functions ran in a step that exited 0.
- `helper_written_accepted_edit_steps`: additionally, the context write came from helper code and that step's edit was accepted. This attributes **the write** to the helper; it does not prove the helper alone decided the content (the caller's arguments can shape it). It is recorded as such.
- "Reuse" is claimed only when `helper_written_accepted_edits_in_2plus_steps` is true.

Tests: `test_helper_use_is_verified_not_just_inferred`, `test_compile_or_import_alone_is_not_helper_execution`.

## Accounting

- `Ledger` (`runs/ledger.json`) persists across invocations. Its ceiling is fixed at creation and can only be lowered. `--max-usd` caps one command's spend.
- Before every attempt, including actions, repairs, summaries, API retries and smoke calls, the runtime reserves a cost bound. Input tokens are bounded by `input_token_bound(payload)`: the UTF-8 bytes of the system text, message content and output-config JSON, plus 2,048 tokens for framing and any hidden structured-output scaffolding. They are priced at the higher of the input and cache-write rates. `max_tokens` is priced at the output rate.
  - The input bound is an **assumption-based bound, not a provider guarantee**: it assumes the tokenizer emits at most one token per UTF-8 byte.
  - It is **checked on every live response**. If reported input tokens exceed the bound, or output exceeds `max_tokens`, the run stops as `accounting_bound_violated` (invalid for comparison).
  - `smoke` additionally compares the bound with the free `count_tokens` endpoint.
  - If the reservation doesn't fit, nothing is sent and the run ends as `budget_exhausted`.
- **Pending reservations are persisted before dispatch** (`pending` in the ledger file).
  - If the process is interrupted in-process (for example Ctrl-C), the attempt is settled as `interrupted:*` at the full reservation.
  - If the process is killed, the next `Ledger.open` converts leftover pending entries to `unresolved_after_restart` at the full reservation.
  - Opening is refused (`LedgerBusy`) while another live pid holds pending reservations. This is a guard, **not a concurrency lock**: live commands must be run sequentially.
  - `compare` halts on `accounting_bound_violated` or `budget_exhausted`. No further calls are dispatched, the remaining cells are recorded as `missing` with the reason, and on a bound violation the command exits with code 4 (`test_compare_halts_on_accounting_bound_violation`).
  - Tests: `tests/test_budget.py`, including a subprocess killed after reserving.
- Settlement:
  - provider-reported usage × dated price;
  - `0` only for failures that are provably unbilled: 4xx, 429, and credential-chain failures before sending;
  - the **full reservation** for potentially billed failures: 5xx, timeouts, connection drops, unexpected errors and interruptions. These are flagged `reservation_assumed` and reported separately as unresolved assumed charges.
  - A refusal is settled from its returned usage.
- SDK-internal retries are disabled (`max_retries=0`), so every attempt is visible. Runtime retries (2, with backoff) count toward the 20-call cap.
- Usage recorded: uncached input, cache-read, cache-creation, output and `thinking_tokens` (reported by the SDK as part of output; not added again). Locally estimated input tokens are recorded separately and labelled.

## Edit evidence (report)

`report.edit_evidence` checks every accepted content-changing edit against the very next action request's saved payload. It applies three separate checks:
- **Structural:** removed IDs are absent and added IDs present.
- **Content (exact):** the request's working context begins with exactly the accepted revision's entries (id, role and body), followed by the runtime-appended entries for that step.
  - A valid empty revision is a trivially matching prefix.
  - A missing revision artifact is reported as `unverifiable`.
  - Rewritten entries must carry their new body.
- **Removed text (partial, heuristic):** every non-framing line of at least 8 characters from each removed entry is searched for in every entry of the next request.
  - Hits on lines of 40 or more characters are "strong"; shorter hits are "weak", possibly coincidental.
  - Runtime framing lines (exit status, stream headers, `thought:`, `code:`) and lines under 8 characters are not checked. The report gives the checked share of removed characters (`coverage`) and never claims that all removed text is absent.
  - If the pre-edit revision artifact is missing, the check is reported as unavailable.

Tests: `test_edit_evidence_checks_content_and_reappearance`, `test_short_removed_text_reappearing_is_reported`, `test_valid_empty_accepted_revision_passes_prefix_check` and `test_missing_revision_artifact_is_unverifiable`.

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
- The value is whitespace, quote and case normalised against the accepted variants. Matching a stale variant gives `stale_value`. From score/2 on, a `<identifier>=<value>` answer is also matched on its value part.
- Evidence references must parse as `path:line` or `path:a-b` (at most 20 lines), exist, and cover both the cause group and the value group.
- Strict success requires all four components. Outcomes are `correct`, `stale_value`, `unsupported`, `incorrect` and `no_answer`.

Ground truth exists only in memory until the run ends, then goes to `evaluator/`, which is never mounted.

## Staged tasks (experiment 002)

- **Generator:** `staged.py` (`staged-incident-gen/1`) builds three stage file sets under `stage-1/`, `stage-2/` and `stage-3/`, plus one update message per stage.
- **Release:** `TaskInstance.stages` holds the stage files in host memory. The runner writes stage 1 at start. Each `advance` action writes the next stage into the read-only fixture mount and appends a pair of entries: `sN.act` "advance" and `sN.obs` with the update message and the new-file listing.
  - Released files never change. Earlier stages stay readable, and both arms re-read through the same sandbox.
  - Future stages are not on disk until released. Evaluator truth is written only after the run.
  - Test: `test_future_stages_and_truth_are_not_visible_early`.
- **Schema:** the `advance` action exists only in `STAGED_ACTION_SCHEMA`, used for staged tasks. Single-stage tasks keep the original schema and reject `advance`, so experiment 001's requests stay reproducible. Staged instructions are in the task text, not the system prompt.
- **Premature final:** a `final` before the last stage produces a neutral observation ("k of 3 stages released"), with no answer feedback. It counts toward the call cap.
- **Management timeline:** `summary.json → management` records:
  - per-step stage, estimated and reported request size, and pressure before management;
  - the first pressure step and stage;
  - the steps at which accepted edits took effect (the next request), plus summary, spill and recovery steps;
  - the first management step and stage;
  - the action steps, executions and advances that followed it.
- **Scoring additions:** an `origin` evidence group (the stage-1 release-note line), `stale_causes` (the superseded timeout hypothesis gives outcome `stale_hypothesis`), and two stale values (the build default and the repo config default).

## Coding tasks, second generator (experiment 005)

- **Modules:** `coding2.py` (`invoice-gen/2`) uses `invoice_ref2.py` for expected results. `invoice_ref2.py` is standalone (standard library only), so tests can install it verbatim in the sandbox as a known or deliberately faulty submission. Fault flags exist only for evaluator validation.
- **Check selection** is predicate-based:
  - module checks must differ from the result with that module forgotten (`retained`, `final_stage`) or with its old version (`replaced`);
  - `kept_part` checks must match the old version but differ from an over-applied partial change;
  - `interaction` checks must depend on at least three rules.
  - Input kinds per module (for example, bulk qty at and one below the threshold, a discount crossing the shipping threshold, refunds crossing thresholds or going negative) are cycled, so boundary kinds always appear.
- **Stale variants:** each check records the result under the old version of every changed rule whose old version differs, and a failed output matching one is reported as stale for that rule.
- **Snapshots:** `advance_step` copies the workspace to `evaluator/snapshots/stage-k` before releasing stage k+1. The evaluator directory is never mounted. `evaluate_coding` runs the same sandboxed evaluator on each snapshot with that stage's hidden checks.
- **Scorer version per task:** `TaskInstance.scorer_version` and `tasks.scorer_version_for()` give the scorer per task. They are used for `evaluator/score.json`, the comparison `frozen` block, and (through the metrics rows) the export manifest.
- **Tests:**
  - `tests/test_coding2.py`, with `tests/coding2_solutions.py` holding the independent implementation and the faulty submissions;
  - `test_manifest_reports_the_scorer_of_the_exported_tasks` in `tests/test_export.py`;
  - `test_balanced_order_alternates_within_each_instance` in `tests/test_budget.py`.

## Request layouts and prompt caching (experiment 006)

- **Rendering:**
  - `prompts.render_user_blocks` and `summary_user_blocks` return text blocks and breakpoint candidates.
  - `ModelRequest.from_blocks` keeps the joined `system`/`user` strings, used for estimates, logs and tests, next to the blocks.
  - `provider.message_body` renders either layout; it is shared by the real and scripted providers, so tests see the exact payload shape.
- **Block content:** the working context is split into one block per entry because cache hits happen only at block boundaries. A growing single block could never be read back, and a step appends 2–3 entries, well inside the provider's 20-block lookback.
- **Breakpoints:** explicit, not automatic. Automatic caching would mark the per-call status block, a unique tail.
- **Per-run settings:** `Runner.run(..., caching=)` overrides the config for one run (the four-condition matrix), and the run id carries `-cacheon-` / `-cacheoff-` under `blocks/1`.
- **The run tag** is the first system block, so every cache entry key starts with it.
- **Evidence:**
  - `_Run.prefix_evidence` compares each request's blocks with the previous request of the same family;
  - `call()` records provider latency and per-kind usage;
  - `summary.json → request` collects them.
- **Cost:** `ModelPrice.cost` prices 1-hour writes separately when the provider reports the TTL breakdown.
- **Tests:** `tests/test_caching.py` uses a scripted provider that simulates prefix caching with a 20-block lookback, which tests the accounting plumbing, plus a Docker test of edits under `blocks/1`.

## Design choices

- **JSON-lines transcript with escaping** instead of nonce delimiters: it's simple and can't be forged.
- **The budget covers the whole request**, which is what the provider actually sees. CLM's system text is about 1.1K chars (~350 tokens) longer than the baseline's (2,685 vs 1,622 chars), so the CLM arm has slightly less transcript room under the same budget. This is recorded, not compensated.
- **A fresh container per execution.** Only the workspace persists, so helpers persist as files and must be imported (cwd is on `sys.path` because `-E -s` is used rather than `-I`).
- **Effort `low` by default** (`claude-opus-5-5` can't disable thinking). This keeps thinking and the JSON within the 2,048-token output cap and keeps cost bounded. It's identical for both arms and recorded in `run.json`.
- **No prompt caching by default.** Earlier configs send no `cache_control`; experiment 006 adds it as an explicit, versioned option (see above). Cache fields are always accounted when present.
- **The scenario name is hidden from the model.** The full cause/remedy code list is shown in every instance.

## Departures from the spec

1. **Runtime spill** (§3, step 6) is an extra runtime-initiated content move. It is used only when a request would exceed the hard limit, and identically in both arms. The original text stays in the event log and in a workspace file the model can read.
2. **Code-prefixed answers.** The answer fields keep the spec's names, but `root_cause` and `remedy` must start with a code from a published list. This makes scoring deterministic without a judge model.
3. **Live validation** ran after the build session, once a credential file was provided (results.md). Added beyond the planned sequence: one **dev-only calibration run** in summary mode, to check SPEC §6's pressure condition before freezing. It showed pressure, so no adjustment was made.
4. **Model choice.** `claude-opus-5-5` (effort `low`) follows current Anthropic guidance. It's configurable, and it was used for every live run.
5. **Post-hoc scorer correction (score/2).** After the comparison, `required_value` also accepts `<identifier>=<value>`, the form used in the authoritative change records. score/1 results are preserved in each run, and `report` shows both. This was prompted by one held-out answer and is disclosed in results.md.
