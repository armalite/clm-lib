# 001 – Protocol: planned procedure versus subsequent changes

## Planned before any live run (SPEC v1.2 §8.2, build session 2026-10-04)

These items were fixed in code from the build session and not edited before the comparison:
- task generator `incident-gen/1`;
- development seed 101 and held-out seeds 201/202/203;
- prompt version `2026-10-04.1`;
- the default limits in `configs/default.toml`;
- the scorer, score/1.

Planned sequence:
1. One minimal smoke call.
2. One bounded guided helper demonstration on the development instance.
3. Freeze settings, then run 3 held-out tasks × 2 modes × 2 repetitions, sequentially, with mode
   order alternating across pairs.
4. Stop at the budget ceiling and report missing cells.

## What actually happened, in order

1. Build and review sessions: offline only. No working credential existed. One smoke attempt
   failed at credential refresh with nothing sent (`20261004T031059-smoke`, $0).
2. Two rounds of review fixes: accounting, edit evidence and helper evidence. The source was then
   committed by the maintainer as `1230323`.
3. Live sequence at `1230323`, on a clean tree:
   - `doctor --probe` passed.
   - smoke (`20261004T065324-smoke`) passed.
   - guided dev run (`20261004T065342-guided-dev-a667`).
4. **Added step, not in the original sequence:** one development-task calibration run in summary
   mode (`20261004T065458-summary-dev-fac1`). SPEC §6 requires checking for context pressure
   before freezing. The guided run couldn't answer that, because its prompted edits kept the
   context small. Pressure occurred, so the budget and fixtures were **not** changed.
5. Comparison `20261004T065603`, launched without changes. At launch the tool recorded the model,
   provider settings, prompt and generator versions, full config, git HEAD `1230323` and
   "no uncommitted diff". All 12 cells completed, with none missing, invalid or halted.
6. **Post-hoc change: the score/2 scorer correction.** See README.md. score/1 results stay in
   each run's `evaluator/score.json`. score/2 results come from re-scoring the recorded answers;
   the patch is in `artifacts/source/`.

## What was "frozen", precisely

- **Comparison:** the frozen state was recorded by the tool at launch (the `frozen` block in the
  comparison record).
- **Smoke, guided and calibration runs:** the tool did not record code state. They used the same
  commit, which is inferred from the absence of source edits between them and the comparison.
- **Evaluation instances:** the held-out instances were never used for development or
  troubleshooting before the comparison.
