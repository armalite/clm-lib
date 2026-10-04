# Results

Status as of 2026-10-04 (build session). Only measured outcomes are reported here.

## Completion levels

| Level | Reached? | Evidence |
| --- | --- | --- |
| Mechanism implemented | **Yes** (offline) | 42 passing tests, including real-sandbox read-back tests (below). |
| Live mechanism verified | **No** | Blocked: no working provider credential (see Blockers). |
| Helper demonstration | **No** | Not attempted live, for the same reason. The offline tests cover helper tracking only with a scripted double. |
| Pilot evaluated | **No** | 0 of 12 comparative cells run; all 12 missing. |
| Benefit observed | **No** | No comparative data. |

No model behaviour is claimed. All runs so far used the `ScriptedProvider` test double, and those runs say nothing about what a real model would do.

## Spend

| Item | USD |
| --- | --- |
| Live provider calls | 0 |
| Ledger (`runs/ledger.json`) charged | 0.0000 of 10.00 ceiling (1 failed smoke attempt, $0) |

The only network traffic to the provider was free credential checking: two `doctor --probe` calls and one `clm-lib smoke` attempt. In each case the SDK's profile refresh was rejected (`invalid_grant`) before any Messages request was sent. The smoke attempt is in the ledger as `error:auth` at $0.00. No tokens were billed.

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

## Blockers

1. **Provider credentials.** Discovery reported presence only, never values:
   - `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`: unset.
   - An `ant` CLI OAuth profile exists for the user's individual organisation, with inference scope. Its access token expired on 2026-09-03, and the SDK's refresh attempt failed with `invalid_grant: Refresh token expired`.
   - An `OPENAI_API_KEY` is exported in the shell profile, but the OpenAI API rejects it (401 `invalid_api_key`), so no OpenAI-compatible adapter was added.
   - `ANTHROPIC_BASE_URL` points at the default `https://api.anthropic.com` and carries no credential.
2. **Smallest missing setting:** one valid Anthropic credential. Either run `ant auth login` (the SDK picks the profile up automatically), or export `ANTHROPIC_API_KEY` in the shell that runs clm-lib.

Remaining live sequence, once a credential exists:

```bash
uv run clm-lib doctor --probe
uv run clm-lib smoke
uv run clm-lib guided --task dev
uv run clm-lib compare --reps 2
uv run clm-lib report --out runs/report.md
```

Per SPEC §8.2, use the `dev` task for any troubleshooting before `compare`. Freeze `PROMPT_VERSION` (currently `2026-10-04.1`) and the config before comparative runs. If dev runs show no context pressure, adjust fixture volume or budget **once** and record the change here.

## Run index

| Run ID | Kind | Provider | Status | Cost |
| --- | --- | --- | --- | --- |
| `runs/offline/20261004T030919-clm-dev-e069` | offline demo | scripted | completed (scripted answer, scored `incorrect` by design) | $0 |

Earlier offline demo runs during development (`…-43af`, `…-4811`) exposed a bug in the demo script itself: the marker string was present in the code body. They are kept under `runs/offline/` and were not used as results.
