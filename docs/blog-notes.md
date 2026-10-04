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

- **The mechanism, offline.** With a scripted model double driving the real sandbox, code inside the container removed a large observation, and the very next request no longer contained it. The immutable event log still did. See `docs/results.md` for the test list.
- **Nothing about real model behaviour.** No live runs were possible in the build session because the API credentials had expired. There is no evidence yet on:
  - whether a real model edits its context unprompted;
  - whether it writes and reuses helpers;
  - whether CLM beats or loses to summaries on cost or accuracy.

Write-ups must not imply any of the above until `docs/results.md` contains live runs.

## Candidate trace excerpts (scripted double, mechanism only)

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

Live excerpts to look for once runs exist:
- the first unprompted CLM edit and the request right after it (`report` checks that removed IDs are absent from that next payload);
- any helper file in `files/step-*/after/helpers/` and the later step that imports it;
- a baseline summary that dropped or kept the stale-vs-authoritative distinction;
- `stale_value` outcomes in either arm.

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
