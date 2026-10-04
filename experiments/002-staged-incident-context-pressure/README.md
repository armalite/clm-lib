# 002 – Staged incident under sustained context pressure

## Question

When evidence arrives over time and the working context must be managed while the investigation continues, does letting the model edit its own context (CLM arm) change task quality or cost compared with automatic summarisation (summary arm)?

Experiment 001 could not answer this: its tasks were solved in 3–4 calls and neither arm ever managed context.

## Setup

- **Task family** `staged-incident-gen/1`: a fictional service incident with evidence released in 3 stages. The agent sends an `advance` action to receive the next stage.
  - **Stage 1:** a deploy and upstream timeouts. Among about 15 routine items, the build's release notes lower both a client timeout and the DB pool default. That pool line is the exact early fact.
  - **Stage 2:** an APPLIED change fixes the timeout, superseding the stage-1 hypothesis, and DB pool exhaustion appears. A note repeats a stale pool value from the repo config.
  - **Stage 3:** an APPLIED change raises the pool part-way (the new current value) and exhaustion persists. A PROPOSED larger value is a distractor.
  - **Final answer:** cause, current value, remedy, and `path:line` evidence for the symptom, the current-value record and the stage-1 origin line.
- **Fairness:**
  - Both arms get the same model and settings, evidence schedule, task text, tools, scratch files and limits.
  - Future stages and the evaluator truth are not on disk until released (truth only after the run). Released files never change, and both arms can re-read every earlier file.
  - CLM receives ordinary capability instructions and pressure reminders. Edits and helpers were never requested.
  - The baseline is the unchanged summary policy: summarise at 70% of the budget, keep the newest 4 entries verbatim, one bounded retry, then an explicit overflow.
- **Settings:** identical to experiment 001. `claude-opus-5-5`, effort `low`, 20 calls, 2,048 output tokens per call, 8,000-token request budget, 70% pressure, 1,000-token recovery reserve.
- **Calibration:** 2 runs on fresh development instances, one per arm. Both managed context during stage 2 with meaningful work remaining, so no adjustment was made (see `protocol.md`).
- **Evaluation:** frozen comparison `20261004T080858`. 3 fresh instances (`staged-eval-1..3`) × 2 modes × 2 repetitions, sequential, with mode order alternating per pair. All 12 cells ran; none were missing or invalid.

## Results (measured)

| arm | strict success | completed | mean cost | total cost | mean calls (action + summary) | mean executions | mean input / output tokens | mean peak request | mean elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| summary | **4/6** | 5/6 | $0.372 | $2.229 | 12.0 (8.2 + 3.8) | 5.5 | 56,000 / 7,377 | 6,346 | 99 s |
| clm | **6/6** | 6/6 | $0.231 | $1.386 | 10.2 (10.2 + 0) | 7.2 | 44,060 / 2,741 | 6,005 | 56 s |

Per-component results:
- **Diagnosis and remedy:** CLM 6/6. Summary 5/6; the overflow run gave no answer.
- **Current value:** CLM 6/6. Summary 5/6, same reason.
- **Stale values and stale hypothesis:** none in either arm.
- **Evidence groups:**
  - CLM: all three groups covered in 6/6.
  - Summary: origin and value covered in 5/6, symptom in 4/6.

Management actually happened during continuing work:
- **CLM:** 20 accepted edits, 0 rejected; every run made 2–6 edits. The first edit took effect at stage 1 in all 6 runs (steps 3–5), usually **before** any pressure reminder; 4 of 6 runs never reached the pressure threshold. Between 6 and 10 action steps followed the first edit.
- **Summary:** 20 summaries; every run summarised 2–5 times. The first summary came at stage 1 or 2 (steps 4–5), followed by 2–8 action steps.
- **Recoveries:** none in either arm.

Paired results, every pair of the same instance and repetition:

