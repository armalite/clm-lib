# Results overview

**Experiment write-ups, findings and evidence live in [clm-lib-test-results](https://github.com/armalite/clm-lib-test-results).** That repository owns the experiment READMEs, summaries, historical protocols, frozen provenance and exported run artifacts. This file keeps only a short overview, plus the library's own verification history.

## Experiments

| Experiment | One-line outcome | Write-up |
| --- | --- | --- |
| 001: short incident pilot | Tasks finished in 3–4 calls before either context-management method activated (0 edits, 0 summaries). A separate prompted run demonstrated real context replacement and helper use. | [001](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/001-short-incident-pilot) |
| 002: staged incident under context pressure | Both methods active in every run. CLM 6/6 strict successes vs the baseline's 4/6, about 38% lower mean cost, mostly from avoiding summary calls. This is a small synthetic pilot against one baseline. | [002](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/002-staged-incident-context-pressure) |
| 003: robust summary comparison | Against `token-tail/1`, a baseline that handles large recent entries and leaves room, both approaches got 6/6 strict successes. CLM was still about 25% cheaper and 28% faster, matching the baseline's spending on summary calls. | [003](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/003-robust-summary-comparison) |
| 004: coding with changing requirements | On an invoice-package task with requirements added and replaced over four stages, both approaches got 12/12 strict successes with no regressions or stale rules. CLM was about 25% cheaper, matching the baseline's spending on summary calls. | [004](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/004-coding-changing-requirements) |

The previous, longer version of this file is archived verbatim in the results repository as [`notes/clm-lib-results-log-2026-10-04.md`](https://github.com/armalite/clm-lib-test-results/blob/main/notes/clm-lib-results-log-2026-10-04.md). The draft blog notes moved there too, to [`notes/blog-notes.md`](https://github.com/armalite/clm-lib-test-results/blob/main/notes/blog-notes.md).

## Spend

The active ledger is `runs/ledger.json` in this repository (gitignored). Totals so far: **USD 16.214** (experiments 001–003 USD 8.917; experiment 004 USD 7.297), all from provider-reported usage, with no unresolved assumed charges. The ceiling was raised once, explicitly, for experiment 004: from USD 10 to USD 38.917 (recorded in the ledger's `ceiling_history`). It was never reset. Live commands for experiment 004 use `configs/exp004.toml`; a config with a lower ceiling lowers it again. Historical snapshots are exported to the results repository's `ledger-snapshots/`.

## Live accounting checks

These live checks confirmed the library's cost accounting:
- **`smoke`** (`20261004T065324-smoke`): all 7 checks passed.
  - Reported input was 485 tokens; the free `count_tokens` endpoint returned 66; the reservation bound was 2,798.
  - Structured output adds about 420 hidden input tokens, which the bound's 2,048-token overhead covers.
- **Reservation bound:** it held on every live response across both experiments. Reported input was always at or below the bound, and output at or below `max_tokens`.

## Offline verification (2026-10-04)

Commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Results: ruff and format clean; mypy reported no issues in 13 source files; **42 passed** in about 22 s. Docker 20.10.13 with `python:3.12-slim`.

Mapping to SPEC §8.1:

| # | Test(s) | Executor |
| --- | --- | --- |
| 1 | `test_marker_removed_from_next_request_but_kept_in_event_log` | Docker |
| 2 | `test_rewrite_retain_and_reorganise_matches_outgoing_payload` | Docker |
| 3 | `test_invalid_edits_keep_previous_state_with_bounded_receipt` (malformed, oversized, duplicate id); `test_context.py` | Docker |
| 4 | `test_protected_prefix_cannot_be_replaced`; role rejections in `test_context.py` | Docker |
| 5 | `test_sandbox_cannot_see_secret_env_or_evaluator_paths`; `doctor --sandbox` | Docker |
| 6 | `test_summary_replaces_older_entries_and_keeps_tail`, `test_summary_overflow_after_bounded_retry`, `test_clm_recovery_*`, `test_spill_*`, `test_max_calls_stop` | test double (no code runs) |
| 7 | `test_all_call_types_and_retries_are_accounted`, `test_rejected_edits_counted_in_clm`, `test_no_call_starts_after_budget_exhaustion`, `test_ledger_ceiling_never_increases`, `test_scripted_provider_cannot_be_used_live_*` | test double |
| 8 | `test_scorer_outcomes` (correct / unsupported / stale / incorrect / no answer), `test_reference_parsing`, `test_fresh_runs_do_not_inherit_helpers_or_context` | mixed |

Sandbox self-test (`clm-lib doctor --sandbox`): `dummy_secret_visible= False uid= 1000 network=blocked`.

Offline demo (`clm-lib demo-offline`, scripted model, run `20261004T030919-clm-dev-e069` under `runs/offline/`):

- request 1: marker in working context = False
- request 2: marker in working context = True
- request 3: marker in working context = False (removed by sandboxed code, then accepted)

## Review-fix session (base commit 39c02a8)

Phase 1 (offline fixes for the review of 39c02a8) is complete:

| Review item | Fix | Test |
| --- | --- | --- |
| Pending reservations existed only in memory | Persisted to the ledger before dispatch. In-process interruption is settled at the full reservation. Leftovers from a killed process are charged on the next open (`unresolved_after_restart`). A concurrent live pid blocks opening. | `test_pending_reservation_is_persisted_before_dispatch`, `test_killed_process_reservation_is_charged_on_restart` (real killed subprocess), `test_live_pending_from_another_process_blocks_open`, `test_interrupt_during_provider_call_charges_reservation` |
| The chars/2 heuristic was not a bound | UTF-8 byte bound + 2,048 overhead tokens, labelled as an assumption. Each live response is checked against it, and a violation stops the run as `accounting_bound_violated`. `smoke` cross-checks it with `count_tokens`. 5xx, unexpected errors and interruptions are no longer charged $0. | `test_bound_covers_payload_bytes`, `test_run_stops_when_reported_usage_exceeds_bound`, `test_billing_classification` |
| Edit evidence checked only IDs | Content check: the exact accepted entries are a prefix of the next request; rewritten bodies are verified; removed text is searched for in the next request. Structural and content verdicts are separate. | `test_edit_evidence_checks_content_and_reappearance` |
| Helper "invocations" were inferred from code text | An in-container audit hook records the workspace files actually imported or compiled. There are three evidence tiers (candidate / verified execution / verified with accepted edit), and reuse is claimed only on the last. | `test_helper_use_is_verified_not_just_inferred` |

Verification: `ruff check`, `ruff format --check` and `mypy src` are clean; `pytest`: **51 passed** in about 30 s. `doctor --sandbox` is unchanged: `dummy_secret_visible= False uid= 1000 network=blocked`.

Phases 2–4 (smoke, guided dev run, frozen comparison) were **not run**. `ANTHROPIC_API_KEY` is not present in the agent process's environment (`doctor`: `credential env ANTHROPIC_API_KEY: unset`). The process inherited its environment before the key was added. No secrets were searched for, and the old OAuth profile was not used. Live spend: $0.00.

## Second review-fix session (base commit 5f58d76)

| Review item | Fix | Test |
| --- | --- | --- |
| `compare` continued after `accounting_bound_violated` | `run_matrix` halts. No further dispatch; the remaining cells are `missing` with the reason; exit code 4. The comparison record now includes a `frozen` block (model, provider settings, prompt and generator versions, config, git HEAD and uncommitted-diff hash). | `test_compare_halts_on_accounting_bound_violation` |
| An empty accepted revision failed the prefix check, and the removed-text check overclaimed | Empty revisions pass; missing artifacts are `unverifiable`. The removed-text check covers every non-framing line of 8 or more characters, splits hits into strong (≥ 40) and weak, reports `coverage`, and never states that all removed text is absent. | `test_valid_empty_accepted_revision_passes_prefix_check`, `test_short_removed_text_reappearing_is_reported`, `test_missing_revision_artifact_is_unverifiable` |
| Compiling or importing counted as helper execution | `sys.monitoring` PY_START counts give the helper functions actually executed. Audit-hook stack capture attributes `context.json` writes to helper code. The metrics were renamed to `function_execution_steps` and `helper_written_accepted_edit_steps`, and the docs say that write attribution does not prove the helper chose the content. | `test_helper_use_is_verified_not_just_inferred`, `test_compile_or_import_alone_is_not_helper_execution` |
| PID check | Documented as a guard, not a concurrency lock; live commands must run sequentially. | (docs) |

Verification: `ruff check`, `ruff format --check` and `mypy src` are clean; **56 passed**. `doctor --sandbox` is unchanged.

Live phases: **still not run**. `ANTHROPIC_API_KEY` is still unset in the agent process (`doctor`). Live spend $0.00; cumulative $0.00 of $10.00.

## Coding task family and evaluator (2026-10-05)

`coding.py` adds an invoice-package task with staged, superseding requirements and a sandboxed evaluator (SPEC §15). Tests: `tests/test_coding.py`. They cover stage visibility, evaluator isolation, read-only visible tests, and scoring of correct, regressed, stale-rule and unsubmitted solutions. Incident prompts are verified byte-identical.

## Summary-baseline policies (2026-10-04)

`token-tail/1` was added alongside the unchanged default `fixed-tail/1` (SPEC §14). Tests: `tests/test_summary_policy.py`. In one of them, a scripted large-recent-observation case overflows under `fixed-tail/1` and completes under `token-tail/1`. The default policy's prompts are byte-identical to those recorded in experiment 002.

## Exporter ownership change (2026-10-04)

- Experiment narratives (README, SUMMARY, protocol) moved to the results repository, along with the experiment-001 source patches; they were already identical copies there.
- `clm-lib export` no longer overwrites human-maintained documents, and creates generic starters only when they are missing.
- The experiment-level `SHA256SUMS` now excludes those documents. Immutable artifact trees keep their own checksums and conflict protection, and existing artifact entries are re-verified before the experiment checksum file is rewritten.
- Tests: `tests/test_export.py`.
