# Blog notes (draft material, not for publication as-is)

## What this project is

clm-lib is a small, independent Python implementation of the core idea behind Context Language Models. The model's working transcript is a file the model can rewrite with its own code, and the runtime builds the next request from whatever the model leaves in that file.

It runs on a hosted model (configured for `claude-opus-5-5` through the Anthropic API), not a trained CLM. It is an engineering experiment inspired by the paper, not a reproduction of it.

The pieces:
- **The editable region is authoritative.** The next request's `<working_context>` is rendered from the accepted `context.json` revision, not from a replayed history plus a notes file.
- **There is a protected prefix.** The system protocol and the task are rendered by the runtime on every call and can't be edited. A forged `<task>` tag inside an entry is escaped as data.
- **General transformations are allowed.** The model writes arbitrary Python against the JSON file: delete, rewrite, merge, reorder or add notes. There is no menu of compaction operations.
- **Validation is structural only:** JSON shape, roles, unique IDs, sizes, no symlinks. Invalid edits are rejected with a short receipt and the old state is kept. Nothing checks whether retained claims are true.
- **Sandboxing.** Each execution is a fresh Docker container with no network, a non-root uid, dropped capabilities, resource limits, read-only fixtures and a writable workspace. Credentials, traces and ground truth are never mounted.
- **Comparison.** A summary baseline uses the same model, tools, budget and limits, and replaces older history with a model-written summary at the same 70% threshold where CLM only gets a reminder.

## What has actually been shown so far

All figures are from `docs/results.md`; run IDs are given there.

- **The mechanism works with a real model, when prompted.** In the guided dev run (`20261004T065342-guided-dev-a667`), `claude-opus-5-5` wrote a small helper, `helpers/ctx.py::compact(note)`, and called it in two later steps. Each call replaced the whole working context with one model-written note.
  - The next real request began exactly with the accepted revision: 3,435 → 212 chars, then 2,853 → 267.
  - The task answer was strictly correct.
  - This run was explicitly prompted to build and use a helper, so it is a capability demonstration, not spontaneous behaviour.
- **Unprompted, the model never edited its context.** In 6 held-out CLM runs there were 0 edits.
- **Nothing in the comparison needed context management.** The model solved every held-out task in 3–4 calls using targeted searches, so the summary baseline never summarised either.
  - Both arms were 6/6 strictly correct after a disclosed scorer correction (5/6 vs 6/6 before it; the single failure was a formatting mismatch in the scorer, not a wrong answer).
  - The CLM arm cost about 23% more per run, partly because its system prompt is longer. With n = 6 per arm and no context management happening, this says nothing about CLM's real cost.
- **Experiment 001's honest headline:** on single-stage tasks, an efficient agent didn't feel the 8K-token pressure the fixtures were designed to create. The plumbing works, but there was no evidence for or against CLM-style editing.
- **Experiment 002 (staged evidence) did exercise management.** Evidence arrived in 3 stages, and later stages superseded earlier values and the first hypothesis.
  - Every CLM run edited its context unprompted (2–6 edits, the first already in stage 1, usually before any pressure reminder). Every baseline run summarised 2–5 times.
  - CLM: 6/6 strictly correct, mean $0.231. Baseline: 4/6, mean $0.372. CLM was cheaper in all 6 matched pairs.
  - The traces explain both gaps. The baseline spent 46% of its cost regenerating summaries. Its two failures were a verbatim-tail overflow and a 24-line evidence range that broke the published 20-line limit.
  - Both arms preserved the key early fact (an exact release-note line), so this is not "summaries forget things".
- **The cautious headline for 002:** on one synthetic staged task family, with n = 6 per arm, letting the model edit its own context gave equal-or-better strict success at about 38% lower cost than this summary baseline. A stronger baseline (token-bounded tail, incremental summaries) is the obvious untested competitor.

## Live trace excerpt (guided run, prompted)

The model's helper, written in step 1 (`files/step-001/after/helpers/ctx.py`; reformatted for readability, the verbatim file is in the run directory):

```python
def compact(note, keep_last=0):
    d = load()
    e = d["entries"]
    kept = e[-keep_last:] if keep_last else []
    d["entries"] = [{"id": "notes", "role": "note", "body": note}] + [
        x for x in kept if x["id"] != "notes"
    ]
    json.dump(d, open(P, "w"))
```

The call in step 2 (`scripts/step-002.py`, abridged):

