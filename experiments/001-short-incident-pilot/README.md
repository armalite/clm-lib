# 001 – Short incident pilot

## Question

On short synthetic incident investigations under an 8K-token request budget, does letting the
model edit its own working context (CLM arm) change task quality or cost compared with automatic
summarisation of older history (summary arm)?

## Setup

- Model `claude-opus-5-5`, effort `low`, structured JSON actions, provider-default sampling.
- One task family (`incident-gen/1`): a fictional service incident, with about 250K characters of
  logs, config, change records, metrics and notes per instance. Each instance has a superseded
  ("stale") value that a later authoritative record overrides.
- Evaluation: 3 held-out instances (seeds 201–203), each with a different root-cause scenario,
  × 2 modes × 2 repetitions = 12 runs, run sequentially with mode order alternating per pair.
- Shared limits: 20 calls per task, 2,048 output tokens per call, 30 s per sandboxed execution,
  6,000-character output cap, 8,000-token request budget, pressure reminder or summary at 70%.
- Scoring is deterministic: cause code, remedy code, exact current value (stale values flagged),
  and file:line references covering the cause and the value. Strict success needs all of them.

## Outcome

**The comparison did not test context management.** The model solved every held-out task in
3–4 calls with 2–3 targeted searches.
- **CLM edits:** 0 across the 6 CLM runs.
- **Summaries:** 0 across the 6 summary runs.
- **Pressure:** flagged in 7 runs, always on the final-answer request. In the baseline, only the
  4-entry recent tail existed by then, so there was nothing older to summarise.

| arm | strict success, score/1 (recorded) | strict success, score/2 (corrected) | mean cost | mean calls |
| --- | --- | --- | --- | --- |
| summary | 5/6 | 6/6 | $0.0605 | 3.00 |
| clm | 6/6 | 6/6 | $0.0743 | 3.17 |

- **Scoring correction (post-hoc, disclosed).**
  - In one summary run the answer was `clients.pricing-core.timeout_ms=750`. That is the correct
    value written in the `setting=value` form of the authoritative change record.
  - score/1 required the bare `750` and marked the answer wrong.
  - score/2 also accepts `<identifier>=<value>`. It is applied uniformly to every run by
    re-scoring the recorded answers. The original score/1 records are unchanged.
  - The correction came after seeing this held-out result. It changes the evaluator only, not
    the agent, prompts or limits, and no runs were repeated.
- **Cost:** the CLM arm cost about 23% more per run. Part of this is its longer system prompt
  (about 350 extra tokens per call); the rest is run-to-run variation, including one 4-call run.
  With 6 runs per arm and no context management, this is not evidence about CLM's cost.
- **Guided demonstration (separate, prompted).** On the dev task, the model was explicitly asked
  to build and use a helper (`20261004T065342-guided-dev-a667`).
  - It wrote `helpers/ctx.py::compact(note)` and called it in steps 2 and 3. Instrumentation shows
    the function ran, and that both accepted `context.json` writes came from it.
  - Each next real request started exactly with the accepted revision: the working context went
    from 3,435 to 212 characters, then from 2,853 to 267.
  - So the run demonstrated **real context replacement and prompted helper use**. It is not
    evidence of spontaneous editing, and the helper was never revised.
- **Calibration (separate).** One dev-task summary-mode run (`…-summary-dev-fac1`) checked that
  the budget creates pressure. It did: a summary fired at step 5. No settings were changed.
- **Total cost of the experiment:** $1.0550, all from provider-reported usage; $0 assumed.

## Limitations

- One synthetic task family. The model's efficient searching meant it never needed to manage
  context on the held-out instances.
- Two repetitions per cell; no statistical claims.
- The scorer correction was made after seeing a held-out result (disclosed above).
- The guided run is a prompted capability demonstration, excluded from the comparison.

See `protocol.md` for what was planned versus changed, `report.md` and `metrics.*` for numbers,
and `artifacts/` for the raw evidence.