| instance | rep | summary | clm | clm − summary cost |
| --- | --- | --- | --- | --- |
| staged-eval-1 | 1 | correct, $0.434 | correct, $0.247 | −$0.187 |
| staged-eval-1 | 2 | **context_overflow (no answer)**, $0.344 | correct, $0.231 | −$0.113 |
| staged-eval-2 | 1 | correct, $0.470 | correct, $0.223 | −$0.247 |
| staged-eval-2 | 2 | **unsupported**, $0.350 | correct, $0.196 | −$0.154 |
| staged-eval-3 | 1 | correct, $0.256 | correct, $0.205 | −$0.051 |
| staged-eval-3 | 2 | correct, $0.375 | correct, $0.284 | −$0.092 |

## What explains the differences

These explanations are supported by traces unless marked otherwise.

- **Cost.** The summary arm spent $1.026 of its $2.229 (46%) on summary calls. Each regenerated summary was about 2–3K characters, and summaries came almost every step once pressure began. Excluding them, CLM spent *more* on its own actions ($1.39 vs $1.20): it took more action steps (10.2 vs 8.2) and more executions (7.2 vs 5.5). CLM's lower total comes from avoiding summary calls, not from doing less investigation.
- **Overflow** (summary, `staged-eval-1` rep 2). At step 7 the verbatim 4-entry tail contained a 6,105-character observation from step 5. Summarising the older section, even after the bounded retry, could not bring the request under the hard limit, so the run ended with the specified explicit overflow. This is the declared baseline behaving as specified, not a harness bug. A token-bounded tail might avoid it, but that is untested.
- **Unsupported** (summary, `staged-eval-2` rep 2). Cause, remedy, value and the origin reference were all correct. The only symptom reference was a 24-line range, but the task text (identical for both arms) allows ranges of at most 20 lines. The range does contain symptom lines. The scorer applied the published rule, and it was **not** corrected after the fact.
- **The early exact fact survived in both arms.** In all 11 completed runs, the final answer cited the stage-1 release-note line `…release-notes-<build>.md:<line>`.
  - In 10 of them, the final request still held that reference: in a model-written note (5 CLM runs) or in a summary (5 summary runs).
  - In the remaining CLM run (`staged-eval-1` rep 2) it was absent from the final request. That run's step-7 code referred to the release-notes file again; this is inferred from the code text, not a verified re-read.
  - So the quality gap is not explained by one arm losing the early fact.
- **An edit followed by the next request** (CLM, `staged-eval-1` rep 1, run `20261004T081056-clm-staged-eval-1-ed3b`):
  - At step 2 (stage 1), the model's code replaced `s1.act` and `s1.obs` (6,606 chars) with one 329-char note: `Stage1: release notes stage-1/deploy/release-notes-8.19.0-ff7b.md:9 risk-score timeout 2500->800; :15 db.pool.max_size 60->14. deploy stage-1/deploy/deploys.log:4 ... config yaml:11 pool 60 ...`.
  - The next request (`requests/0003.json`) begins exactly with that note, followed by `s2.act`, `s2.obs` and `s2.rcpt`. Reported input fell from 6,017 to 4,017 tokens.
  - The model then rewrote the same note at 5 more steps as stages arrived. Its final answer cited `stage-1/deploy/release-notes-8.19.0-ff7b.md:15`, plus the stage-3 change record for the current value 24.

## What this does and does not show

- **Measured:** on these 3 instances and 2 repetitions, the CLM arm completed all runs with strict success, at about 38% lower mean cost and about 43% lower mean elapsed time than the summary baseline. CLM was cheaper in every matched pair.
- **Inferred:** the cost advantage comes from avoiding repeated summary calls. The quality advantage comes from two specific baseline failure modes (verbatim-tail overflow and an over-long reference range), not from systematically better reasoning.
- **Not shown or untested:**
  - statistical significance (n = 6 per arm);
  - other task families;
  - a stronger baseline (token-bounded tail, cheaper summariser, incremental summaries);
  - other models or efforts;
  - whether CLM's advantage survives when the baseline does not need to summarise on every step.

## Limitations

- One synthetic task family and generator. The evaluation instances vary entities, values, timings and arrangement, but share the storyline.
- Two calibration runs on different instances, one per arm: enough to confirm management happens, not to tune anything.
- The removed-text "reappearance" check reports false positives when the model re-types its own note text in new code.
- Helper creation was not requested and did not occur.

See `protocol.md` for planned versus actual procedure, `report.md` and `metrics.*` for every number, and `artifacts/` for the raw evidence.