```python
from helpers import ctx

ctx.compact(
    "Notes: changes.log:14 CHG-4162 APPLIED shipping-api db.pool.max_size=12 (was 40) ...; "
    "config yaml:10 max_size 40 (superseded). oncall notes:5 stale. Hypothesis DB_POOL_EXHAUSTED."
)
```

The next request's working context (`requests/0003.json`): `notes` (212 chars), then `s2.act`, `s2.obs`, `s2.rcpt`. The receipt reads:

```text
context.json edit accepted at step 2: revision 1 -> 2; entries 2 -> 1; body chars 3435 -> 212;
removed [s1.act, s1.obs]; added [notes]; rewritten []
```

The note keeps the exact authoritative value and line reference and marks the stale config value as superseded. That is the behaviour the stale-observation trap tests. It also shows the risk: the whole raw observation was discarded in favour of the model's own claims, and nothing validates those claims.

## Offline trace excerpt (scripted double, mechanism only)

From `runs/offline/20261004T030919-clm-dev-e069`. The "model" here is a fixed script, not an LLM.

The edit event, from `events.jsonl`:

```json
{"event": "context_edit", "step": 2, "status": "accepted", "changed": true,
 "removed": ["s1.obs"], "added": ["n1"], "chars_before": 3268, "chars_after": 191}
```

The receipt that the runtime appended to the next request. It lists only IDs and counts, so the removed text doesn't come back:

```text
context.json edit accepted at step 2: revision 1 -> 2; entries 2 -> 2; body chars 3268 -> 191;
removed [s1.obs]; added [n1]; rewritten []
```

The revision diff (`context/rev-0002.diff`, abridged): the 3,000-character observation entry `s1.obs` is replaced by a note `{"id": "n1", "role": "note", "body": "fixtures listed; bulky output dropped"}`.

Since observed live (experiment 002): unprompted CLM edits and baseline summaries on evaluation tasks. Still not observed: a helper revision, unprompted helper creation, and any `stale_value` or `stale_hypothesis` outcome.

## Live trace excerpt (experiment 002, unprompted CLM)

From `20261004T081056-clm-staged-eval-1-ed3b`, step 2 (still stage 1). The model's code replaced `s1.act` and `s1.obs` (6,606 chars) with one note:

```text
Stage1: release notes stage-1/deploy/release-notes-8.19.0-ff7b.md:9 risk-score timeout 2500->800;
:15 db.pool.max_size 60->14. deploy stage-1/deploy/deploys.log:4 quotes-api 8.19.0-ff7b 08:21:06.
config yaml:11 pool 60, :7 timeout 2500. gateway 503 upstream timeout from line 93 on.
oncall suspects risk-score timeout (informal).
```

- The next request (`requests/0003.json`) begins with exactly this note, followed by the step's own action, observation and receipt. Reported input fell from 6,017 to 4,017 tokens.
- The note was rewritten at five more steps as stages arrived.
- The final answer cited `release-notes-8.19.0-ff7b.md:15`, the line the note had kept since stage 1, together with the stage-3 change record giving the current value 24.

## Design points worth explaining

- Why the transcript is JSON-lines data inside one user message instead of native tool-call turns: editing history can't break tool-call pairing or thinking-block replay rules.
- The ordering rule: an edit acts on the context as of the previous step, and the current step's own output is appended afterwards, so the model can never delete output it hasn't seen yet.
- Accounting: every attempt reserves a worst-case cost before dispatch; failed-before-generation errors are charged $0; timeouts are charged the full reservation. That makes the spend report conservative rather than exact.
- The spill rule: the one place the runtime moves content itself, applied identically to both arms, only at the hard limit.

## Limitations to state plainly

- One synthetic task family from one generator; four instances; 12 comparative runs at most. It's a stress test with no statistical power.
- The fixtures were designed to exceed the budget. An agent that searches efficiently may never feel pressure, and that would be a legitimate finding.
- The budget is applied to an estimated token count, recalibrated from provider usage.
- CLM's longer instructions take about 350 tokens of the same 8K budget.
- Prompted helper creation (guided mode) is a capability demonstration, not evidence of spontaneous behaviour or savings.

## Credits

Concepts from the Context Language Models paper (https://arxiv.org/html/2609.37725v1) and the public harness README of facebookresearch/context-language-models (commit 18dc111). No code from that repository (CC BY-NC 4.0) was used.
