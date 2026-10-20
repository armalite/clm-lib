# Results overview

**Experiment write-ups, findings and evidence live in [clm-lib-test-results](https://github.com/armalite/clm-lib-test-results).** That repository owns the experiment READMEs, summaries, historical protocols, frozen provenance and exported run artifacts. This file keeps only a short overview, plus the library's own verification history.

## Experiments

| Experiment | One-line outcome | Write-up |
| --- | --- | --- |
| 001: short incident pilot | Tasks finished in 3–4 calls before either context-management method activated (0 edits, 0 summaries). A separate prompted run demonstrated real context replacement and helper use. | [001](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/001-short-incident-pilot) |
| 002: staged incident under context pressure | Both methods active in every run. CLM 6/6 strict successes vs the baseline's 4/6, about 38% lower mean cost, mostly from avoiding summary calls. This is a small synthetic pilot against one baseline. | [002](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/002-staged-incident-context-pressure) |
| 003: robust summary comparison | Against `token-tail/1`, a baseline that handles large recent entries and leaves room, both approaches got 6/6 strict successes. CLM was still about 25% cheaper and 28% faster, matching the baseline's spending on summary calls. | [003](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/003-robust-summary-comparison) |
| 004: coding with changing requirements | On an invoice-package task with requirements added and replaced over four stages, both approaches got 12/12 strict successes with no regressions or stale rules. CLM was about 25% cheaper, matching the baseline's spending on summary calls. | [004](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/004-coding-changing-requirements) |
| 005: harder coding accuracy | Stopped after calibration: with interacting, partially changing rules over 6 or 8 stages, both approaches passed every hidden check in all 8 calibration runs. By the pre-registered rule, the evaluation was not run, and no correctness claim is made. | [005](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/005-harder-coding-accuracy) |
| 006: prompt-caching comparison | On the experiment-004 coding task, with a shared block layout and explicit 5-minute cache breakpoints, all 48 runs were strictly successful. With caching on, CLM was about 35% cheaper than the baseline (27% with caching off) and about 20% faster either way. | [006](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/006-prompt-caching-comparison) |
| 007: reusable context-management helpers | On a 10–12-round incident task, CLM did not create helpers when merely allowed (initial calibration). With the strategy instructed (revised phase, 24 runs), strict success was baseline 4/8, direct editing 6/8, reusable functions 7/8; both CLM strategies were about a third cheaper and a quarter faster than the baseline in every block, with no clear difference between them. Two CLM-only runtime defects were found and fixed. | [007](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/007-reusable-context-helpers) |
| 008: optional Jev context review | Opt-in, host-mediated `review_context` action (current revision only). Diagnostic: 10/10 affected and 1/12 unaffected labelled entries flagged. Pilot (4 runs): all succeeded; the agent never used the optional action, so no effect is shown. Larger evaluation not run. | [008](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/008-jev-context-review) |
| 009: update-triggered Jev review (strategy 1) | Both arms see the same round update and review instruction; only the assisted arm gets Jev per-entry advice before the next call. Evaluation (24 runs, 4 fresh instances × 3 reps): with advice, mean cost USD 0.415 vs 0.375 and time 158 s vs 149 s (cheaper in 4/12 blocks), so the pilot's saving did not hold; strict success 10/12 vs 8/12, not conclusive. Strategies 2 and 3 are planned only. | [009](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/009-jev-update-review) |
| 010: Jev at Claude's context-edit decisions (strategy 2) | v1 (review optional) and v2 (review required, Claude-labelled) stopped at their calibration gates. v3 (review required, runtime-supplied update; A ordinary CLM, B review, C review + Jev), 36 evaluation runs: Jev added nothing measurable over the review alone (cost −2%, the same correctness); the review workflow cost about 30% more than ordinary CLM; strict success 12/12 vs 10/12, not established. | [010](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/010-jev-initiated-review) |
| 011: Jev checks proposed context rewrites (component diagnostics) | v1: three narrow questions (lost / changed / evidence) on 36 author-labelled frozen cases, with no agent runs: 16/16 harmful rewrites detected but 3/16 acceptable ones flagged (limit 2), so the readiness gate failed; deterministic checks managed 7/16. v2 (evidence-complete: full revision, execution output and newly cited source lines; an `insufficient` question; fresh cases): 16/16 flagged, 1/16 acceptable flagged and 15/16 clear, so the gate passed; the same-evidence deterministic checks managed 11/16 with 8/16 false flags. Gap cases were flagged rather than marked uncertain. | [011](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/011-jev-edit-check) |
| 011: proposed-edit checking on the changing-requirements coding workload — development pilot | A (ordinary CLM), B (an experimental proposal/confirmation step) and C (B plus Jev checks of each proposal) on 2 W2 development instances, run once: all 6 runs strict success (35/35 hidden checks); no harmful context edit observed; every B/C edit confirmed (0 bypasses) and 9/9 C proposals checked; Jev flagged 4, all acceptable; mean cost A USD 0.155, B 0.177, C 0.246 (Jev USD 0.0009). A development pilot, not an evaluation. | [011](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/011-jev-edit-check) |
| 012: Jev screening of document-search results | Component diagnostic on 36 frozen labelled search results: 58/58 support passages and 23/23 complete evidence sets kept with 22% of text omitted (gate passed; threshold 0.5 set from development cases). Development pilot (2 handbook instances × A/B): all 4 runs correct, but screening was never exercised, because Claude read the documents directly instead of searching in both B runs. | [012](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/012-jev-result-screening) |
| 012 v2: Jev screening of document-service search results | The handbook moved behind runtime search/fetch actions (no files). Compatibility diagnostic: 28/28 support passages and 16/16 evidence sets kept, 23% of text omitted. Development pilot (2 fresh instances × A/B): screening exercised in 12/12 B searches; all runs correct; none of the 14 omitted passages needed; Claude fetched 12 back, so B cost USD 0.131 against A's 0.119 per run (+10%). | [012](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/012-jev-result-screening) |
| 014: Jev as a primitive inside Claude's own context helpers — prompted live demonstration | One instructed run on `w2-dev-1` (configured use, not adoption): Claude wrote a saved helper that called real Jev in 4 executions (`jev-primitive/1`); its `p > 0.5` rule chose which notes received a review marker; all 5 accepted edits reached the next requests; 35/35 hidden checks, 14 model calls (none extra), USD 0.184 (TypeSafe 0.0002), 105 s. Jev's influence was small; no benefit measured. | [014](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/014-jev-helper-primitives) |
| 014: W3 comparison phase — development pilot | New W3 register workload (export-gen/2; uncited change notices; register objective in both arms). B smoke + 2 instances × A/B, frozen protocol, run once: all runs code-correct with no register harm; B's helpers called Jev (37 calls) but offline replay found no register change that depended on the answers in any of 15 Jev-using executions (answers were printed; Claude wrote the edits); B cost +18.5% and +32% vs A. Mechanism not exercised; evaluation not run. | [014](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/014-jev-helper-primitives) |
| 014: development revision R1 — configured delegation smoke | One run on w3-smoke-1 under an explicit contract (the helper reads the register, Jev's judgments govern keep/amend/retire, and callers pass only the stage): the contract held; the helper's 0.6 threshold on Jev's answers chose the amendments (7/7 correct at the notices, one false hit at stage 2, replaced rules kept as overridden amendments); 42/42 code checks, register harm 0, USD 0.275. The frozen valid-substitution counterfactual was unsupported at the notices (sequential requests), so it is not demonstrated under the frozen criteria; a post-hoc last-request diagnostic shows answer-dependent selection at all three notices. | [014](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/014-jev-helper-primitives) |
| 014: R1 matched development pilot | 2 fresh W3 instances (w3-dev-3, w3-dev-4) × A (ordinary CLM) / B (unchanged R1 instruction + Jev), frozen protocol, shared 12k budget, once per cell: all runs code-correct with no register harm; B's helpers executed answer-driven keep/amend/retire branches (last-response replay substantive at 2 and 4 notice stages); B cost +97.7% and +61.3% (means USD 0.293 vs 0.528) and took 45–78 s longer, mainly from 1.7–3.5× larger registers. No practical advantage; no larger evaluation proposed. An exploratory offline review (results repository, `r1-pilot-review.md`) found the delegated judgments mostly correct, traced the overhead to verbatim registers and extra steps, notes that "register harm 0" means *passed the current register scorer*, and recommends no further paid work on this configuration. | [014](https://github.com/armalite/clm-lib-test-results/tree/main/experiments/014-jev-helper-primitives) |

**Jev programme status (2026-10-14): paused after 014; no further paid work is authorised.** The cross-experiment summary and the reopening criteria are in the results repository, in `experiments/jev-programme-review.md`.

**Programme 015 (public CLM comparison) status (2026-10-15): implementation only, no runs and no results.** The first milestone is a five-arm comparison on an eight-turn incident thread (`paper_clm_shell`, `paper_clm_tools`, `clm_lite_v2`, `clm_lite_v3_1`, and the `token-tail/1` summary baseline); the wider programme is deferred. Chunk 1 adds opt-in shared foundations (SPEC §27): the native-tool transport `native-tools/1`, tool interfaces, `session-counters/1`, `tool-output-cap/1` and accounted native dispatch. They are not wired into the runner; historical configurations are unchanged. No API calls were made and no experimental spending ceiling has been agreed. The draft protocol and the open decisions are in the results repository, under `experiments/015-public-clm-comparison/`. **Library-first sequencing (2026-10-15):** the lite arms' context-management runtime will come from the separate [guarded-clm](https://github.com/armalite/guarded-clm) library, which clm-lib will import at a pinned version; clm-lib will not hold a duplicate implementation. **Correction (2026-10-15):** guarded-clm's design changed: it is now a Pydantic AI capability (`ClmContext`, `ClmStepLimit`, `clm_context()`). It replaces that library's earlier, unreleased core, and its five-chunk plan is superseded. Its offline verification is described in its own docs/results.md. **Integration (2026-10-15):** the library now runs on an opt-in clm-lib path (`clm_lib.p015`, SPEC §28), in a separate environment, verified offline with scripted and mocked-provider models only. There are no paid results.

The previous, longer version of this file is archived verbatim in the results repository as [`notes/clm-lib-results-log-2026-10-04.md`](https://github.com/armalite/clm-lib-test-results/blob/main/notes/clm-lib-results-log-2026-10-04.md). The draft blog notes moved there too, to [`notes/blog-notes.md`](https://github.com/armalite/clm-lib-test-results/blob/main/notes/blog-notes.md).

## Spend

The active ledger is `runs/ledger.json` in this repository (gitignored). Totals so far: **USD 46.994** (experiments 001–003 USD 8.917; 004 USD 7.297; 005 USD 3.719, calibration only; 006 USD 11.207; 007 initial calibration USD 2.573; 007 revised phase USD 13.281), all from provider-reported usage. One interrupted call was charged its full reservation (USD 0.141, assumed). The ceiling was changed five times, each time explicitly and with a recorded reason (`ceiling_history`):
- for experiment 004, from USD 10 to USD 38.917;
- for experiment 005, to USD 46.214;
- for experiment 006, to USD 49.933;
- for the 007 initial phase, to USD 61.140;
- for the 007 revised phase, to USD 63.713 (each later change: spend at the change + USD 30).

It was never reset. On 2026-10-12 the ceiling was set explicitly to USD 56.994 (spend + USD 10, for experiment 008). Experiment 008 then spent USD 1.415 (Anthropic 1.4147, TypeSafe 0.0004), leaving USD 48.409 spent. On 2026-10-13 the ceiling was set explicitly to USD 58.409 (spend + USD 10 new, for experiment 009; 008's unused allowance did not carry over). Experiment 009's initial phase then spent USD 1.801 (Anthropic 1.7983, TypeSafe 0.0028), leaving USD 50.210 spent. On 2026-10-13 the ceiling was set explicitly to USD 70.210 (spend + USD 20, for 009's evaluation; earlier unused allowances did not carry over), and the evaluation spent USD 9.478 (Anthropic 9.4677, TypeSafe 0.0098), leaving USD 59.688 spent. On 2026-10-13 the ceiling was set explicitly to USD 89.688 (spend + USD 30, for experiment 010); 010's calibration spent USD 1.522, leaving USD 61.209 spent. For 010 v2 the ceiling was set explicitly to USD 91.209 (spend + USD 30); its calibration spent USD 2.516, leaving USD 63.725 spent. For 010 v3 the ceiling was set to USD 93.725 (spend + USD 30); v3 spent USD 18.278 (Anthropic 18.2603, TypeSafe 0.0177), leaving USD 82.003 spent. For 011's diagnostic the ceiling was set explicitly to USD 85.003 (spend + USD 3, TypeSafe only); it spent USD 0.0026, leaving USD 82.006 spent. For 011's v2 diagnostic the ceiling was set explicitly to USD 85.006 (spend + USD 3, TypeSafe only; v1's unused allowance did not carry over); it spent USD 0.0045, leaving USD 82.010 spent. For 011's agent pilot the ceiling was set explicitly to USD 97.010 (spend + USD 15, both providers; the v2 allowance did not carry over); the smoke and six pilot runs spent USD 1.283 (Anthropic 1.2807, TypeSafe 0.0023), leaving USD 83.293 spent. For 012 the ceiling was set explicitly to USD 88.293 (spend + USD 5, both providers; the 011 pilot allowance did not carry over); 012's diagnostics, smoke and pilot spent USD 0.523 (Anthropic 0.5153, TypeSafe 0.0072), leaving USD 83.816 spent. For 012 v2 the ceiling was set explicitly to USD 93.816 (spend + USD 10, both providers; the v1 allowance did not carry over); v2's compatibility diagnostic, smoke and pilot spent USD 0.536, leaving USD 84.352 spent. For 014's prompted demonstration the ceiling was set explicitly to USD 85.352 (spend + USD 1, both providers; the 012 v2 allowance did not carry over); the one run spent USD 0.184 (Anthropic 0.1837, TypeSafe 0.0002), leaving USD 84.536 spent. For 014's comparison phase the ceiling was set explicitly to USD 87.536 (spend + USD 3, both providers; earlier allowances did not carry over); its B smoke and four-run pilot spent USD 1.647 (TypeSafe USD 0.0041), leaving USD 86.182 spent. For 014's development revision R1 the ceiling was set explicitly to USD 87.182 (spend + USD 1, both providers; the remaining comparison allowance did not carry over); its one smoke spent USD 0.275, leaving USD 86.457 spent. For 014's R1 matched pilot the ceiling was set explicitly to USD 89.457 (spend + USD 3, both providers; the R1 smoke's remainder did not carry over); its four runs spent USD 1.642, leaving USD 88.100 spent. Live commands now need a config whose ceiling is not below USD 89.457, such as `configs/exp014p2.toml`. Historical snapshots are exported to the results repository's `ledger-snapshots/`.

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

## Programme 015 `paper_clm_tools` (2026-10-20, offline; not evaluated)

**What was built (SPEC §39):** the structured-tools paper-style arm, `paper-clm-tools/1`. The model manages its context through `context_read` and `context_apply`, with operations to replace, delete, substitute and insert, staged and applied all or nothing. Over the limit, it rolls back to a margin, with per-session retries and then a final turn. It is a separate arm, not a version of `paper_clm_shell`, whose versions are unchanged. The other arms, the task and the scorer are unchanged.

**Verification:**
- **The new tests:** 33, in `tests/p015/test_paper_tools.py`. They run sessions through the real runner, Pydantic AI's Anthropic adapter over an in-memory transport, the launcher and the export.
- **The p015 suite:** 284 passed, with network blocked.
- **The historical suite:** 349 passed, unchanged.
- **Lint and types:** ruff, format and mypy are clean in both environments.

No provider call was made, and nothing was spent.

**Details:** the specification, the differences from the structured-tools reference and from the shell arm, the fidelity limitations, the offline evidence and a proposed (unauthorised) one-session live validation are in clm-lib-test-results `experiments/015-public-clm-comparison/paper-clm-tools.md`.

## Programme 015 live validation `dev-v3a` of paper arm version 3 (2026-10-20)

**What ran:** one session of `paper-clm-shell/3` on `docthread-dev-1` (`docthread-task/2`, the shared settings).

**The result:** the editing behaviour was **not demonstrated.**
- **The one edit:** the model wrote a 91.6% prose reduction of its context on the final over-limit request.
- **Why it was rejected:** the model renumbered the kept blocks' headers ("there is no block 48; the file had blocks 1 to 47"). The rejection was correct under the documented rule.
- **The ending:** the turn ended at its context limit in turn 4, scoring 4/10.

**Spend:** $0.247759, all confirmed.

**Details:** clm-lib-test-results `experiments/015-public-clm-comparison/dev-v3a-record.md`.

## Programme 015 paper arm version 3 (2026-10-20, offline; not evaluated)

**What changed (SPEC §38):** `paper-clm-shell/3` gives the paper arm a part-level context file. A tool result can be rewritten as the model's own summary and stays the native result of its call; deleting a call deletes its result; notes become user text; and the runtime rebuilds valid provider messages. Every other paper rule is unchanged, and versions 1 and 2 are kept.

**Verification:** offline, through the real runner, the Docker sandbox and the Anthropic adapter's request bodies.

**Not established:** no live call has been made with it, and confirm1 remains a version 2 result.

## Programme 015 confirmation `confirm1` (2026-10-19, COMPLETE)

**What ran:** six fresh docthread instances (seeds 7407 and 7402 to 7405, and 7496, by the declared rule) × guarded-clm 0.2.0.dev1, `paper-clm-shell/2` and `token-tail-p015/1`. Each of the 18 sessions ran once, with `docthread-task/2`, the stop-on-first-incomplete-turn policy and the smoke-dev2 shared settings.

**Exact-format points out of 10, per instance (guarded / paper / summary):**

| instance 1 | 2 | 3 | 4 | 5 | 6 |
| --- | --- | --- | --- | --- | --- |
| 10 / 4 / 10 | 10 / 0 / 10 | 10 / 4 / 10 | 9 / 4 / 9 | 10 / 4 / 10 | 9 / 4 / 10 |

**Every paper session ended at its context limit,** turn 4 on five instances and turn 1 on one. No mirror edit was accepted in any of them: twelve rejections, all correct under the documented format.

**guarded-clm and the summary arm** completed every turn. They tie on five instances; the summary arm leads by one on the sixth. guarded-clm cost more on each instance: $6.40 against $2.96 in total.

**The run:** 473 attempts, none failed or retried, no truncation.

**Spend:** $10.873678, all confirmed, of the $40 block. Nothing is pending.

**Interpretation:** one model and one task family; the six instances are the units. The `pages` instances come from a narrower part of their template because of the size filter. The paper result reflects this implementation's encoding and rules.

**Details:** clm-lib-test-results `experiments/015-public-clm-comparison/confirm1-record.md`.

**Findings review (2026-10-20):** clm-lib-test-results `confirm1-findings.md`.
- **Cost:** the arithmetic difference between guarded-clm and the summary arm is in cache-write charges; its cause is not isolated.
- **Accuracy:** no difference between those two arms is established.
- **The paper arm:** the editing contract that rejected all its edits is mostly our representation, so the paper comparison is not reference-aligned for editing.

## Programme 015 confirmation preparation tools (2026-10-19, offline)

**What was added (SPEC §37):**
- **The instance checker** applies the predeclared instance rule and writes a manifest that holds no answers. It has run on the development instances only; no confirmation instance has been generated.
- **The exposure report** is read-only over exports. Its totals match the recorded smoke-dev1 and smoke-dev2 evidence.

**One decision is needed before generation:** the rule's evidence-size bounds have more than one reading.

**Live spend:** $0.00.

## Programme 015 paper arm version 2 and the confirmation proposal (2026-10-19, offline)

**What changed (SPEC §36):** `paper-clm-shell/2` keeps a rejected edit's reason through the rollback of its step, which smoke-dev2 showed was lost. Nothing else of the rolled-back step returns, the feedback counts towards the limit, and every other paper-arm rule is unchanged.
- Version 1 stays the default, and is what both smokes ran.
- The new path is verified offline only. Its effect on real-model recovery is unmeasured, and it does not remove the arm's context-limit risk.

**The confirmation configuration** (`configs/p015/proposed-confirmation.json`) is proposed: six fresh instances, three arms, 18 sessions. It is not frozen, not authorised, and no instance has been generated.

**Live spend:** $0.00.

## Programme 015 live development smoke smoke-dev2 (2026-10-19, COMPLETE)

**What ran:** the replacement for smoke-dev1, approved within the remaining USD 13.134552 of the original USD 15 allowance. One eight-turn session per arm on `docthread-dev-1` (`configs/p015/smoke-dev2.json`: `docthread-task/2`, `p015-turn-endings/1`, `p015-truncation/1`; guarded-clm 0.2.0.dev1, source `51e6dbe8…`).

**Results** (each session ran once):
- **guarded-clm:** completed, 10/10, recall 3/3;
- **`paper_clm_shell`:** ended at its context limit in turn 4, 4/10. Its only edit was rejected, and last-step rollback removed the rejection reason and the next four inspections (corrected 2026-10-19). A fidelity review (clm-lib-test-results 015 `paper-arm-smoke-dev2-review.md`) finds no harness defect and nothing inconsistent with the sources. It proposes one narrow correction, not yet approved: keep a rolled-back step's edit receipt in the rollback notice;
- **`token_tail_p015`:** completed, 9/10. The quote had the right line with its column spacing collapsed.

A compatibility result, not a comparison.

**The event-loop fix held:** 73 attempts, none failed or retried. No step limit, call cap or truncation occurred.

**Spend:** USD 1.691882, all confirmed, nothing pending. The programme stands at USD 3.557330 of 15.

**Exports:** all verify.

**Details:** clm-lib-test-results `experiments/015-public-clm-comparison/smoke-dev2-record.md`.

## Programme 015 clarified task, turn endings and truncation (2026-10-19, offline)

**What changed (SPEC §35):**
- **The task's reply format** now states each field's required form, and question 4 asks for the line that states the causing setting and its value (`docthread-task/2`). The scorer, evidence, tools and budgets are unchanged, and version 1 is kept.
- **Continuation:** a new turn-ending policy (`p015-turn-endings/2`) lets the next turn run after a main-model step or call limit. Only guarded-clm supports it. The paper and summary arms cannot follow it safely through their existing bookkeeping, so it is refused for launches that include them.
- **Truncation:** a response stopped at `max_tokens` ends the session as `output_truncated`, with its cost recorded and no retry (`p015-truncation/1`).

**smoke-dev1's recorded 6/10 is unchanged.** Its record now separates exact-format misses from factual errors.

**The successor smoke `configs/p015/smoke-dev2.json`** is prepared and not authorised.

**Verification:** offline only, with scripted models and an in-memory transport. **Live spend:** $0.00.

## Programme 015 session event-loop fix (2026-10-19, offline)

**What changed (SPEC §34, `p015-session-loop/1`):** each p015 session now runs all its turns on one event loop (`asyncio.Runner`). It closes its model clients on that loop before closing the loop. This fixes the defect found in smoke-dev1. Pooling, retries, requests, accounting and every arm's behaviour are unchanged.

**Verification:** against a local keep-alive HTTP server, with synthetic responses and no provider:
- the old pattern fails on turn 2 as it did live;
- the fixed sessions (all three arms, summariser requests, successive sessions, error and stop endings, and a three-arm launch) make no failed or extra attempts;
- request bodies are unchanged.

**Live spend:** $0.00. The ledger is unchanged.

**smoke-dev1's status:**
- guarded-clm completed, but under the defect;
- `paper_clm_shell` was stopped by the operator and is incomplete;
- `token_tail_p015` never ran.

Its contract is now refused, because the source fingerprint changed. A fresh, separately identified three-arm smoke is proposed, not launched.

**Details:** clm-lib-test-results `experiments/015-public-clm-comparison/smoke-dev1-record.md`.

## Programme 015 live development smoke smoke-dev1 (2026-10-18, PARTIAL)

**What was approved and run:** the maintainer approved a USD 15 block. `docthread-dev-1` was run with the frozen contract (`configs/p015/smoke-dev1.json`; guarded-clm 0.2.0.dev1 at `3f18473`).

**Results:**
- **guarded-clm:** completed 8 turns, scoring 6/10 with recall 2/3. A compatibility result, not a comparison.
- **`paper_clm_shell`:** stopped by the operator after 3 calls.
- **`token_tail_p015`:** not run.

**The harness defect:** the p015 runners call `asyncio.run` per user turn while the session's `AsyncAnthropic` client persists. The first attempt of every later turn therefore failed with "Event loop is closed" (7 of 23 attempts), and the bounded retry then succeeded.
- **Reproduced offline** against a local HTTP server. There, the failed request reached the server, so the live failures may have been billed.
- **Fixed offline on 2026-10-19** (the entry above). The smoke is not resumed against the changed source.

**Spend:** USD 1.865448 charged to the block: 0.494056 confirmed and 1.371392 at reservation for the 7 failed attempts. Nothing is pending.

**Exports:** both runs verify.

**Details:** clm-lib-test-results `experiments/015-public-clm-comparison/smoke-dev1-record.md`.

## Programme 015 accepted guarded-clm 0.2.0.dev1 (2026-10-16, offline)

**What changed (SPEC §33):**
- the guarded arm no longer has or passes `strict_ratio_learning`;
- configurations that name it are refused with a migration error;
- the strict-learning configuration is kept byte-identical under `configs/p015/superseded/`, and its successor `configs/p015/proposed-comparison-0.2.0.dev1.json` pins the library;
- live plans must state and match the pin;
- `uv.lock` records 0.2.0.dev1.

**The library:** guarded-clm `3f18473` (0.2.0.dev1, clean, source sha256 `51e6dbe8…d9e4`), unmodified.

**Checks:**
- **The p015 suite:** 168 passed, with network blocked.
- **Direct library and `GuardedSession`** give identical transcripts, with provider-like usage, on:
  - the edit-and-read regression (it now completes);
  - append-only learning;
  - an edit with a plausible observation;
  - the 11 existing equivalence cases.
- **guarded-clm's 32 stored scenarios** match through the accounting wrapper, including the re-recorded `over_budget_ignored`. Its estimate figures changed; its requests, guard decisions and ending did not.
- **The backstop boundary** at 0.5 was checked with the library's own `Calibration`.
- **8 guarded docthread sessions** are identical to the earlier strict-off records.
- **The historical suite** (dev environment): 349 passed, including the isolation tests. Ruff, format and mypy are clean in both environments.
- **Details:** clm-lib-test-results `experiments/015-public-clm-comparison/backstop-verification.md`.

**Not shown:** anything live. The strict-learning evidence below stays as recorded, valid only for `db619a8`.

## Programme 015 guarded-clm strict ratio learning and explicit live settings (2026-10-16, offline; the strict-learning part is superseded by the entry above)

**What changed (SPEC §32):**
- the guarded arm passes `strict_ratio_learning` explicitly (harness default False);
- the proposed 015 configuration (`configs/p015/proposed-comparison.json`) sets it to True;
- live plans must state every experimental choice;
- the cache-isolation, retained-thinking and ledger statements were corrected.

**Library and lock:** guarded-clm `db619a8` (0.2.0.dev0, clean, source sha256 `ed22df29…5fbfcc1`), unmodified. clm-lib's `uv.lock` guarded-clm entry went from 0.1.0.dev0 to 0.2.0.dev0, and only the p015 environment was re-synced.

**Checks:**
- **The p015 suite** has **167 passed**, with network blocked: 138 before, plus 11 strict-ratio and 18 live-spec tests.
- **With strict on,** the direct library run and `GuardedSession` give identical transcripts, including on guarded-clm's rewrite-induced collapse shape with provider-like usage. Off, that shape ends as `context_limit` with reported input under 10,000 tokens; on, it completes. A purely appending run is identical either way.
- **16 guarded docthread sessions** (strict off and on, 8 pairs): identical transcripts in every pair. The scripted usage never produces a collapse-inducing observation, so this shows no change there, not neutrality under real usage.
- **The historical suite** (dev environment): 349 passed, unchanged, including the isolation tests. Ruff, format and mypy are clean in both environments.
- **Details:** clm-lib-test-results `experiments/015-public-clm-comparison/strict-ratio-verification.md`.

**Not shown:** anything live. The earlier offline verification (guarded-clm 0.1.0) stays as recorded; this is new evidence. No provider or token-counting call was made, and the active ledger is unchanged.

## Programme 015 comparison settings and launcher (2026-10-16, offline)

**What was built (SPEC §31):**
- the shared model configuration `p015-model/1` and the bounded retry policy `p015-retry/1`;
- the spending gate `p015-spend/1`;
- the resumable launcher `p015-launch/1`;
- the paper arm's `paper` and `readme` profiles.

The settings, the fidelity review, the cost scenarios and the proposed confirmation design are in the results repository (`experiments/015-public-clm-comparison/`).

**Checks:**
- **The p015 suite** has **138 passed**, with network blocked: 122 before, plus 4 configuration tests, 11 launcher tests (one with the real sandbox) and 1 net new paper test; other paper tests were updated for the profiles.
- **The launcher proof:** a three-arm matrix with the real sandbox; restarts; a killed launcher with an attempt in flight, marked uncertain and not redispatched; stops; a lowered block stop and a summariser refused before dispatch; two competing launchers; a changed fingerprint; and complete and partial exports.
- **The arm matrix:** 23 scripted sessions, rerun with the new default profile; all exported and verified.
- **The historical suite** (dev environment): 349 passed, unchanged, including the isolation tests.

**Not shown:** anything about model performance or live compatibility. No provider or token-counting call was made, and the active ledger is unchanged.

## Programme 015 paper and summary arms (2026-10-16, offline)

**What was built:** `paper_clm_shell` (`paper-clm-shell/1`) and the summary baseline (`token-tail-p015/1`), on a shared runner, `ArmSession` (SPEC §30). The fidelity mapping, the settings table and the full session table are in the results repository, under `experiments/015-public-clm-comparison/` (`paper-clm-shell-fidelity.md`, `arm-settings.md` and `arms-offline.md`).

**Checks:**
- **The suite:** the p015 suite has **122 passed** (88 before, plus 34 arm tests), with network blocked. Four of the new tests use the real Docker sandbox.
- **Sessions:** 21 scripted docthread sessions, reproducible with `python -m clm_lib.p015.docthread.arms_offline OUT --matrix`:
  - every ordinary session completed 8 questions and scored 10/10;
  - the stress sessions reached their target paths: rejected edits, rollbacks, a final over-limit request, the step limit, summary retries, summary overflow and the summariser cap;
  - provider failures and spend refusals ended explicitly.
- **Exports:** all 21 sessions were exported and verified.
- **Fairness:** the three arms show the model the same task: the prompts, the task tools' schemas, argument handling, results and the output contract.
- **The historical suite** (dev environment): 349 passed, unchanged, including the isolation tests.

**Not shown:** anything about model performance. No provider was called, and the ledger is unchanged.

## Programme 015 incident-thread workload `docthread` (2026-10-16, offline)

**What was built:** the generator, tools, task, scorer and a scripted investigator (SPEC §29). The design, parameters and full session table are in the results repository (`experiments/015-public-clm-comparison/docthread-workload.md`).

**Checks:**
- **The suite:** the p015 suite has **88 passed** (68 before, plus 20 workload tests), with network blocked.
- **Scripted sessions** on both development instances, with both policies and both usage paths:
  - all 8 questions completed and 10/10 scored, because the script answers with the true values;
  - a non-tidying policy peaked at about 27,200 estimated tokens against the 29,952 limit, with PRESSURE and one held-back output;
  - a tidying policy stayed near 6,200 tokens and refetched all three recall facts;
  - a provider failure and a spend refusal ended explicitly, and their unfinished questions were scored as missing.
- **Exports** verified (`guarded-session-export/1`).
- **The historical suite** (dev environment): 349 passed, unchanged.

**Not shown:** anything about model performance. No provider was called.

## Programme 015 guarded-clm integration (2026-10-15, offline)

**What was checked:** the `clm_lib.p015` path (SPEC §28), in the p015 environment (Python 3.13.15; pydantic-ai-slim 2.27.1, pydantic 2.14.1, pydantic-core 2.50.1, anthropic 0.125.0; guarded-clm from the sibling checkout). No provider was called. Network connections were blocked in every p015 test.

**The p015 suite: 68 passed**, in these files:
- **`tests/p015/test_equivalence.py`:** 11 synthetic cases run directly through guarded-clm's `clm_context()` and through `GuardedSession` give identical transcripts. That covers every model request (messages, instructions, tool definitions, settings, usage), every trace record, and every output, history or error. The cases:
  - an ordinary read and answer, on both the fixed-ratio and the provider-like usage paths;
  - accepted, rejected, unchanged and protected-refused edits;
  - several calls in one response;
  - a dense burst being held back, an edit freeing room, and a refetch;
  - PRESSURE and the reserve, re-arming within a turn, and the new-turn reset;
  - earlier answers retained;
  - the step limit and the call cap;
  - tool retry and tool failure;
  - provider failure.
- **guarded-clm's own test suite** (57 tests, run from its checkout with the p015 interpreter): all pass on this environment's Python 3.13.15 and pinned stack.
- **Library version metadata:** the loaded `guarded_clm.__version__` is `0.1.0` while its distribution metadata says `0.1.0.dev0`. The provenance records both, and the source fingerprint identifies the exact code. The library has this as an open question for itself.
- **`test_library_scenarios.py`:** guarded-clm's 32 public scenarios, with every model wrapped in `AccountedModel`, match the library's stored transcripts, with one settled attempt per request.
- **`test_accounting.py`:**
  - every attempt is reserved and settled once;
  - failures are billed at the reservation (5xx) or at 0 (429);
  - a mid-session budget refusal is its own ending, and nothing is dispatched;
  - live requests are refused without an authorisation, a price or `max_tokens`;
  - a real `AnthropicModel` runs over an in-memory transport. Its cache reads and writes are priced separately, and the capability's next estimate is anchored on the total reported input.
- **`test_tools.py`:** the bridge preserves the tool's name, description and schema. Invalid arguments get a retry and never run the tool. Results are capped. A raising tool is a `tool_failure`. Calls stay concurrent.
- **`test_export.py`:** the export round trip is complete, checksummed, immutable and tamper-evident, and refusals are exported too.
- **`test_provenance.py`:** fingerprints and git dirty flags.

**What it does not show:** live compatibility, the effect of real-model latency on concurrency, or any task result.

**Historical compatibility (dev environment, unchanged):**
- `tests/test_p015_isolation.py`: the runner, CLI and exporters load neither the new path nor Pydantic AI; no config selects it; the environment still has pydantic 2.13.5 and anthropic 1.11.0 and lacks guarded-clm.
- The full historical suite: **349 passed** (346 before, plus the 3 isolation tests). The p015 tests are not collected in this environment. Ruff, format and mypy are clean in both environments.

## Programme 015 shared foundations (2026-10-15)

The opt-in modules in SPEC §27 (`transport.py`, `tooling.py`, `counters.py`, `output_cap.py`, `native_call.py`) are tested with scripted providers and temporary test ledgers in `tests/test_native_transport.py`, `tests/test_native_accounting.py` and `tests/test_output_cap.py`. One test uses the real Docker sandbox, with no network. The tests cover:
- single and multiple tool calls, with their IDs, order and pairing;
- malformed responses and per-call errors;
- identical error classification in both adapters;
- counter increments;
- retries and summaries, accounted separately;
- the Unicode output cap;
- historical compatibility: the single-action payload, the observation format and executor caps, and the runner, CLI and exporter not importing the new modules.

With ruff, format and mypy clean, **346 passed** (297 before), in about 12 minutes. **No API calls were made.** The scripted tests do not establish compatibility with the live API.

## Multi-round incidents and helper conditions (2026-10-05)

`rounds.py`, the `clm_direct` / `clm_helpers` modes, the separate summary-call allowance, `helper_audit.py`, three-condition schedules and the `spill_past_receipt` fix are covered by SPEC §18. The tests are in `tests/test_rounds.py`:
- generation and evidence support;
- the scorer, on correct answers and ten kinds of faulty answer;
- the condition texts, with earlier prompts byte-identical;
- the summary allowance;
- helper tiers and wrapper detection (Docker);
- direct-edit reuse detection (Docker);
- release visibility end-to-end (Docker);
- the spill fix;
- schedule balance.

With ruff, format and mypy clean, **132 passed**. The revised 007 phase adds the `clm_reuse` condition, phase labels in exports, helper categories and adherence, and the `recheck_after_notice` fix. With these, **138 passed**. Experiment 008 adds `tests/test_jev.py`, including the review-revision boundary tests; with it, **160 passed**. Experiment 009 adds `tests/test_update_review.py`; with it, **171 passed**. Experiment 010 adds `tests/test_initiated_review.py`; with it, **181 passed**. Its v2 adds `tests/test_pre_edit_review.py`; with it, **194 passed**. Its v3 adds `tests/test_pre_edit_review_v3.py`; with it, **204 passed**. Experiment 011 adds `tests/test_jev_edit_check.py`; with it, **210 passed**. Its v2 adds six tests to that file (evidence selection, payload leakage, verdicts and the coverage gate, same-evidence references, v1/v2 separation, question wording); with them, **216 passed**. The 011 agent pilot adds `tests/test_w2.py` (W2 generation, scorer validation with an independent and deliberately faulty solutions, sandbox isolation) and `tests/test_edit_check_pilot.py` (proposal invariants, Jev presentation, recovery exception); with them, **244 passed**. Experiment 012 adds `tests/test_handbook_screen.py` (handbook scorer validation, screening policy and payload, cap repair, unscreened recovery, failure handling, opt-in scope, diagnostic scoring, export rescoring); with it, **256 passed**. 012 v2 adds `tests/test_docservice.py` (corpus, scorer, service actions, identical presentation, unscreened fetch recovery, fail-open, no execute screening, context edits, and a sandbox filesystem check that the corpus is unreachable); with it, **263 passed**. Experiment 014 adds `tests/test_jev_primitive.py` (the mailbox primitive in real containers with a scripted fake Jev: a saved helper's answer-dependent edit and reuse, service errors, limits, invalid requests, timeouts, unanswered requests at exit, no extra model calls, and the per-run extra instruction); with it, **270 passed**. The 014 comparison phase adds `tests/test_w3.py` (W3 generation and information boundary, an independent solution, faulty solutions, register-scorer alternatives and targeted faults, assessment timing, replay attribution) and `tests/w3_solutions.py`; with them, **291 passed**. R1's replay additions (`jev-replay/2`) add four tests; with them, **295 passed**. The R1 matched pilot adds the two new instances to the W3 tests (including sandbox runs of the independent solution) and the `last_request` checks; with them, **297 passed**.

## Request layouts and prompt caching (2026-10-05)

The `blocks/1` layout, caching controls, `run-tag/1` isolation, cache-aware evidence and the four-condition Williams schedule are covered by SPEC §17. The tests are in `tests/test_caching.py`:
- historical configs keep the single-user payload;
- caching-on and caching-off payloads differ only in `cache_control`;
- the status block is never marked;
- the run tag is stable within a run and differs across runs;
- cache-aware cost and reservations;
- cached tokens counted in the request budget and settled per kind;
- summary requests use the same layout and caching;
- CLM edits still shape the next request under `blocks/1` (Docker);
- the report reads `blocks/1` payloads (Docker);
- the schedule is balanced.

With ruff, format and mypy clean, **116 passed**.

## Second coding generator and evaluator (2026-10-05)

`coding2.py` and `invoice_ref2.py` add `invoice-gen/2` and `invoice-checks/2` (SPEC §16). The tests are in `tests/test_coding2.py`:
- the reference agrees with an independent implementation on every hidden check and on more than 10,000 random inputs;
- every forgotten rule, outdated rule, over-applied partial change, calculation-order mistake and refund interaction mistake fails hidden checks in the expected category on every instance;
- in the sandbox, the independent implementation is strictly successful, faulty submissions and wrong return types are detected, stage visibility and isolation hold, and regressions need snapshot evidence.

The export manifest now reports each task's scorer (`test_manifest_reports_the_scorer_of_the_exported_tasks`). `compare --order balanced` is tested by `test_balanced_order_alternates_within_each_instance`. With ruff, format and mypy clean, **104 passed**.

## Coding task family and evaluator (2026-10-05)

`coding.py` adds an invoice-package task with staged, superseding requirements and a sandboxed evaluator (SPEC §15). Tests: `tests/test_coding.py`. They cover stage visibility, evaluator isolation, read-only visible tests, and scoring of correct, regressed, stale-rule and unsubmitted solutions. Incident prompts are verified byte-identical.

## Summary-baseline policies (2026-10-04)

`token-tail/1` was added alongside the unchanged default `fixed-tail/1` (SPEC §14). Tests: `tests/test_summary_policy.py`. In one of them, a scripted large-recent-observation case overflows under `fixed-tail/1` and completes under `token-tail/1`. The default policy's prompts are byte-identical to those recorded in experiment 002.

## Exporter ownership change (2026-10-04)

- Experiment narratives (README, SUMMARY, protocol) moved to the results repository, along with the experiment-001 source patches; they were already identical copies there.
- `clm-lib export` no longer overwrites human-maintained documents, and creates generic starters only when they are missing.
- The experiment-level `SHA256SUMS` now excludes those documents. Immutable artifact trees keep their own checksums and conflict protection, and existing artifact entries are re-verified before the experiment checksum file is rewritten.
- Tests: `tests/test_export.py`.
