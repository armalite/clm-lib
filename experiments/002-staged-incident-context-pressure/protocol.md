# 002 – Protocol: planned procedure versus subsequent changes

## Planned (SPEC v1.3 §12), written before any experiment-002 live run

- Task family `staged-incident-gen/1` (`src/clm_lib/staged.py`): 3 evidence stages released by
  an `advance` action.
  - Stage 1 holds an exact early fact: a release-note line that lowered the DB pool default.
  - Stage 2 supersedes the stage-1 timeout hypothesis.
  - Stage 3 supersedes the pool value. A PROPOSED, not applied, change is a distractor.
- Instances:
  - development `staged-dev-1..3` (seeds 3101–3103), for calibration only;
  - evaluation `staged-eval-1..3` (seeds 3201–3203), not run before freezing.
- Arms: `summary` (existing policy: summarise at 70% of the budget, keep a 4-entry tail) and
  `clm` (ordinary capability instructions plus pressure reminders). Edits and helpers are not
  required.
- Settings: identical to experiment 001 (`configs/default.toml`: `claude-opus-5-5`, effort
  `low`, 20 calls, 2,048 output tokens, 8,000-token request budget, 70% pressure, 1,000-token
  reserve). Adjust the workload or the shared budget only if calibration shows management is not
  exercised.
- Evaluation: 3 instances × 2 modes × 2 repetitions, sequential, with mode order alternating
  per matched pair. Preserve every outcome; no reruns.
- Scoring: score/2. Strict success needs the cause, remedy, exact current value, and evidence
  covering the symptom, the current-value record and the stage-1 origin record. Outcomes also
  flag stale values and the stale hypothesis.

## Calibration (development instances only)

| # | Run | Instance | Mode | Result | Management observed |
| --- | --- | --- | --- | --- | --- |
| 1 | `20261004T080421-summary-staged-dev-1-3ddb` | staged-dev-1 | summary | strictly correct, 12 calls, $0.382 | pressure from step 4 (stage 2); summaries at steps 4–7; the retained-tail rule held; 5 action steps (3 executions, 1 advance) after the first summary |
| 2 | `20261004T080624-clm-staged-dev-2-c8e9` | staged-dev-2 | clm | strictly correct, 9 calls, $0.198 | 2 unprompted accepted edits at stage 2, before any pressure reminder; 5 action steps after the first edit |

**Decision:** both arms manage context during stage 2, with meaningful work remaining. No
workload or budget adjustment was made. Calibration stopped after 2 runs, to keep budget for the
evaluation. `staged-dev-3` was not used.

## Changes after calibration and before freezing (provenance only)

- `compare` now saves the full uncommitted source (tracked diff plus untracked files) as a patch
  next to the comparison record.
- `run.json` now records `task_prompt_sha256`.
- Neither change affects agent behaviour, prompts, limits or scoring.

## Frozen

Comparison `20261004T080858`, launched on 2026-10-04. The comparison record's `frozen` block
stores the model and provider settings, prompt and generator versions, the full config, git HEAD
`1230323` and the SHA-256 of the saved source patch (`20261004T080858.patch`).

## After the comparison

- All 12 cells completed; none were missing, invalid or halted. No runs were repeated.
- **No scorer, prompt, setting or code changes were made after the evaluation.** The two
  summary-arm failures are reported as they were scored: one explicit `context_overflow`, and
  one `unsupported` caused by a 24-line reference range breaking the published 20-line limit.
- Analysis code (trace inspection for README.md) only reads the artifacts.
