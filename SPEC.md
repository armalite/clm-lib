# clm-lib: Technical Specification

Version: 2.23 | 19 October 2026

Repository: `clm-lib` | Python package: `clm_lib` | CLI: `clm-lib`

This file defines the implementation requirements and acceptance criteria. User-facing setup and usage live in `README.md`; measured outcomes and experiment write-ups live in the results repository (§13), with a short overview in `docs/results.md`; implementation design lives in `docs/architecture.md`.

Version history:
- 1.2: the original specification. Its evaluation protocol (§8.2) was executed as experiment 001.
- 1.3: adds experiment 002, a staged incident task under sustained context pressure (§12), and the results export (§13). Requirements in §1–§11 are unchanged except where §12 extends them.
- 1.4: experiment write-ups move to the results repository, and the exporter preserves human-maintained documents (§13). The blog notes and detailed results move there too (§9).
- 1.5: adds a selectable, versioned summary-baseline policy (§14) for experiment 003. The original policy stays the default.
- 1.6: adds a coding task family with changing requirements and a sandboxed evaluator (§15), and an explicit, recorded ledger-ceiling change.
- 2.0: adds an optional, host-mediated context-review action backed by TypeSafe Jev (§19), off by default.
- 2.1: adds update-triggered context review with optional Jev advice (§20), off by default. Other Jev-assistance strategies on the roadmap (Claude-initiated review, proposed-edit checks) are planned only and are not specified or supported.
- 2.2: adds Claude-initiated context review with optional Jev advice (§21), off by default.
- 2.3: adds §21's required pre-edit review variant (`initiated-review/2`), off by default, and an optional artifact group in run selections.
- 2.4: adds §21's runtime-supplied review update (`initiated-review/3`), off by default.
- 2.5: adds the experiment-011 edit-check component diagnostic (§22). Agent-side edit checking, result screening and the changing-requirements workload remain planned only. Proposed-edit checks, result screening and the changing-requirements workload remain planned only and are not specified or supported.
- 2.6: adds §22's evidence-complete v2 diagnostic (`jev-edit-diag/2` case files: evidence contract `edit-evidence/1`, questions `jev-edit-check/2`, verdict rule `edit-verdict/2`). It remains a component diagnostic; agent-side edit checking is not supported.
- 2.7: adds the W2 changing-requirements workload (`export-gen/1`, scorer `export-checks/1`) and opt-in proposed-edit checking for CLM runs (`edit-check-pilot/1`, §23), off by default; the §22 diagnostics are unchanged.
- 2.8: adds the handbook document-search workload (`handbook-gen/1`, scorer `handbook-checks/1`) and opt-in Jev screening of structured search results (`result-screen/1`, §24), off by default.
- 2.9: adds §24.3, the experiment-012 v2 document-service workload (`docsvc-gen/1`): runtime `search` and `fetch` actions over a corpus held only in host memory, with `result-screen/1` unchanged except for the recovery verb in its notice and disclosure.
- 2.10: adds §25, Jev as a primitive inside model-written code (`jev-primitive/1`), opt-in and off by default (experiment 014).
- 2.11: adds §25's per-run extra instruction (`run --extra-instruction-file`), used for experiment 014's prompted demonstration; off unless given.
- 2.12: adds §26, the W3 register workload (`export-gen/2`, code scorer `export-checks/2`, register scorer `register-checks/1`) and offline replay attribution for jev-primitive runs (`jev-replay/1`, `replay-jev`), for experiment 014's comparison phase; opt-in (W3 tasks and the command only).
- 2.13: updates §26's replay attribution to `jev-replay/2` (item-level answer matching, valid inverted and permuted substitutions with logs, unsupported-counterfactual detection, a supporting code audit, and post-hoc last-request variants), used for experiment 014's development revision R1.
- 2.14: adds §26's W3 instances `w3-dev-3` and `w3-dev-4` (fresh seeds of the dev-1 and dev-2 schedules) and the replay's neutral `last_request` keys, for experiment 014's R1 matched development pilot.
- 2.15: adds §27, shared foundations for programme 015 (opt-in, not yet wired into the runner): the native-tool transport `native-tools/1`, tool interfaces, versioned counters `session-counters/1`, the task-tool output cap `tool-output-cap/1` and accounted native dispatch. No arm, workload or live path uses them yet; §27.6 lists what is pending.
- 2.16: adds §28, the programme 015 guarded-clm integration path (`clm_lib.p015`): opt-in, in a separate environment, verified offline with scripted and mocked models only.
- 2.23: adds §35: the clarified docthread task (`docthread-task/2`; version 1 kept), versioned turn-ending policies (`p015-turn-endings/1` and `/2`, continuation after a main-model step or call limit, supported by guarded-clm only), and output-limit truncation as its own ending (`p015-truncation/1`); offline-verified.
- 2.22: adds §34: one event loop per p015 session (`p015-session-loop/1`), fixing the per-turn event-loop defect found in the partial smoke-dev1, with client shutdown on that loop; verified offline against a local HTTP server.
- 2.21: adds §33: the accepted guarded-clm 0.2.0.dev1 (always-on implausible-ratio backstop) on the p015 path; `strict_ratio_learning` removed and refused; the library pinned in live plans. §32.1 is superseded.
- 2.20: adds §32: guarded-clm's `strict_ratio_learning` on the p015 path (explicitly True in the proposed 015 configuration), live plans that must state every experimental choice, and corrections to §31.1's cache-isolation statement.
- 2.19: adds §31, the programme 015 comparison contract (`p015-model/1`, `p015-retry/1`) and the resumable launcher with spending controls (`p015-launch/1`, `p015-spend/1`), verified offline only; amends §30.2 with the `paper` and `readme` profiles.
- 2.18: adds §30, the programme 015 paper and summary arms (`paper-clm-shell/1`, `token-tail-p015/1`) on a shared runner, `ArmSession`, verified offline with scripted sessions only.
- 2.17: adds §29, the programme 015 incident-thread workload `docthread` (generator `docthread-gen/1`, task `docthread-task/1`, scorer `docthread-score/1`), verified offline with scripted sessions only.
- 1.9: adds a multi-round incident task family, two CLM capability variants (direct editing only; reusable helpers allowed), a separate summary-call allowance, helper-evidence tiers and a three-condition schedule (§18).
- 1.8: adds a versioned request layout (`blocks/1`), optional provider prompt caching with explicit breakpoints, per-run cache isolation, cache-aware accounting evidence, and a four-condition comparison schedule (§17).
- 1.7: adds a second coding generator with interacting, partially changing rules, an evaluator with richer categories, exact type checks and stage snapshots (§16), a balanced comparison order, and per-task scorer metadata in comparisons and exports.

## 1. Purpose and intended outcome

`clm-lib` is a small, runnable Python experiment that implements Context Language Model (CLM) behaviour on top of an existing hosted model. It demonstrates model-directed changes to live working context, including the ability for the model to write and reuse its own context-management helper code, and compares this against summary-based context management on synthetic tasks.

The project delivers a small reusable library core, with a demo agent and an evaluation harness built on top of it. Becoming a standalone CLM library is an explicit goal; the initial API may remain experimental.

This is an independent implementation of publicly described concepts (see §11). It is not a port of, and does not copy code from, the reference implementation. Substantive departures from this specification must be recorded in the implementation notes rather than silently changing the objective.

## 2. What counts as CLM here

The runtime exposes its current working conversation as an editable file. The model generates code that can inspect and transform that file. After execution, the runtime validates the candidate and uses the accepted contents to construct the next model request. Without an edit, ordinary appending continues.

Required distinction: this file is authoritative for the editable portion of the next input. Updating a notes file while replaying the unchanged old conversation is not sufficient. Neither is a developer-written summariser presented as CLM.

| Component | Responsibility |
| --- | --- |
| Model | Decide what to retain, remove, rewrite or reorganise; generate editing code and optional reusable helpers. |
| Runtime | Expose context; execute code in isolation; validate and apply edits; assemble requests; enforce budgets; record evidence. |
| Provider adapter | Make stateless model calls and return text, usage and errors. No invisible conversation replay. |
| Evaluator | Check the task answer against ground truth the agent cannot access. |

System instructions, the original task and enforced limits remain outside the editable region (a protected prefix, consistent with the reference harness). The runtime must allow general transformations of working content, not merely a menu of delete/summarise actions. Structural validation is appropriate; it cannot establish that a retained claim is true.

Creating helpers during a run is distinct from improving a skill across runs or changing model weights. This version implements neither cross-run skill optimisation nor training. A new helper is not required on every run, and helpers must not be described as emerging spontaneously when the prompt requests one.

## 3. Architecture and interfaces

### 3.1 Tooling

Python 3.11+, managed with uv. ruff for linting, pytest for tests, lightweight type checking (mypy). Keep dependencies small.

### 3.2 Layout

Suggested layout, adaptable to existing conventions:

```text
src/clm_lib/
  context.py       # working-context schema, revisions, candidate validation
  runner.py        # agent loop, modes and budgets
  provider.py      # one real adapter and a scripted test adapter
  executor.py      # isolated Python execution
  baseline.py      # summary policy
  tracing.py       # requests, context revisions, usage and costs
  tasks.py         # fixture generation and scoring
  cli.py
tests/
configs/
docs/architecture.md
docs/results.md      # short results overview + library verification history
README.md
SPEC.md
pyproject.toml
.env.example
.gitignore
```

Use a small importable runner and explicit provider/executor interfaces. The `clm-lib` CLI calls the same `clm_lib` core. Keep synthetic task/scoring logic separate from context-management operations and include a minimal Python usage example. The library must be usable locally without package publication. No plugin registry, database, web UI, orchestration framework or package publication is needed.

### 3.3 Request format and model actions

The model returns one JSON action per call: either `execute` with Python source, or `final` with a structured answer. The schema is documented and responses are validated. Use provider-supported structured output where available; otherwise permit at most one bounded repair retry. Never parse arbitrary prose as executable code.

Each request is rendered fresh from: protected instructions, the original task, and the editable transcript. The transcript is data within the request, not a mechanism for inventing provider system roles. This avoids broken native tool-call/result pairings when history is edited. The representation is identical across modes, except for the context-edit capability and management instructions. The exact protocol is an engineering choice for this experiment, not a claim to reproduce every reference detail.

### 3.4 Working-context file

Working turns are stored in a documented JSON file with stable IDs and textual bodies. Editing may delete, rewrite, combine and create working entries, including notes. Authoritative revision metadata is kept outside the model-writable file. The model must be able to author its own Python transformations, not only invoke prewritten compaction functions. Mutable notes must never replace the original task or authoritative evaluator records.

### 3.5 One iteration

1. Mirror the accepted working state into the per-run workspace; retain the previous valid version externally.
2. Build and record the actual outgoing request, its context revision and a content hash. Apply context/call/cost limits.
3. Call the provider. Record response and usage. A valid final answer ends the task.
4. For an execution action, run its Python in the task sandbox. The model can read fixtures, write helpers and edit the context file there.
5. Read the candidate. Reject malformed structure, duplicate IDs, disallowed roles, excessive size or other explicit format violations. On failure retain the previous valid context and record a rejection receipt. Do not execute host-side code while parsing.
6. Accept a valid revision atomically. Append a bounded execution result/receipt according to a documented ordering rule. Do not reinsert the full pre-edit history. Preserve complete execution history separately in the immutable trace.
7. Continue with the accepted state. Identify whether an edit actually changed content, rather than counting every file write as useful editing.

The specification and tests must state exactly whether the latest command/result is preserved after an edit, and the next-request trace must make this auditable. Append only a small receipt where necessary; a deleted large observation must not reappear inside that receipt.

### 3.6 Execution isolation

Model-generated code must not run unrestricted on the host that holds API credentials and personal files. The preferred executor is a Docker sandbox with: no network, a non-root user, dropped capabilities, memory/CPU/process/time limits, a read-only fixture mount, and a dedicated writable workspace containing the editable file and helpers. Credentials, the provider client, evaluator answers, immutable traces and the user's home directory stay outside it. The Docker socket is never mounted inside the sandbox. Reuse an appropriate standard image; avoid unnecessary infrastructure.

Host-side edit application is separate from container execution. Validate candidate size and file type; reject symlinks and paths escaping the expected workspace. Use per-run containers/workspaces and bounded output capture. Both comparison arms get equivalent fixture access and output caps.

If Docker is unavailable, an already-available, genuinely isolated executor may be used if suitable. A Python subprocess with a timeout is not equivalent isolation. The runtime must never silently fall back to unrestricted execution of generated code; if no isolated executor exists, live and sandboxed runs are blocked with an explanation.

## 4. Model access, cost and limits

### 4.1 Provider

Implement one provider first, preferably Anthropic; an OpenAI-compatible endpoint is an acceptable alternative. Only add a second provider if it resolves an actual access problem. Provider, model and endpoint are configurable. Model identifiers must be verified against provider documentation or APIs, not assumed.

Credentials are resolved only through the provider SDK's supported configuration mechanisms. The library never prints, logs or copies credential values, never logs authenticated headers, and never reads credential stores or other projects directly. Diagnostics may report only whether a credential source is available. If indispensable connection information is missing, offline functionality must still work and the tool must name the smallest missing setting.

### 4.2 Cost control

Default live-validation ceiling: USD 10 total, covering smoke calls, all modes, summaries, malformed-output retries and interrupted runs. The spend ledger persists across invocations. A lower user-specified budget takes precedence; the ceiling is never increased automatically.

Before calls, resolve dated model prices from official sources or explicit configuration, and conservatively estimate the maximum next-request cost from input and output limits. Reserve that amount before dispatch. Account for cache pricing and provider usage semantics. Stop before dispatch if the remaining budget is insufficient. If cost cannot be bounded (for example an unknown model price), live mode stays blocked with an actionable explanation. Show incurred cost and estimated/reserved cost separately; do not promise an exact billing cap where provider reporting prevents one.

### 4.3 Run limits

Defaults: sequential requests; at most 20 model calls per task (compaction, summary and formatting retries included); 2,048 output tokens per call; 30 seconds per code execution; finite API timeouts and retries. Limits are configurable, recorded in every run, and shared across comparison arms. Honour the model's actual context capacity, reserving output headroom.

## 5. Run modes

### A. Summary baseline

Same task-solving model, execution tool, fixtures, answer format, context budget and limits as CLM. The baseline can write ordinary helper files but has no authoritative context-edit file. At a declared token threshold, the model is asked to summarise older history while preserving task-relevant exact details, unresolved questions and failed approaches. A declared small recent tail is retained and the protected prefix is unchanged. The summary replaces the summarised section rather than being appended.

Summary calls and their cost are counted. Threshold and tail configuration are recorded. The summary prompt must not be deliberately weak or detail-destroying. If the summary remains too large, apply a bounded retry, then return an explicit overflow result; never silently truncate critical information.

### B. CLM evaluation

The context file is exposed and the model is told it can edit it and author helpers as useful. Budget occupancy information and threshold reminders are provided. The runtime does not provide a bespoke prewritten editing algorithm, disclose fixture ground truth, force a helper, or require an edit every step.

The same warning/pressure thresholds as the baseline apply: the baseline triggers its prescribed summary; CLM receives a request to manage its context. CLM may edit earlier if the model chooses. Enough room is reserved to request an edit before exceeding the limit. If recovery fails within the shared limits, the run is recorded as failed or exhausted.

### C. Guided helper demonstration

The agent is explicitly prompted to create a reusable context-management helper, invoke it at least twice, and revise it if useful. Code, executions, revisions and resulting requests are recorded. A model-created helper run twice is the target; a meaningful code revision is a stretch outcome, never faked or manufactured after the run.

This mode is labelled a prompted capability demonstration and is excluded from the baseline-vs-CLM effectiveness comparison. No helper appearing in mode B is a legitimate result. Mode C must not be presented as spontaneous behaviour or as evidence of lower cost.

## 6. Synthetic task and fixtures

A fictional service-incident investigation requiring inspection of multiple local log/configuration files. Fixtures are generated deterministically, with one development instance and three held-out instances of the same task family. Held-out instances differ in concrete entities, values and evidence arrangements, not just answer wording. Evaluation seeds and prompt versions are frozen before comparative runs; documentation must state that all instances share one synthetic generator.

Each task has:

- a root cause supported by traceable evidence;
- one exact configuration value or identifier required for the answer;
- a superseded observation that must not override a clearly authoritative later update;
- plausible but irrelevant observations and repeated log content;
- a concrete remedy that follows from the evidence.

The answer is structured with `root_cause`, `required_value`, `remedy` and `evidence_refs`. A deterministic scorer checks supported cause/remedy categories, the exact value and valid supporting source references, reporting component scores and strict all-components success. Another paid model is not used as the primary judge. Ground-truth answers stay outside the sandbox and agent-visible prompts.

The working-context budget is moderate and configurable, initially around 8K input tokens, to exercise management in short runs. Total fixture material exceeds that budget, but no individual required observation exceeds the executor output cap. Relevant evidence stays reachable and files are line-addressable. The agent may search efficiently rather than read everything; tasks must not force wasteful behaviour just to cause overflow.

If development runs do not create context pressure, fixture volume or budget may be adjusted once before freezing the evaluation, and the change recorded. If evaluated agents avoid pressure by reading efficiently, that is reported; tasks must not be made progressively harder until CLM wins. This is a controlled stress test, not an estimate of normal production savings.

## 7. Evidence and reporting

Generated runs are stored under a gitignored directory, separating editable context, the immutable chronological event history, and evaluator results. Each run records configuration and model identity, fixture seed/hash, prompt version, limits, mode, start/end times, status and stopping reason.

Required measurements:

- task correctness and component scores;
- total provider calls, executions, summaries, edit attempts, accepted/rejected edits and retries;
- cached and uncached input, cache-write tokens where applicable, output tokens, and any separately billed reasoning usage;
- provider-reported versus locally estimated usage, clearly labelled and without double counting;
- total dollar cost using documented prices, or an explicit unavailable status;
- elapsed time, peak working-context size, and context revisions;
- any human intervention after a run starts.

Exact requests are preserved (excluding secrets and request headers) so it is provable what the model saw. Context diffs and generated scripts are saved. Executable code returned by the model is recorded before it runs, and helper file contents before/after executions are recorded so authorship and reuse are reviewable. Evaluator truth stays out of model-visible snapshots until final scoring.

Minimum context size, edit count and generated-code volume are not success objectives. Lower cost with worse task quality is a tradeoff, not an unqualified improvement. Failed and exhausted runs are included in outcome reports and cost totals. Infrastructure-invalid runs are labelled separately with their spent cost preserved, never silently replaced. No statistical-significance claims are made from this small pilot.

## 8. Verification and evaluation protocol

### 8.1 Offline tests

Focused offline tests use a scripted provider, which must be unable to silently substitute for the real adapter in live mode:

1. Remove a unique marker through the actual executor/read-back path; assert it is absent from the very next assembled request while the original event log retains it.
2. Rewrite a value, retain an exact required fact and reorganise entries; assert the resulting outgoing payload matches the accepted version.
3. Attempt malformed/oversized/duplicate-ID edits; assert the previous valid state survives and a bounded receipt is provided.
4. Verify protected original task/system text cannot be replaced by edits, including text pretending to be a system message.
5. Verify model-generated code cannot see a dummy secret environment variable or the evaluator answer path. These check the configured boundary; they are not a proof of sandbox security.
6. Exercise summary replacement, context overflow recovery and stop conditions.
7. Verify all call types, rejected edits and retries are included in accounting, and that no call starts after exhaustion of the remaining reserved budget.
8. Verify scoring distinguishes correct, unsupported and stale-constraint answers; verify fresh runs cannot inherit helpers or context from earlier runs.

Lint and type checks run alongside these tests.

### 8.2 Live validation sequence (executed as experiment 001)

With valid access and isolation:

- one minimal real provider smoke call;
- one bounded guided helper demonstration on the development instance, plus basic troubleshooting;
- freeze prompts/configuration, then attempt three held-out tasks × two modes (summary and CLM) × two repetitions = 12 comparative runs, sequentially, alternating mode order across pairs;
- stop at the budget ceiling if the full matrix cannot fit, and report completed cells, missing cells and paired results without treating incomplete cells as failures or evidence of a win.

Both arms use the same model version and sampling settings; seeds are used only if supported and identical randomness is not claimed. Held-out failures must not be repeatedly tuned against and still called held-out results. If tuning after evaluation is essential, that set is relabelled development and the earlier outcomes are preserved.

### 8.3 Later experiments

Each later experiment follows §12's protocol rules: calibrate on fresh development instances, freeze, then evaluate on instances that did not inform tuning.

## 9. Deliverables and acceptance

A documented CLI covering: an environment doctor with no secret output; offline tests/demo; one live run; the guided helper demo; a bounded comparison; and report generation. README shows the exact commands that work. No web UI.

Required files:

- installable/importable Python package and CLI;
- configuration examples and `.env.example` containing names/placeholders only;
- synthetic fixture generator, task definitions and scorer;
- focused tests and a reproducible dependency setup;
- README with quickstart, model/access setup, sandbox prerequisites, run commands, budgets, architecture and limitations;
- `docs/results.md` with a short results overview, cost totals and library verification history, linking to the results repository;
- in the results repository: experiment write-ups with actual outcomes, cost provenance, run IDs and blockers, and draft blog notes (`notes/blog-notes.md`) with an accurate project description, trace excerpts and limitations, not a fabricated success story;
- an implementation note listing important design choices and departures from the reference and this spec.

Completion levels:

| Level | Evidence |
| --- | --- |
| Mechanism implemented | Offline tests prove actual next-request replacement, protected prefix and recovery. |
| Live mechanism verified | A real model-generated edit changes a subsequent real request and the task continues. |
| Helper demonstration | A real model authors and reuses a helper, with prompting disclosed. Reported if not observed. |
| Pilot evaluated | Comparative runs are scored and all costs/statuses reported, including missing cells. |
| Benefit observed | Results support a specific quality/cost claim on this small task set. Not required for completion. |

The live and helper levels are never reported complete based on scripted model doubles. If the model does not edit or produce helpers within the allotted attempts, the functioning capability is delivered with that finding. If credentials, executor setup or budget block live work, the offline implementation is still complete and the exact remaining action and command are documented.

No claim of a general-purpose library or benchmark reproduction beyond what exists.

## 10. Scope exclusions

No RLM, multi-agent system or delegation infrastructure, persistent-memory platform, cross-task helper selection, RL/fine-tuning, Suffix Cache Reuse, model-server changes, production deployment, broad framework integration or automatic publication of packages or articles. No copying of the official implementation or any other repository's code. The described concepts are implemented independently with ordinarily licensed dependencies, and the research is credited.

## 11. Public references and source discipline

Use the paper for research claims and current official implementation documentation for runtime details. Blog posts are commentary, not independent replication. The reference revision consulted is pinned in the implementation notes. The official research repository is licensed CC BY-NC 4.0; its source must not be copied into this differently licensed library. This project is original code, not a port.

- Paper: https://arxiv.org/html/2609.37725v1
- Official repository: https://github.com/facebookresearch/context-language-models
- Reviewed harness documentation: https://github.com/facebookresearch/context-language-models/blob/18dc11115f50f261233c5bba7937834491e307e8/clm/clm_harness/README.md
- Separate skill-evolution workflow (background reading only): https://github.com/facebookresearch/context-language-models/blob/18dc11115f50f261233c5bba7937834491e307e8/clm/clm_icl/README.md

This is an engineering experiment inspired by CLM. Its action protocol, synthetic tasks and runtime are not an exact reproduction of the authors' benchmark setup, and nothing here implies the paper guarantees savings with a particular model or workload.

## 12. Experiment 002: staged incident under sustained context pressure

Motivation: in experiment 001 the agent solved short tasks in 3–4 calls, so neither arm managed context. Experiment 002 is the smallest extension of the harness that makes useful information have to survive sustained pressure while investigation continues.

### 12.1 Task

- Evidence is released in stages. Only stage 1 is mounted at the start. The agent requests the next stage with a staged-only `advance` action; the next stage's files are then written into the read-only fixture mount, and an update message is appended to the working context.
- Future stages and evaluator truth exist only in host memory until released (or, for truth, until scoring).
- Released files never change. Earlier evidence stays readable and re-readable throughout, and both arms have the same access.
- A `final` answer before all stages are released is not accepted. The runtime records a neutral observation stating how many stages are released; it reveals nothing about the answer.
- The task family must contain:
  - exact facts from early evidence that stay relevant later;
  - authoritative updates that supersede earlier values and an earlier hypothesis;
  - further investigation after pressure begins;
  - a final diagnosis, remedy and supporting evidence.
- Scoring extends §6 with:
  - an `origin` evidence group, citing the early record that introduced the problem;
  - a `stale_hypothesis` outcome, when the cause is a superseded hypothesis.
  Stale-value detection covers both the superseded build default and the repository config value.

### 12.2 Arms and fairness

- Same as §5: same model and settings, task, evidence schedule, tools, scratch files and limits.
- The CLM arm gets ordinary capability instructions and pressure reminders only. Edits and helpers are never required in evaluation runs.
- The baseline uses the existing summary policy unchanged.
- Neither arm is made to read wastefully.

### 12.3 Protocol

1. Calibrate on fresh development instances, distinct from evaluation instances.
2. Check that pressure occurs while meaningful investigation remains, and that summary eligibility (including the retained-tail rule) and management activity actually happen.
3. Adjust the workload or the shared budget only if needed, recording every calibration attempt. Selection is based on exercising context management, never on which arm does better.
4. Freeze the task generator, evaluation instances, prompts, model and settings, scorer and code state. The comparison record stores the git HEAD and the full uncommitted source as a patch.
5. Run 3 evaluation instances × 2 modes × 2 repetitions, alternating mode order across matched pairs. Preserve every outcome; do not tune against evaluation failures or rerun for better results.
6. If calibration cannot establish a useful workload, stop and report the specific problem instead of spending budget on an uninformative comparison.

### 12.4 Measurements

In addition to §7:
- strict success, with diagnosis, remedy, current value, stale-value and stale-hypothesis errors, and each evidence group reported separately;
- the step and stage of first pressure and of first management (edit or summary);
- the number of action steps, executions and stage advances that follow first management;
- premature final attempts.

Reports show paired results and per-arm aggregates for all assigned runs, with reasons for any missing or invalid ones. They include concrete traces of what was retained or removed, what the next request contained, and how the final answer used it. Trace-supported explanations are distinguished from causal claims.

## 13. Results export

Completed experiment evidence is copied (never moved) to a separate results repository, one directory per experiment.

**Ownership:**
- **clm-lib** owns experiment *selection*: `experiments/<id>/experiment.json`, with run IDs, roles, settings and provenance notes, but no findings. It also owns the exporter, raw runs and the active ledger.
- **The results repository** owns every human-written document (the root `README.md`, and per experiment `README.md`, `SUMMARY.md` and `protocol.md`), historical protocols, provenance patches and all exported evidence.

**Exporter behaviour:**
- **Human documents:** the exporter never overwrites a human-maintained document. If one is missing, it creates a generic starter (or copies a seed found next to `experiment.json` on a first export) and reports doing so. No experiment-specific findings are embedded in the exporter.
- **Generated files:** `manifest.json`, `report.md`, `metrics.json` and `metrics.csv` are regenerated on every export. The root `EXPORT_FORMAT.md` describes the layout, ownership and checksum policy.
- **Immutable evidence:** run, fixture, comparison and source trees under `artifacts/` each carry `SHA256SUMS`. Re-exporting identical evidence is a no-op, and a different tree under an existing name is refused. For staged runs, fixtures are verified against the stages each run released.
- **Experiment-level checksums:** `experiments/<id>/SHA256SUMS` covers `artifacts/**` and the generated files, and excludes human-maintained documents, so editing a write-up is never mistaken for corrupted evidence. Its existing `artifacts/` entries are re-verified before it is rewritten; a mismatch stops the export.
- **Ledger:** snapshots are historical copies; the active ledger stays in clm-lib.
- **Exclusions:** credentials and request headers are never present or exported. Local account identifiers are redacted and the redaction documented.
- **Corrections:** post-hoc scorer corrections keep both the original and the corrected results.

## 14. Selectable summary-baseline policies

The summary baseline's policy is selected by `[baseline] policy`, and recorded in each run's `run.json` and `summary.json` (`summary_policy`) and in the comparison's frozen block.

- **`fixed-tail/1`** (the default; experiments 001 and 002): at the pressure point, summarise everything except the newest `tail_entries` entries, which are kept verbatim whatever their size. Accept a summary if the request fits the hard limit, with one retry at half the length, and otherwise end with an explicit overflow. Its prompts are unchanged, so earlier configurations keep their meaning.
- **`token-tail/1`** (experiment 003):
  - **Tail:** at the pressure point, keep the longest run of newest entries that fits `tail_ratio` of the budget. The single newest entry is kept even above that allowance if it fits `newest_ratio`; otherwise it is summarised as well.
  - **Summary size:** request a summary sized so the request lands near `target_ratio` of the budget, clamped to `summary_min_chars`–`summary_max_chars`.
  - **Acceptance:** the first attempt is accepted only if it relieves pressure. The retry (at half the length) is accepted if it fits the hard limit. Otherwise the run ends with an explicit overflow.
  - **Summariser:** the same model, with explicit instructions to preserve current versus superseded values, exact values, references and unresolved work.
- **Accounting:** every summary attempt is a normal accounted provider call.
- **Scope:** policies change only the summary arm. The CLM arm and the action protocol are unaffected.

## 15. Coding tasks with changing requirements

- **Generator:** `invoice-gen/1` (`coding.py`). A package skeleton is seeded into the workspace.
- **Stages:** requirements are released over staged `advance` actions. Each stage has an immutable requirements document, listing only changes after stage 1. A read-only visible `unittest` suite is rewritten at each release to match all rules in force, so visible tests never demand superseded behaviour.
- **Action protocol:** coding tasks use a task-kind variant of the protocol text, in which `final` carries `{"summary": ...}`, and a coding-specific summariser instruction for the summary arm. Incident-task prompts are byte-identical to earlier versions.
- **Evaluator-only checks:**
  - **Generation:** they are generated by a host-side reference implementation, cover only disclosed requirements, use inputs different from the visible tests, and stay in host memory until scoring.
  - **Execution:** the submitted workspace copy is evaluated in a fresh sandbox container. Model-written code never runs on the host, and the visible tests play no part in scoring.
  - **Categories:** `retained` (regression), `replaced` (with stale-rule detection) and `final_stage`.
  - **Strict success:** all checks pass and the agent submitted. Checks are components of a task, not independent samples.
- **Ledger ceiling:** it may be changed only by an explicit command with a recorded reason (`ceiling_history`). It is never raised automatically, and opening the ledger with a lower configured ceiling still lowers it.

## 16. Coding tasks with interacting, partially changing rules

- **Generator:** `invoice-gen/2` (`coding2.py`, reference rules in `invoice_ref2.py`). `invoice-gen/1` stays unchanged and reproducible.
- **Workload levels:** a base workload (`coding2-*`, 6 stages) and a predefined harder variant (`coding2h-*`, the same schedule plus 2 stages), both defined before any calibration.
- **Rules:** rules interact across the calculation, and a CHANGED rule may replace only part of an earlier rule. It names the stage it modifies; the parts not mentioned stay in force. Requirements stay precise and rereadable. Visible tests are rewritten at each stage for every rule in force.
- **Disclosure:** checks and visible tests use only fields and functions disclosed by that stage.
- **Evaluator `invoice-checks/2`:**
  - It keeps every §15 guarantee: host-side generation, inputs different from the visible tests, scoring in a fresh sandbox, and no host execution of model code.
  - **Categories:** `retained`, `replaced` (with the old rule's result recorded, so stale-rule output is distinguished from other failures), `kept_part`, `interaction` and `final_stage`, plus a `boundary` flag.
  - **Exact types:** return types and keys are checked exactly; a missing function or a wrong type fails.
  - **Wording:** a failure of a retained rule is a *retained-rule failure*. It is a *regression* only with evidence that the rule worked earlier.
  - **Snapshots:** on each `advance`, the runtime copies the workspace host-side, outside every mount. After the run, each copy is scored against hidden checks for the rules then in force.
  - **Validation:** the evaluator is validated offline against an independently written correct implementation and deliberately faulty ones: forgotten and outdated rules, over-applied partial changes, calculation-order mistakes and interaction mistakes.
- **Comparison order:** `compare --order balanced` alternates the first arm across instances and across repetitions within an instance. The default `pair` order is unchanged.
- **Scorer metadata:** the comparison `frozen.scorer_version` and the export manifest's `exported_with.current_scorer` report each task's own scorer. A selection mixing scorers gives a sorted list.

## 17. Request layouts, prompt caching and run isolation

- **Layouts are versioned** (`request.layout`, recorded in every run, export row and report):
  - `single-user/1`: one system string; one user string with `<task>`, `<runtime_status>` and `<working_context>`, in that order. It is the default and is used by every earlier configuration, whose payloads must stay unchanged.
  - `blocks/1`: text content blocks.
    - **System:** the run tag (when enabled), then the system prompt.
    - **User message:** the task block (which also opens `<working_context>`), one block per context entry, and the runtime-status block last.
    - **Summary requests** use the same convention, with the transcript to summarise as entry blocks.
    - **Repairs** append their notice as a final block.
- **Prompt caching** (`request.prompt_caching`, or set per run) is allowed only with `blocks/1`.
  - **On:** an explicit `cache_control` with the configured TTL goes on the task block and on the last entry block of every action, repair and summary request. Nothing after them is marked.
  - **Off:** no `cache_control` is sent, and the payload is otherwise identical.
  - **No padding:** prompts are never padded to reach the provider's minimum cacheable length.
- **Run isolation** (`request.run_isolation = "run-tag/1"`): every request in a run begins with a fixed-length system block holding 32 random hex characters. The tag is generated per run, is stable within the run and carries no task information. Because cache matching is an exact prefix match, no run can read another run's entries.
- **Accounting:**
  - Cost prices uncached input, 5-minute and 1-hour cache writes, cache reads and output separately; `input_tokens` excludes cached tokens.
  - Reservations assume the dearest applicable input-side rate.
  - Request-size budgeting, pressure and the response bound check use total input including cached tokens.
- **Evidence per request:**
  - the saved request's `meta.prefix` (common leading blocks with the previous request of the same family, the first changed block, breakpoints);
  - `latency_s` and the cache usage on each response;
  - per run, `summary.json → request` (settings, run tag, first-call cache usage, usage, cost and latency per call kind).
- **Four-condition comparisons:** `compare --conditions summary:off,summary:on,clm:off,clm:on` runs each instance and repetition block in Williams order. Each condition appears equally often in each position, and the schedule is written into the frozen block.

## 18. Multi-round incidents and reusable context-management helpers

- **Task family `incident-rounds-gen/1`** (`rounds.py`, kind `rounds`):
  - **Evidence:** 10–12 hourly evidence rounds released by `advance` under `round-NN/`: service logs, incident-board updates and change records. Rounds never change and stay rereadable.
  - **Answer contract:** a final answer lists incidents (cause, services, status, evidence references) and unresolved follow-up codes.
  - **Scorer `rounds-score/1`:** deterministic checks for causes (with superseded and false-alarm detection), services, statuses (with stale detection), evidence groups including early change records, and open follow-ups.
- **CLM capability variants** (modes `clm_direct`, `clm_helpers` and `clm_reuse`):
  - **Shared text:** the CLM instructions without the reusable-helper sentence, plus one clause. `clm_direct` asks the model not to save context-management routines for later steps; `clm_helpers` permits saving, calling and revising them; `clm_reuse` (the revised experiment 007) instructs the model to perform its context edits through a reusable module it writes, while it still decides what to keep and when to edit.
  - **Unchanged modes:** `clm` and `guided` keep their original text.
  - **No runtime involvement:** the runtime never generates, schedules or invokes model-written code.
- **Summary-call allowance:** `limits.summary_calls_in_max_calls = false` counts only task calls against `max_calls` and caps summary calls separately (`max_summary_calls`). The default (true) preserves earlier experiments.
- **Helper evidence** (`helper_audit.py`):
  - **Tiers:** created, executed, repeated accepted edits (at least two steps, confirmed in the next request), and substantive repeated logic (a static classification of the writing function, distinguishing wrappers).
  - **Reuse findings** for saved context-management code reused in later steps, used to check `clm_direct` compliance.
  - **Export:** tiers are exported per run.
- **Spill fix:** `limits.spill_past_receipt = true` lets the spill rule move an oversized observation followed by its edit receipt. The default (false) keeps the original rule. It was found in experiment-007 calibration.
- **Pressure-notice fix:** `limits.recheck_after_notice = true` re-checks the request after the runtime adds its CLM pressure notice. A request the notice pushes over the hard limit then gets the spill and recovery handling, instead of ending the run. The default (false) keeps the original behaviour. It was found in the revised experiment-007 calibration.
- **Phases:** an experiment's runs may carry a `phase` label (and comparisons a `comparison_phases` entry) in `experiment.json`. The exporter copies it into the manifest and metrics rows, and the report labels runs and comparisons with it, so phases are never pooled.
- **Helper categories:** for each helper function, the audit reports whether it writes supplied note content, selects, removes or reorganises existing entries, applies more substantial logic, or only reads. It also reports per-run adherence for `clm_reuse` (full, partial or none, by whether accepted edits happened in steps where saved context-management code executed) and for `clm_direct`.
- **Three-condition schedules:** `compare --conditions` with three conditions uses the six permutations in turn, one per block, and records the realised order balance in the frozen block.

## 19. Optional host-mediated context review (TypeSafe Jev)

- **Opt-in:** `[jev] enabled` (default false), or the per-run condition `<clm mode>+jev`, adds the `review_context` action to a CLM run's action schema and system prompt (`prompts.JEV_CLAUSE`). Every earlier configuration and prompt is unchanged. Summary modes cannot enable it.
- **Action:** `{"action": "review_context", "revision": N, "entry_ids": [...], "update": "..."}`, or `"update_entry_id"` in place of `"update"`.
  - **Validation:** the revision must be the **current** context revision (the one the runtime status shows); a stale or unknown revision is rejected. The ids and any `update_entry_id` must be in that revision, with no duplicates and within `max_entries`; exactly one non-empty update within `max_update_chars`; and within `max_reviews` per run.
  - **Boundary:** entries the agent has deleted remain in host-side history but are never sent to Jev. Validation runs before any ledger reservation or Jev request. (Earlier unreleased drafts accepted any existing revision; this was found in an offline check on 2026-10-12, before any experiment-008 run, and fixed.)
  - **Rejections** return a notice and make no Jev call.
- **Request:**
  - **Where it runs:** the host runtime, never the sandbox, calls `POST {jev.base_url}` with the pinned `jev.model`.
  - **State and questions:** the state is the update, with one `noul` question per entry, batched up to `batch_size`. Each question carries the fixed question `jev-review/1` with criteria, plus the exact entry text from the current revision.
  - **Never sent:** no other content (future stages, evaluator truth, audit history).
- **Advice:**
  - **Observation:** one probability per submitted entry ("no answer" when missing or malformed), plus coverage and any service problem.
  - **No effect on context:** the action changes no entry; only the usual runtime append of the step's action and observation happens.
  - **Failures:** a failed or malformed call leaves the run able to continue.
- **Accounting:**
  - **Ledger:** every Jev attempt is a ledger entry (kind `jev`, provider `typesafe`), reserved before dispatch and settled from reported usage at the dated `[typesafe."<model>"]` price.
  - **Retries:** only the configured `jev.retries` happen (429, 529, 5xx, timeouts), with no hidden SDK retries.
  - **Evidence per run:** `jev/NNNN.json` (request body without headers, response, latency, usage, cost); `summary.json → jev` (reviews, attempts, errors, retries, TypeSafe tokens kept apart from Anthropic tokens, cost, latency, and per-review answers with later accepted edits by entry id); and `cost.by_provider`.
- **Credentials:** `TYPESAFE_API_KEY` is read from the host environment only, and never logged, saved or passed to the sandbox.
- **Status line:** `limits.status_shows_revision` adds the current context revision number to `<runtime_status>`, for both arms of a comparison.
- **Diagnostic:** `clm-lib jev-diagnostic` runs labelled (update, entry) cases through the same request builder. It reports flags and misses at a reporting threshold fixed in the case file; that threshold is never an editing policy.
- **Limitation:** sandboxed helper code cannot call Jev. Advice is available only as a separate action whose result is used in a later step.

## 20. Update-triggered context review (experiment 009, strategy 1)

- **Opt-in:** `[update_review] enabled` and `advice` (both default false), or the per-run conditions `<clm mode>+upd` (control) and `<clm mode>+upd-jev` (assisted). It is available to CLM modes on `rounds` tasks only. It is exclusive with the §19 action, which stays off. Every earlier configuration, prompt text and release observation is unchanged.
- **Trigger:** each release of rounds 2…n through `advance`.
- **Payload (`round-files/1`, `update_review.build_payload`):**
  - **Content:** the released round's `board.md` lines, then its `changes.md` lines, verbatim, each prefixed `round-NN/<file>:<line>:`.
  - **Processing:** blank and `#` heading lines are dropped. A payload over `max_payload_chars` is cut at a line boundary with a marker.
  - **Never included:** logs. No other processing is applied.
- **Presentation (both arms):**
  - **The release observation** keeps its historical text and gains an `UPDATE (round-files/1; context revision R was current at this release)` block with the payload and "Consider whether this update affects your working context." An empty payload is reported as "No incident-board or change-log lines were released this round."
  - **The system prompt** gains `UPDATE_REVIEW_CLAUSE`; the assisted arm also gains `UPDATE_REVIEW_JEV_CLAUSE` (prompt version `2026-10-13.1`).
- **Advice (assisted arm):**
  - **When:** before the next main-model call, on the host, inside the advance step. It costs no extra model turn.
  - **Candidates:** every entry of the current revision (the context shown in that step's request), in order. At most `max_candidates` are scored, and any beyond are listed as "not assessed".
  - **Questions:** one `noul` question per candidate (`jev-review/1`), with the payload as `state.update`. Batches hold up to `[jev] batch_size` questions and `max_batch_chars` entry characters.
  - **Observation:** the advice is appended in the §19 format. Failures give "no answer" plus a service notice, and the run continues.
  - **Unchanged:** no context entry is changed, and no edit is approved or rejected.
  - **Not called:** for an empty payload or an empty context.
- **Accounting and evidence:**
  - **Ledger:** Jev attempts use the §19 ledger path (kind `jev`), and `summary.json → jev.source` is `update_review`.
  - **`summary.json → update_review`:**
    - **Per update:** step, stage, revision, the payload and its SHA-256, sources, candidate and not-assessed ids, status, answers, models, errors, attempts, Jev tokens, cost and latency, appended and advice characters.
    - **Later accepted edits:** before the next update, with candidates changed, flagged then changed, flagged not changed, and unflagged then changed. These are temporal association only.
  - **Exports:** `ur_*` columns.
- **Diagnostic instances:** `rounds-diag-1..2` (seeds 7301–7302) are reserved for diagnostic case text and smoke runs.

## 21. Claude-initiated context review (experiment 010)

- **Opt-in:** `[initiated_review] enabled` and `advice` (both default false), or the per-run conditions `<clm mode>+rev` (control) and `<clm mode>+rev-jev` (assisted).
  - **Scope:** CLM modes only. It is exclusive with §19 and §20.
  - **Unchanged:** every earlier configuration, prompt text and action schema.
- **Action:** both arms get `{"action": "begin_review", "new_information": "<text>"}` (with an optional `thought`), added to the action schema, plus `INITIATED_REVIEW_CLAUSE` (prompt version `2026-10-13.2`). The assisted arm also gets `INITIATED_REVIEW_JEV_CLAUSE`.
- **Status line:** `context reviews used k/max_reviews`.
- **No other path changes:** editing with `execute` remains possible at any time, and no review is triggered automatically.
- **Validation:** at most `max_reviews` per run, and a non-empty `new_information` of at most `max_info_chars`. A rejection returns a notice, makes no Jev call, and does not consume the limit.
- **Response (same step, no extra model call):**
  - **Candidates:** the entries of the **current** revision whose role is in `candidate_roles` (default note, observation and summary; step actions and receipts excluded), in order, at most `max_candidates`, with any beyond listed.
  - **The observation** states the revision, the new information as given, the candidate ids and the excluded count, and that the context is unchanged.
  - **Assisted:** Jev scores the candidates with `jev-review/1`, with `state.update` = the new information, and the advice is appended in the §19 format. Failures give "no answer" plus a service notice.
  - **Never:** no entry is changed, and no edit is approved or rejected.
- **Evidence:**
  - **`summary.json → initiated_review`:**
    - **Totals:** reviews, rejections, reviews before the last stage, advised and failed reviews, entries listed and scored, observation and advice characters, and Jev attempts, tokens, cost and latency.
    - **Per review:** step, stage, revision, the new information, candidate, excluded and not-assessed ids, answers, and accepted edits until the next review. It also records `edited_next_step`; candidates changed; flagged then changed, flagged not changed, and unflagged then changed (temporal association only); and `observation_requests_carried`, the number of later requests whose context still held the review observation.
  - **Also:** `initiated_review` events, `jev.source = initiated_review`, and `ir_*` export columns.
- **Required pre-edit review (`initiated-review/2`, experiment 010 v2):** `[initiated_review] required` (default false), or the conditions `<clm mode>+pre` (review, no Jev) and `<clm mode>+pre-jev` (review with Jev).
  - **The action:** `begin_review` takes `purpose`, which is `update` (with 1–`max_info_chars` of `new_information`) or `compaction` (no new information).
  - **Prompt clauses:** `INITIATED_REVIEW_V2_CLAUSE` and `INITIATED_REVIEW_V2_JEV_CLAUSE` (prompt version `2026-10-13.3`).
  - **What a valid review issues:** one edit authorisation, bound to the bodies of every entry of the reviewed revision.
  - **How an authorisation ends:**
    - **consumed** by the first accepted, content-changing edit;
    - **replaced** by a new valid review;
    - **expired** at the next stage release;
    - **invalidated** if a reviewed entry's body changed;
    - **unused** at the end of the run.
  - **What does not end it:** runtime appends of later steps' entries.
  - **The gate:** `ContextStore.read_back(authorise=...)` checks every valid, content-changing `context.json` candidate before commit. Without a valid authorisation the candidate is rejected through the normal rejection path, keeping the previous revision and saving the rejected bytes. Unchanged writes need no authorisation. Model code runs once, and its other effects are not rolled back.
  - **Recovery exemption:** while an over-limit recovery request is active, an edit needs no authorisation, and is counted as `recovery_exempt`.
  - **Jev (with `+pre-jev`):** it scores candidates only for `update` reviews. A `compaction` review records `not_applicable`, makes no call, and still issues the authorisation.
  - **The status line** shows the authorisation state.
  - **Evidence:**
    - **`summary.json → initiated_review`:** `required`, `version`, reviews by purpose, `reviews_not_applicable`, `edits_blocked` with steps, accepted edits that were authorised, recovery-exempt or unauthorised, and the `authorisations` with their outcome counts.
    - **Per review:** `authorisation_outcome` and `authorised_edit_step`.
    - **Also:** `edit_log` entries carry `authorised_by_review_step`, and the v2 export columns are `ir_version` through `ir_auth_unused`.
- **Export groups:** a run in `experiment.json` may carry `"group": "<name>"` (lowercase letters, digits and dashes). It is then exported to `artifacts/runs/<group>/<role>/<run_id>/`, so protocol versions of one experiment never share a folder. Runs without a group are exported as before.
- **Runtime-supplied review update (`initiated-review/3`, experiment 010 v3):** `[initiated_review] update_source = "latest-round"` (default `stated`), or the conditions `<clm mode>+pre3` and `<clm mode>+pre3-jev`.
  - **Scope:** it requires the required pre-edit review (as above), on `rounds` tasks only.
  - **The action:** `begin_review` takes an optional `purpose` and `new_information`, which are recorded only. Only over-long notes and the review limit are rejected.
  - **The update:** the latest released round's `round-files/1` payload (§20 construction, cut at `V3_PAYLOAD_MAX_CHARS` = 4,000). The review material names it (round, files, line count, SHA-256 prefix) but does not quote it.
  - **Jev (with `+pre3-jev`):** it is called whenever the payload is non-empty and candidates exist, whatever the label. Otherwise the review records `not_applicable` with `empty_update` or `no_candidates`.
  - **Repeat reviews:** a repeat review of a round calls Jev again (`round_review_index`).
  - **Unchanged:** authorisation, the gate, the recovery exemption and the candidate rule.
  - **Prompt clauses:** `INITIATED_REVIEW_V3_CLAUSE` and `INITIATED_REVIEW_V3_JEV_CLAUSE` (prompt version `2026-10-13.4`).
  - **Evidence:**
    - **Per review:** `update_source`, `update_round`, `payload`, `payload_sha256`, `payload_lines`, `payload_truncated`, `applicable` and `not_applicable_reason`.
    - **Summary:** `reviews_applicable`, `reviews_not_applicable_by_reason`, `reviews_repeat_round` and `reviews_by_label`.
    - **Export:** the `ir_update_source` columns.

## 22. Edit-check component diagnostic (experiment 011)

**`clm-lib jev-edit-diagnostic --cases <file> --split dev|frozen`** sends one TypeSafe request per case, and makes no main-model calls.
- **`state`:** `task`, `current_context`, `update` and `proposed_context` (the entries with `id`, `role` and `body`, in order). Nothing else from the case file is sent: no labels, categories, families, provenance or justifications.
- **The questions (`jev-edit-check/1`, `jev_edit_check.QUESTIONS`):** three `noul` questions, `lost`, `changed` and `evidence`, each with fixed criteria and a shared scope instruction.
- **Verdict:** a case is flagged if any probability is at least the case file's `reporting_threshold` (0.5).
- **Missing answers:**
  - a case with every answer missing, or a failed call, is unanswered;
  - for the gate, an unanswered harmful case counts as missed and an unanswered acceptable case as flagged;
  - a partial case uses the available maximum.
- **Attempts:** each attempt is reserved and settled in the ledger (kind `jev-diagnostic`), with `[jev] retries` for retryable errors.
- **Deterministic reference checks:**
  - **reference loss:** `file:line`, `§N`, `ABC-123` or `[Author Year]` tokens lost;
  - **new numbers:** numbers absent from the current context and the update.
- **Output:** `runs/jev-diagnostic/<stamp>-edit-<split>/`. It holds `requests/<case>.json` (request, response, latency, usage, cost) and `summary.json`, with per-case, per-question and per-category metrics, the references, and the gate when the split has 16 harmful and 16 acceptable cases. It is exported like other diagnostics.
- **v2 (selected when the case file's `version` is `jev-edit-diag/2`).** v1 files keep v1 behaviour, and their output directories, questions and scoring are unchanged.
  - **Evidence (`edit-evidence/1`, `select_evidence`):** the state has `task`, `current_context`, `update`, `execution_output`, `cited_sources`, `evidence_notes` and `proposed_context`.
    - **`current_context`:** all entries of the current revision, each cut at 6,000 characters (proposed entries are cut the same way).
    - **`update`:** cut at 4,000 characters.
    - **`execution_output`:** the proposing execution's stdout, cut at 6,000 characters.
    - **`cited_sources`:** for each `path:lines` reference that appears in the proposal but not in the current context, the numbered lines from the case's released `sources`. Paths resolve by exact match or a unique path suffix. Each reference gets at most 20 lines, each line at most 300 characters, and 8,000 characters in total.
    - **`evidence_notes`:** every truncation, missing execution output and unresolved or ambiguous reference.
    - **Never sent:** whole files, deleted revisions, or anything else from the case.
  - **Questions (`jev-edit-check/2`, `QUESTIONS_V2`):**
    - **`lost`, `changed`, `evidence`:** support may come from any supplied material, and `changed` treats removal as judged by `lost`.
    - **`insufficient`:** whether the supplied material is insufficient to check the new or changed content.

    `summary.json` records `questions_sha256`.
  - **Verdict (`edit-verdict/2`):**
    - *flagged* if any of `lost`, `changed` or `evidence` reaches the threshold;
    - else *uncertain* if `insufficient` does;
    - else *clear*;
    - *unanswered* if no answer came back.

    An uncertain or unanswered harmful case is not a detection, and an acceptable case that is not clear needs inspection.
  - **Gate** (16 harmful and 16 acceptable cases): harmful flagged ≥ 14, acceptable flagged ≤ 2, and acceptable clear ≥ 12. Cases labelled `insufficient` or `ambiguous` are reported separately.
  - **Deterministic references:** the new-number check accepts numbers from the same supplied evidence.
  - **Output:** `runs/jev-diagnostic/<stamp>-edit2-<split>/`.
- **Not implemented in §22:** an agent-side edit check, edit holding or blocking (see §23 for the agent pilot's opt-in proposals).

## 23. W2 workload and proposed-edit checking (experiment 011 agent pilot)

### 23.1 W2 changing-requirements workload (`export-gen/1`, `w2.py`)

- **Task:** implement `exporter.export_records(records, user, params) -> str` in a seeded package skeleton. Requirements arrive in 5 staged `advance` releases (`w2-dev-1`, `w2-dev-2`; `w2-smoke-1` has 2 stages and an explicit smoke note, and is for integration smoke only).
- **Rules:**
  - `FORMAT` (CSV, then JSON in stage 3; which records are exported is unchanged);
  - `PERM` (viewer raises `PermissionError`, admin sees all, others see their projects);
  - `STATUS` (active only unless `include_archived`);
  - `CONF` (confidential records never exported, except for an admin with `purpose == "audit"`);
  - `SINCE` (date filter);
  - `OBLIG` (stated in stage 2: if the format leaves CSV, include `exported_by`);
  - `LIMIT` (stage 5: `max_records` and `truncated`).

  One instance changes `PERM` in part in stage 4 (members also get the records they own) and the other `STATUS` (`include_archived` honoured only for admins). A CHANGED rule names the part it replaces.
- **Visible material:** immutable, rereadable `stage-N/REQUIREMENTS.md` (each stage's text is also the release observation), and `current-tests/test_stage.py`, replaced at each release. The visible tests cover only the latest stage's new or changed rules, and the task prompt says so.
- **Evaluator (`export-checks/1`):**
  - **Generation:** host-side reference checks on inputs different from the visible tests, using only disclosed options. The checks stay in host memory until scoring and are scored in a fresh sandbox.
  - **Categories:** `retained`, `exception`, `replaced` (with stale-version detection, including raw CSV text), `kept_part`, `deferred`, `final_stage` and `interaction`.
  - **JSON checks** compare the parsed values of named top-level keys.
  - **Snapshots** score the workspace at each release, and regressions need an earlier passing snapshot, as in §16.
  - **Validation:** offline, against an independent correct solution and deliberately faulty ones (tests only).

### 23.2 Proposed-edit checking (`edit-check-pilot/1`)

- **Opt-in:** `[edit_check] enabled` / `advice` (default false), the per-run `edit_check` (`off`, `control` or `jev`), the conditions `<clm mode>+chk` and `<clm mode>+chk-jev`, or `run --edit-check`.
  - **Scope:** CLM modes only, exclusive with §19–§21.
  - **Unchanged when off:** every earlier configuration, prompt text, action schema and the direct-edit path.
- **Proposals (`control` and `jev`):**
  - `ContextStore.read_back(hold=True)` validates the workspace `context.json` exactly as before. A valid, content-changing candidate is returned as `proposed` and is **not committed**.
  - The runtime records proposal `P<n>` (base revision and hash, candidate hash, removed, added and rewritten ids, counts), saves `context/proposal-P<n>.json`, and appends the step's action, observation and a receipt describing it.
  - It mirrors the live context to `context.json` and the candidate to the informational `proposed_context.json`. Acceptance uses the host-held candidate, and the file is excluded from submissions and snapshots.
  - Model code runs once.
- **Decisions:** `{"action": "confirm_edit", "proposal": "P<n>", "decision": "accept" | "withdraw"}`.
  - **Accept** (`ContextStore.accept_proposal`): commits the candidate followed by every entry appended since the base revision (ids renamed on collision). It is refused if the live context differs from the base in any other way (stale) or if the merged result fails validation.
  - **Withdraw:** discards the proposal. The observation says other file changes remain.
  - **A wrong or absent id** changes nothing (`confirm_invalid`).
- **Superseding:** a new content-changing write supersedes a pending proposal (`revised`), and is revalidated and, with `jev`, rechecked.
- **Never applied by:** unrelated executions, unchanged writes, `final` (`unresolved_at_final`) or the run's end (`unresolved_at_end`). A stage release discards the pending proposal (`expired_at_stage_release`) and says so.
- **Recovery exception:** while an OVER LIMIT recovery request is active, a write is applied directly (`recovery_exempt_applied`; a pending proposal becomes `superseded_by_recovery_edit`), and its receipt says so. In that state the OVER LIMIT status line also names a still-acceptable pending proposal and the body characters it would leave.
- **Status line:** `context proposal: P<n> pending (...)` or `none pending`.
- **Prompt clauses:** `EDIT_CHECK_CLAUSE` (both) and `EDIT_CHECK_JEV_CLAUSE` (`jev`), prompt version `2026-10-13.5`.
- **Jev check (`jev`):** `jev-edit-check/2` via `build_edit_request_v2` on `edit-evidence/1`, unchanged. The case is built from runtime inputs only:
  - `task` (the task prompt);
  - `current` (the live revision at the proposal);
  - `update` (the current stage's release text);
  - `exec_output` (the proposing execution's stdout);
  - `sources` (released fixture files up to the current stage);
  - `proposed` (the candidate).
  - **Calls:** one call per proposal, with the `[jev]` retries; every attempt is in the ledger.
  - **Verdict** at `[edit_check] threshold` (0.5): `flagged` (any harm answer ≥ threshold), `incomplete` (a missing answer), `uncertain` (`insufficient` ≥ threshold), `clear`, or `error`.
  - **Beyond `max_checks`** (30): `not_checked`.
  - **Presentation:** the receipt adds the four probabilities, the result and the evidence notes, with fixed caveats. Errors and the limit are explicitly "not a clear result". **Nothing is rejected automatically.**
- **Evidence:**
  - **`summary.json → edit_check`:** mode, versions, threshold, proposals by status, recovery-exempt edits, confirm actions and invalid ones, checks, verdicts, reactions by verdict, flagged or uncertain proposals that were accepted, requirement reads and test runs (all conditions), and the proposal and confirm logs.
  - **Files and events:** `edit_check/P<n>.json` (the exact request and answers), `context_proposal`, `edit_check` and `confirm_edit` events, and `ec_*` export columns.

## 24. Handbook search workload and search-result screening (experiment 012)

### 24.1 Handbook workload (`handbook-gen/1`, `handbook.py`)

- **Instances:** `handbook-dev-1` and `-dev-2` (pilot), `handbook-smoke-1` (2 questions, smoke only) and `handbook-diag-1..4` (component diagnostic only). The task kind is `handbook`, unstaged.
- **The handbook:** 40 markdown documents under `/task/fixtures/handbook/`. Each has a header line (version, status, effective dates, supersedes or superseded by) and numbered sections. A section is a passage, cited `HB-07 §2`.
- **Subjects:** 8 policy subjects. Each has a current policy, a superseded version, an exception memo with a carve-out, a related-budgets distractor, and an FAQ entry restating the current standard value.
- **Questions:** 6 per instance, of the types `current`, `past`, `exception`, `carveout` and `additive`. Numeric answers have one or more valid support sets; some sets need 2 passages.
- **Tools:** `/task/fixtures/handbook_tools.py`, identical in every condition:
  - `search(query, k=5)` (k ≤ 8) uses deterministic stdlib BM25 over passages. It prints a structured block (`<<search-result SR-NNN query=... k=... returned=... saved=...>>`, then `=== <id> | <title> — <heading> | <meta>` and the passage text, then `<<end search-result SR-NNN>>`) and saves the original (`.txt` and `.json`) under `/task/workspace/search-results/`.
  - `show(ids)` prints named passages as `<<passages (unscreened): ...>>`, a format that is never screened.
  - Documents stay directly readable.
- **The final answer:** `{"answers": [{"question", "value", "citations"}]}`.
- **Scorer (`handbook-checks/1`, host side):** a question is correct when the value matches numerically **and** the citations include a complete valid support set. Citation ids are normalised (`HB-7#2`, `HB-07 section 2`, …). A superseded value is counted `stale`; citations outside every support set are counted. Strict success means every question is correct.

### 24.2 Search-result screening (`result-screen/1`, `jev_screen.py`)

- **Opt-in:** `[result_screen] enabled` (default false), the per-run `result_screen` (`off` or `jev`), the condition `<clm mode>+scr`, or `run --result-screen`.
  - **Scope:** handbook tasks only, exclusive with §19–§21 and §23.
  - **Unchanged:** ordinary CLM editing and every other task kind.
- **Presentation, both conditions:** after an execution, structured search blocks in stdout are parsed. A block cut by the existing 6,000-character output cap is cut back to its last complete passage, with a notice naming the passages not shown (from the saved original). No other output is changed. Each block's passages are checked against the saved original (provenance).
- **Screening (`jev`):**
  - **Eligible:** a complete block with ≥ 3 passages and ≥ 2,000 presented characters, whose provenance matches.
  - **The request:** one Jev request per block (`build_screen_request`). `state` holds the task (≤ 4,000 characters), the query, the step's stated `thought` (≤ 500 characters) and every passage (id, source header, text). There is one `noul` question per passage, carrying that passage's id and full text, with the frozen `QUESTION` and `SCOPE`.
  - **Selection:** keep a passage at probability ≥ `[result_screen] threshold` (0.5), and at least `min_keep` (2), highest first, in original order. A missing answer keeps its passage.
  - **Rendering:** omitted passages are removed, and the fixed notice `[result-screen/1: kept k of n passages (c of t chars); omitted ...; unscreened: show([...]) or <saved path>]` (or `all n passages kept`) is added.
  - **Service error** (after `[jev] retries`): the block is shown unscreened with `screening unavailable; nothing omitted`.
  - **Recording:** every attempt is in the ledger and `jev/`, and every screened block in `result_screen/<step>-<id>.json`.
- **Never screened:** `show()` output, saved originals, documents, stderr and any other output, so recovery reads are always unfiltered.
- **Recovery accounting (both conditions):** `show()` calls and their ids (with any previously omitted ids recovered), saved-file reads, and direct document reads (code touching `fixtures/handbook`).
- **Evidence:** `summary.json → result_screen` (blocks, eligible, screened, errors, provenance, cap repairs, passages and characters in, kept and omitted, missing answers, notice characters, recovery counts, block and recovery logs), and `rs_*` export columns.
- **Component diagnostic:** `clm-lib jev-screen-diagnostic --cases <file> --split dev|frozen` (`jev_screen_diag.py`) applies the same request and selection to labelled cases (TypeSafe only). It reports support-passage recall, complete-support-set survival, the omitted share by label, the term-match reference selector and the gate.

### 24.3 Document-service workload (`docsvc-gen/1`, experiment 012 v2)

- **Instances:** `docsvc-dev-1` and `-dev-2` (v2 pilot), `docsvc-smoke-1` and `docsvc-diag-1..3` (compatibility diagnostic only). The task kind is `docservice`.
- **The corpus** (`docservice.py`): 74 documents in host memory only, with 16 policy subjects (overlapping vocabulary), two FAQ documents and general documents. Sections carry subject-specific procedural sentences.
  - **What reaches the sandbox:** the fixture mount holds only `SERVICE.md`. Nothing of the corpus is written to the mount or the workspace except returned results and saved copies of search results. This is the declared service boundary, identical in every condition.
- **Questions and scorer:** as §24.1 (`handbook-checks/1`); answer documents are not named in the task.
- **Actions** (`SERVICE_ACTION_SCHEMA`, `parse_action(allow_service=True)`, `_Run.service_step`):
  - `{"action": "search", "query": str (≤ 300), "k": 1..8 (default 5)}` ranks passages with the §24.1 BM25 (`DocService`, host side). It writes `/task/workspace/search-results/SR-NNN.{json,txt}` (the runtime counts the ids) and returns the §24.1 structured block, capped at `limits.output_cap_chars` with whole passages only.
  - `{"action": "fetch", "ids": [...]}` (≤ 10 passage or document ids; a document id returns all its sections) returns `<<passages (unscreened): ...>>`, capped with whole passages and a notice.
  - Each action is one step, with an action entry and an observation entry appended by the runtime.
- **Screening:**
  - **What is screened:** only `search` observations, through `jev_screen.screen_stdout` with the recovery verb `fetch`. Its notice reads `unscreened: fetch([...]) or <saved path>`, and condition B's disclosure is `SEARCH_SCREEN_SERVICE_CLAUSE` (prompt version `2026-10-13.7`).
  - **Never screened:** `execute` output in `docservice` tasks, fetches and saved-file reads.
  - **Accounting:** `summary.json → result_screen.service` counts searches, fetches, fetched ids and omitted passages recovered by fetch.

## 25. Jev as a primitive inside model-written code (`jev-primitive/1`; experiment 014)

- **Purpose:** model-authored code (including saved, reusable helpers) can ask Jev narrow yes/no questions **during its own execution** and then apply its own logic to edit `context.json` in that same execution.
  - **No model turn** is used to request or interpret the answer.
  - **The host never edits context on Jev's behalf.**
- **Opt-in:** `[jev_primitive] enabled` (default false), `run --jev-primitive on`, or the condition `<clm mode>+jprim`.
  - **Scope:** CLM modes only, exclusive with §19–§24's Jev features. It requires a Jev advisor.
  - **Unchanged when off:** every other behaviour, prompt and executor path.
- **Sandbox interface:** `/task/workspace/clm_jev.py`, written by the runtime at run start (stdlib only).
  - **The call:** `clm_jev.judge(question, items, background="", criteria=None, timeout=25.0)`, where `items` maps keys to texts.
  - **The result:** `{"status": "ok"|"error", "answers": {key: probability|None}, "model", "error"}`.
  - **The transport:** the call writes `req-<id>.json` into the workspace mailbox `/task/workspace/.clm-jev/` and polls for `resp-<id>.json`.
- **Host side** (`jev_primitive.JevBroker`):
  - **Polling:** `DockerExecutor.run(..., broker=)` polls the broker between 0.05 s waits while the container runs; the overall deadline and kill path are unchanged.
  - **Validation:** each request is checked against the documented fields (`version`, `id`, `question` ≤ 500, `background` ≤ 4,000, 1–20 `items` of `key` ≤ 64 and `text` ≤ 4,000, optional `criteria`; ≤ 100,000 bytes), as a regular file with no symlinks.
  - **The Jev request:** `build_body` makes one TypeSafe request: `state` = {background, items}, with one `noul` question per item (the code's question, the item text, the fixed `SCOPE`, the code's criteria or defaults).
  - **The call** goes through the runtime's recorded attempt loop: `[jev]` retries, one ledger reservation and settlement per attempt.
  - **The response file** is written atomically.
  - **Limits:** `max_calls_per_execution` (4) and `max_calls_per_run` (30). Over-limit, invalid or budget-refused requests get an explicit error response without a Jev call.
  - **Budget exhaustion during an execution** answers the request with an error; the step is recorded, then the run stops.
- **After each execution:** unanswered requests (the execution ended first) are recorded as `unanswered_at_exit`, with no Jev call, and the mailbox is emptied.
- **Information boundary:**
  - **Jev receives only the text the code sends**, which the code can already see (the context file, released fixtures, the workspace).
  - **The host adds nothing:** it reads no files on the code's behalf, never executes request content, and exposes no truth, future stages, deleted revisions or host files.
  - **The sandbox keeps** no network and no credentials.
- **Status line:** `jev primitive calls used k/max`.
- **Prompt:** `JEV_PRIMITIVE_CLAUSE` (prompt version `2026-10-13.8`) describes the call and says its use is optional.
- **Evidence:**
  - `jev_primitive/NNNN.json` per request (the sandbox request, the TypeSafe body, the response, status, host time);
  - `jev/NNNN.json` per attempt;
  - `jev_primitive` events;
  - `summary.json → jev_primitive` (requests, calls, by status, items, unanswered at exit, steps with calls, log).
- **Excluded** from coding submissions and snapshots: `clm_jev.py` and the mailbox.
- **Per-run extra instruction** (for configured demonstrations): `run --extra-instruction-file FILE` appends the file's text to the task prompt for that run only. The text is recorded verbatim in `run.json → extra_instruction` (`null` when absent), so a run that was told to use a capability is always labelled as such. Without the option, prompts are unchanged.

## 26. W3 register workload and replay attribution (experiment 014 comparison phase)

- **Workload** (`w3.py`, generator `export-gen/2`, code scorer `export-checks/2`): W2's records-export task extended to 7 stages (smoke: 5). Stages 1–2 are initial requirements and stages 3–7 change notices; every item has a visible id `S<stage>.<n>`, and notice items do not name the items they affect.
  - **Relationships:** full replacements, partial amendments (including an amendment of an amendment), a withdrawal, an exception that survives, two obligations triggered by later items without being restated, decision records and information items.
  - **Instances:** `w3-smoke-1` (8701), `w3-dev-1` (8501), `w3-dev-2` (8502), and `w3-dev-3` (8503) and `w3-dev-4` (8504), which are fresh seeds of the dev-1 and dev-2 schedules. No evaluation instances are defined.
  - **Code scoring:** W2's sandbox evaluator and score structure, with the categories retained, exception, replaced, amended, kept_part, deferred and interaction, plus snapshots at each release.
- **Register objective:** the W3 task prompt asks for a register in the working context, made of items starting `CURRENT` or `HISTORY` that cite item ids. It prescribes neither a helper nor an algorithm.
- **Register scoring** (`register_score.py`, `register-checks/1`), host-side after the run, on accepted revisions only:
  - **Timing:** stage k is assessed on the revision live when stage k+1 is released, and the last stage at the accepted final answer (`_register_point`).
  - **Categories:** see the module docstring.
  - **Output:** `evaluator/register_score.json` and `summary.json → register`.
- **Evaluator-only** (never in prompts, fixtures, the workspace or Jev payloads): item effects, register truth, semantic probes, hidden checks.
- **Pre-execution snapshots:** for W3 tasks, in both conditions, the runtime writes the full pre-execution workspace to `workspace_pre/step-NNN.json` (host-side; nothing visible changes).
- **Replay attribution** (`replay.py`, `jev-replay/1`, `clm-lib replay-jev RUN_DIR [--out DIR]`):
  - **The method:** every execution of a recorded run that called Jev is re-executed in the normal sandbox, on an isolated copy of its pre-execution workspace and the fixtures released at that step, with scripted responses: recorded (which must reproduce the accepted revision), withheld, and inverted.
  - **Safeguards:** no API calls; the run folder is not modified.
  - **Classes:** `substantive`, `marker_only`, `no_register_dependence`, `crash_without_answers`, `no_edit_without_answers`, `not_reproduced`.
  - **What it reports:** dependence of the helper's behaviour on its inputs, not correctness.
  - **Version 2** (`jev-replay/2`, experiment 014 R1):
    - **Answer matching:** answers are matched per judged item (question, background, item key and text).
    - **Valid substitutions:** `inverted` (1 − p) and `permuted` (answers rotated within each recorded request) serve valid probabilities of the same shape, and every served value is logged.
    - **Unsupported counterfactuals:** a request the original run never made gets no answer, and the step's counterfactual is reported as `unsupported_counterfactual`.
    - **Withholding** tests the fallback path only.
    - **`inverted_last` and `permuted_last`** substitute only an execution's last request (a post-hoc diagnostic).
    - **The same classes under neutral names:** `last_request`, plus the run-level `stages_substantive_last_request`, with `last_request_items` listing the decisions the intervention tests. This is the primary, pre-registered intervention of the R1 matched pilot, where it was post hoc for the R1 smoke.
    - **Each step also carries a static audit:** register-item literals, and ids of existing items in the caller and helper code. It is supporting evidence only.
- **Unchanged:** W2 and every earlier task, prompt and config. `configs/exp014c.toml` is the comparison phase's config.

## 27. Programme 015 shared foundations (implementation chunk 1; opt-in)

Programme 015's first milestone is a five-arm comparison on an eight-turn incident thread: `paper_clm_shell`, `paper_clm_tools`, `clm_lite_v2`, `clm_lite_v3_1` and the existing `token-tail/1` summary baseline. **This section specifies only the shared foundations implemented so far.** None of the arms, the incident-thread generator or scorer, the output guard or the launcher exists. Nothing here changes any historical configuration, prompt, schema, request or runtime path: the single-action protocol (§3.3) remains the only protocol the runner uses, and a test checks that the runner, CLI and exporter do not even import the new modules.

**A departure from §3.3, for 015 only:** §3.3 renders each request fresh with no native tool calls, so that editing history cannot break tool-call/result pairing. Programme 015 needs native tools, because the paper arms keep a tool-call message history. The new transport therefore makes pairing explicit and validated (§27.1). The old protocol is kept unchanged for all earlier work.

### 27.1 Transport `native-tools/1` (`transport.py`)

- **Requests** (`NativeRequest`) carry system blocks, the complete message list the arm chooses, the tool definitions, `max_tokens`, a purpose (`main` or `summary`), an optional `tool_choice`, and optional cache breakpoints (message indices). The transport never adds, drops or reorders messages, and never mutates the caller's history.
- **Responses** (`NativeResponse`) keep every provider content block exactly as returned and in order, including `thinking` blocks with their signatures, so a history-replaying arm can pass them back unchanged (`assistant_message()`).
- **Tool calls** are extracted in emission order with their ID, name and arguments. Arguments are never coerced.
- **Classification** (`interpret`):
  - `tool_calls`: one or more pairable calls;
  - `completion`: no call and a normal stop;
  - `malformed`, with explicit reasons: a call or output cut off by `max_tokens`; a `tool_use` stop without calls; calls with a normal stop; duplicate or missing IDs; an unknown block type; an unexpected stop reason.
  - Each call is also checked on its own: unknown tool, arguments not an object, or arguments failing the tool's schema.
  - Calls in a malformed response are reported but must not run.
- **Results:** `results_message` requires exactly one result per emitted call, in the calls' order. Any runtime text blocks come after the results. A call that is not run (invalid, or declined by the arm's policy) gets an explicit error result (`not_executed_result`), so a batch is never silently cut to its first call. `validate_history` checks a whole history: alternating roles, every call answered in the next user message, results first and in order, and unique IDs.
- **Execution policy is not part of the transport.** Each arm will define whether several calls per response may run and how.
- **Context representation is not part of the transport.** Paper arms will keep their message history; lite arms will rebuild each request from their working context. Both only supply a message list.
- **Providers:**
  - `AnthropicNativeProvider` uses the same credential handling, disabled SDK retries and error classification as the single-action adapter. The classification is now one shared function, `classify_anthropic_error`, whose behaviour is unchanged.
  - `ScriptedNativeProvider` is the test double. It is refused by live entry points.
  - The real adapter has been tested only against a fake client. Live compatibility, including replaying blocks, is **unverified**.

### 27.2 Tool interfaces (`tooling.py`)

- **Category:** a tool is either a shared `task` tool, which every arm gets unchanged (the planned incident search and page-read tools), or a `context` tool, which only some arms get (`context_read`, `context_apply`).
- **Side effects** are declared independently: `none`, `workspace` (for example a general shell, which is a task tool) or `context`. Neither property is sent to the model.
- **Registry:** a `ToolRegistry` fixes the tool order, which is part of the cached prefix.
- **Argument validation:** `validate_arguments` checks arguments against a documented JSON-Schema subset, and refuses unsupported keywords rather than under-checking.

### 27.3 Counters `session-counters/1` (`counters.py`)

These counters are derived from an append-only event list, per session and per user turn. The increments are tabulated in the module docstring:
- **Logical calls vs attempts:** a logical call counts +1 at its first attempt. Each dispatched attempt counts +1, whatever its outcome. Each retry counts +1, and a call that fails with no retry left counts as a failed call. All of these are kept separately for the `main` and `summary` roles.
- **Responses:** counted by kind.
- **Tool calls:** requested (by category, including calls in malformed responses), executed (a run that failed still counts as executed, and as failed) and not executed (with a reason).
- **Steps:** task steps and editing steps.
- **Edits:** accepted, rejected and unchanged edits.

Which steps are free editing steps is **not** decided. The arm supplies each step's kind.

**Limits:** the proposed limits are recorded, not enforced (`PROPOSED_LIMITS`):
- 60 task steps per user turn;
- a 144-call session cap. Which calls count is **pending**; `call_totals()` reports four candidate definitions.

**The existing meaning of a call** (§4.3, `limits.max_calls`): one provider attempt, covering actions, repairs, summaries and API retries. Summaries are excluded when `summary_calls_in_max_calls` is false. Adopting it unchanged, or choosing another definition, changes what the cap bounds in the comparison, so the decision is left for review.

### 27.4 Task-tool output cap `tool-output-cap/1` (`output_cap.py`)

- **The cap:** at most 8,000 Unicode code points of content per task-tool result.
- **What counts as content:** a tool's result text, or a command's stdout and stderr together. Error text counts too.
- **What does not count:** fixed framing (the exit-status line and stream labels) and truncation notices. Their size is recorded separately.
- **Streams:** stdout keeps its head. stderr keeps its tail, with up to 2,000 code points reserved when both streams overflow.
- **Encoding:** truncation is by code point, so the delivered text is always valid. A grapheme cluster may be split.
- **Records:** raw and delivered sizes, plus whether the executor's byte capture was complete.
- **Capture size:** command capping requires the executor to capture at least `4 × cap + 4` bytes per stream.
- **Historical path unchanged:** the executor's byte caps and observation format stay as they were for every historical configuration.
- **Not covered:** context-tool results. This is also not v3_1's output guard, which is not implemented.

### 27.5 Accounted native dispatch (`native_call.py`)

Dispatch follows §4.2 and the existing runner, with two differences: the tool definitions, `tool_choice` and `thinking` are included in the input bound, and the latency of failed attempts is recorded.
- **Reservation:** a bound is reserved before every attempt.
- **Settlement:**
  - provider usage at the dated price;
  - zero only for provably unbilled failures;
  - the full reservation when the outcome is unknown or the call was interrupted;
  - a refusal from its returned usage.
- **Retries:** each retry is its own attempt.
- **Bound check:** usage above the bound stops dispatch.
- **Usage kept per attempt and per role:** uncached input, 5-minute and 1-hour cache writes, cache reads, output and latency.
- **Prices:** without a dated price, live dispatch is refused. **`claude-sonnet-5` has no configured price**, and none has been guessed.

### 27.6 Pending before any live 015 work

- **Decisions:**
  - which calls the 144-call cap counts, and whether a separate cap on total billable attempts is added for financial bounding;
  - the free-editing-step policy for each arm;
  - the multiple-call execution policy for each arm;
  - caching placement and TTL;
  - recovery semantics, in particular v2's exact reset semantics relative to the current code;
  - the output guard's ordering and fit rules;
  - context-tool output limits.
- **Model and price:**
  - the `claude-sonnet-5` price and model settings (effort, thinking, `max_tokens`), from official sources and with a retrieval date;
  - the fit of the reservation bound to the model's tokenizer.
- **Fidelity:** a source-by-source fidelity mapping for `paper_clm_shell`, and the explicit differences between it and `paper_clm_tools`. The paper and the reference README describe only the shell design; `context_read`/`context_apply` is this project's adaptation.
- **Components still to build:**
  - the incident-thread generator and scorer;
  - the arms;
  - resumable background launching with spending guards;
  - exports and paired analysis;
  - offline proof through the real runner.
- **Spending:** no experimental spending ceiling has been agreed.

### 27.7 Library-first sequencing (2026-10-15)

- **What changed:** the lite arms' context-management runtime (`clm_lite_*`) is now being built as the separate library [guarded-clm](https://github.com/armalite/guarded-clm). clm-lib will import it at a pinned version and record that version in run provenance; that is guarded-clm's plan chunk 5. The library's behaviour is specified in its own SPEC and is not repeated here.
- **What it supersedes:** the earlier next step, building the incident-thread generator and task tools straight after §27's foundations. The generator and the scorer are still needed, but the library chunks come first.
- **What it does not change:** the §27 foundations (the native transport for the paper arms, the counters and the output cap). How `clm_lite_v2` and `clm_lite_v3_1` map onto the library's configuration is an open decision.
- **Correction (2026-10-15, later the same day):** guarded-clm's design changed: it is now a Pydantic AI capability, `ClmContext`, built by `clm_context()`. The library's own SPEC.md defines its behaviour. Integrating it into clm-lib has not started; because clm-lib's runner is not Pydantic AI based, how the harness hosts the capability is its first design question. **Update:** done offline, see §28. The integration is tracked here and in the results repository; guarded-clm's own plan covers library work only.

## 28. Programme 015 guarded-clm integration path (offline-verified; opt-in)

**What it is:** `clm_lib.p015` hosts the real guarded-clm capability, imported and never copied. It runs on its own Pydantic AI 2.27.1 environment (the `p015` dependency group), and the historical paths never import it.
- **The boundary** is in docs/architecture.md, decisions G1 to G8.
- **The behaviour** of the capability is defined by guarded-clm's own SPEC and is not restated here.

### 28.1 Runner

`GuardedSession(model, ...)` builds one `Agent` with `clm_context()` (`ClmStepLimit`, then `ClmContext`), the system prompt, any instructions, the bridged task tools and any framework tools. That agent is kept for the session.
- **Each turn:** `run_turn(prompt)` calls `agent.run(prompt, message_history=<previous result.all_messages()>, conversation_id=<the session's>, usage_limits=UsageLimits(request_limit=call_cap))`.
- **Not done:** nothing is reset between turns, and no prompt text, instruction or output schema is added for scoring.
- **Endings:** `completed`, `spend_refused`, `budget_exceeded`, `context_limit`, `step_limit`, `call_cap`, `provider_failure`, `tool_failure`, `model_behaviour`, `harness_defect` or `unclassified_error`. A financial refusal is never a context-management ending.

### 28.2 Accounting (`p015-accounting/1`)

`AccountedModel` wraps the model, and each `request()` is one provider attempt.
- **Refused before dispatch:**
  - in `scripted` mode, any model that isn't a `FunctionModel` or `TestModel`;
  - in `live` mode, a session that isn't authorised, a model without a price, or a request without `max_tokens`;
  - any attempt whose ledger reservation doesn't fit.
- **The reservation** prices a byte-based input bound over the messages, instructions and tool definitions, plus `max_tokens`.
- **Settlement:**
  - provider usage at the dated price;
  - 0 for HTTP 4xx and 429;
  - the reservation for any other failure or interruption.
- **Usage split:** uncached input = `input_tokens − cache_read_tokens − cache_write_tokens`, because Pydantic AI's `input_tokens` already includes cached tokens.
- **Disabled:** token counting and streaming are refused.
- **Retries:** none are added. Provider-SDK retries must be off for live use.
- **Counter separation:** the financial attempt counts are separate from the library's step and call counts.

### 28.3 Tool bridge

`bridge(task_tool)` exposes a read-only task tool (`category == "task"`, `side_effects == "none"`) to the model with its exact name, description and schema.
- **Invalid arguments:** these are rejected in Pydantic AI's `args_validator` hook with a `ModelRetry` ("invalid arguments: ..."), before the tool runs.
- **Results:** capped by `tool-output-cap/1`, with nothing else added.
- **Exceptions** propagate and end the run (`tool_failure`).
- **Concurrency:** calls of one response run concurrently, as for any synchronous Pydantic AI tool.

### 28.4 Records and export

A session directory holds:
- `session.json`: the format `guarded-session/1`, the evidence label `scripted-offline`, settings, and the provenance of guarded-clm and of the harness (version, install kind, git revision and dirty flags, source fingerprint, dependency versions; no absolute paths);
- `attempts.jsonl`: each request as sent, and each outcome, usage, latency and settlement;
- `trace.jsonl`: the capability's records;
- `tools.jsonl`;
- `turns.jsonl`.

**Export:** `python -m clm_lib.p015.export` copies the session to `guarded-session-export/1`, with a manifest and `SHA256SUMS`. The export is immutable and checked for consistency. The historical exporter is unchanged.

### 28.5 Status

- **The integration is done and verified offline only** (results in docs/results.md).
- **Not done:**
  - there are no live calls, and there is no live authorisation;
  - the paper and summary arms, the scenario generators, paid-run scheduling and analysis are not implemented;
  - provider compatibility is unverified until an authorised smoke (G8).

## 29. Programme 015 incident-thread workload: `docthread` (offline-verified)

**Where it lives:** `clm_lib.p015.docthread`. It runs on the §28 path, and the design rationale and parameters are in the results repository (`experiments/015-public-clm-comparison/docthread-workload.md`). It is an independently written public scenario; it supersedes an earlier, abandoned plan for this workload.

**The modules:**
- **`fixture.py` (`docthread-gen/1`):** a seeded fictional document-processing pipeline incident, built from an instance definition (seed and mechanism) and fully determined by it.
  - **Contents:** job-event journals by component, hour and page (58 events per page); configuration change logs; queue statistics in ten-minute rows; a playbook.
  - **Mechanisms:** a lease, memory or page-cap setting lowered by one change.
  - **Distractors:** a legitimate rejection burst earlier, an unrelated change shortly before, an index backlog, and decoy playbook symptoms.
  - **Unique support:** `uniqueness_problems` checks that every scored fact is held by its declared outputs and no others, over all 171 calls, and that every output stays under 7,800 characters.
- **`tools.py`:** `EvidenceService`, four read-only `tooling.Tool`s (`read_journal`, `list_changes`, `queue_stats`, `read_playbook`). They are bridged by §28.3, which applies schema validation and the 8,000-code-point cap. Every call is logged under a canonical key, so refetches are counted, never refused. Errors are neutral.
- **`task.py` (`docthread-task/1`):** eight questions with ten values; questions 5, 7 and 8 recall facts seen only while answering 2, 1 and 3. Each user turn ends with the same reply format, a fenced `json` block with the named fields.
- **`scoring.py` (`docthread-score/1`):** per-value exact or numeric rules, and a quote rule (a substring of a real incident-hour line of the failing component, of at least 24 characters, containing the setting at its new value).
  - **Diagnostics,** separate from correctness: a status (`missing`, `malformed`, `distractor`, `other_evidence`, `not_in_evidence`, and for quotes `too_short`, `not_a_journal_line`, `wrong_line`); and, for recall values, `carried_in_turn` and `refetched`.
- **`agent.py`:** a scripted investigator (`tidy` and `keep` policies) used only for offline checks.
- **`offline.py`:** runs sessions through `GuardedSession` and records the task versions, score and measurements in `session.json`.

**Instances:**
- **Development:** `docthread-dev-1` (7301, lease) and `docthread-dev-2` (7302, memory).
- **Confirmation:** defined at freeze time only.

**Status:** offline verification only. There are no model results. See docs/results.md.

## 30. Programme 015 paper and summary arms (offline-verified)

**Where they live:** `clm_lib.p015.paper`, `clm_lib.p015.summary` and `clm_lib.p015.arms`. They run on the §28 path's interfaces and do not use guarded-clm. The fidelity mapping, the settings table and the offline sessions are in the results repository (`experiments/015-public-clm-comparison/`: `paper-clm-shell-fidelity.md`, `arm-settings.md`, `arms-offline.md`).

### 30.1 Runner (`arms.py`)

`ArmSession` hosts one of the two arms the way `GuardedSession` (§28.1, unchanged) hosts guarded-clm: one Pydantic AI `Agent` per session, the bridged task tools (§28.3), the accounting wrapper (§28.2), the recorder and export (§28.4), and one `agent.run` per user turn with the previous turn's messages and the same `conversation_id`. Each arm is a capability that owns the model-visible message list: Pydantic AI adopts the request the capability sends as its history.

**Endings** are §28.1's, with the arms' own errors: `context_limit` (`ArmContextLimit`), `step_limit` (`ArmStepLimit`), `call_cap`, `summary_call_cap` (`SummaryCallCap`), and `sandbox_unavailable` (no isolated container could start; nothing ran on the host).

**Records:** `session.json` gains `arm` and the arm's settings, and records no guarded-clm provenance. The export manifest (`guarded-session-export/1`) gains `arm`, attempts by category, and charges by category and by cost basis. These appear only for arm sessions, so guarded-clm sessions' manifests are unchanged.

### 30.2 `paper_clm_shell` (`paper-clm-shell/1`)

- **The mirror** (`mirror.py`, `ctx-turn-mirror/1`): the editable conversation (everything after the pinned first request) as `/tmp/.live_ctx/LIVE_CTX_MAIN.txt`, one `[[CTX_TURN i role=user|assistant|tool]]` block per message. Calls and results are sections inside their blocks. `parse` rebuilds changed blocks, reuses unchanged ones, and rejects invalid conversations with a reason.
- **The shell** (`shell.py`): `ShellSandbox` runs each `bash` command in a fresh Docker container, with the executor's isolation flags (no network, non-root, no capabilities, `no-new-privileges`, memory, CPU and process limits, read-only root, private `/tmp`, `HOME=/tmp`). It mounts only the mirror directory and a per-session `/work`. It refuses to run without Docker; there is no host fallback.
- **The capability** (`capability.py`, `PaperShellContext`):
  - the `fit` edit gate, applied after each command, and a one-line receipt;
  - nudges at 25, 50 and 75% of the limit, and the adaptive persistent nudge;
  - rollback of the current turn's newest steps when over the limit (2,048-token margin, up to 50 per turn), then one final over-limit request;
  - free edit-only steps; 60 task steps and 144 calls per turn;
  - only the first `bash` call of a response runs, and the others get an explicit not-run result.
- **Settings:** `PaperSettings`. The budget is 32,000 and the limit 29,952 (the 015 standard).
- **Profiles (amended 2026-10-16):**
  - **`paper`** (the default) follows the paper's experimental settings: one editing reminder when the request is within 2,048 tokens of the limit, once per crossing; and over the limit, the last step is rolled back and the request retried, up to 6 times per turn.
  - **`readme`** follows the README's defaults: nudges at 25, 50 and 75% of the budget plus the adaptive persistent nudge; rollback of the newest steps to a 2,048-token margin, up to 50 times.

  Both then allow one final over-limit request before the turn ends. The first version used the README's rules, with the limit as the nudge denominator; both were corrected.

### 30.3 The summary arm (`token-tail-p015/1`)

`TokenTailSummary` applies §14's `token-tail/1` to the p015 conversation. It reuses `baseline.SummaryPolicy` unchanged for the trigger, tail split, summary length and acceptance, and `prompts.summary_user`'s layout for the summariser request.

**What changed for p015:**
- units of one reply and the following request replace entries;
- the summary goes into the pinned first request, and the current turn's question is restated if it was summarised;
- the summariser's system text is a docthread-neutral version of `SUMMARY_SYSTEM_TOKEN_TAIL`;
- there is no spill to a file.

**Summariser calls** go through a second `AccountedModel` (category `summary`, ledger kind `p015:summary`), which shares the ledger, recorder and attempt numbering (`AttemptCounter`). They have their own per-turn cap (`summary_call_cap`, 24, provisional). They are not task steps, and they do not count towards the 144-call cap.

### 30.4 Shared pieces

- **`estimate.py` (`p015-ratio-estimate/1`):** request characters over a ratio (fixed 4 for scripted models; otherwise starting at 3 and moving halfway towards each main call's observed ratio). No token-counting call is made.
- **`AccountedModel`** takes a `category` (default `main`) and an optional shared `AttemptCounter`. Behaviour with the defaults is unchanged.
- **`docthread/plain_agent.py`:** a scripted investigator for requests in plain message form. Its paper policies are `tidy`, `keep`, `never` and bad edits; for the summary arm it uses `none` and also acts as an extractive summariser. `docthread/arms_offline.py` runs the sessions, and `--matrix` runs the reported offline matrix.

**Status:** offline verification only, with scripted models and the real sandbox. There are no model results. See docs/results.md.

## 31. Programme 015 comparison contract and resumable launcher (offline-verified)

### 31.1 The model configuration (`live.py`, `p015-model/1`)

`ModelConfig` is one configuration for every arm and the summariser.

**Settings:**
- **Model:** `claude-sonnet-5`.
- **Sampling:** not sent.
- **Thinking:** adaptive (the default; `thinking` not sent) or `disabled`.
- **Effort:** `high`.
- **Output:** `max_tokens` 8,192.
- **Caching:** automatic prompt caching plus breakpoints after the tool definitions and the instructions, with one TTL (5 minutes by default).
- **Session isolation:** a `run-ref: <32 hex>` line leads each session's system prompt (`tagged_system_prompt`).

**`live_model()`** builds Pydantic AI's `AnthropicModel` on an Anthropic client with `max_retries=0`. It is never called live by the tests; they use an in-memory transport.

**Credentials:** `credential_source_present()` checks only that a credential source exists. `configs/prices.toml` holds the dated `claude-sonnet-5` price.

### 31.2 Accounting additions (`accounting.py`)

- **`RetryPolicy` (`p015-retry/1`):** bounded retries of one logical call, after HTTP 429, 500, 502, 503, 504 or 529, or a connection failure. Each retry is its own reserved, gate-checked attempt, recorded with `logical_call` and `retry`. There are none by default, so the existing offline failure paths are unchanged.
- **`SpendGate`:** a protocol, checked before every reservation. A refusal is recorded and never dispatched.
- **`LaunchStopped`:** the ending `stopped`, in both runners. `GuardedSession` and `ArmSession` accept `gate` and `retry`. With neither, their behaviour is unchanged.

### 31.3 Spending controls (`spend.py`, `p015-spend/1`)

**The authorisation file:** the maintainer writes it, naming the experiment and the launch. It sets a programme ceiling and a block stop. It is reread before every user turn and every provider attempt.

**What is counted:**
- **programme spend:** the ledger's `p015:*` entries;
- **block spend:** the launch's entries;
- **open reservations:** this process's, and those other processes have left pending in the ledger file.

Confirmed and assumed charges are reported apart. An authorisation for another launch is refused, and nothing writes or raises an authorisation or a ceiling.

### 31.4 The launcher (`launcher.py`, `p015-launch/1`)

**The commands** are `plan`, `run`, `status`, `stop` (`--now`, `--clear`), `resolve` and `export`.

**`plan`** writes an immutable contract: the session identities, a balanced instance-major run order, the cache tags, the settings, the ledger and authorisation paths, and fingerprints of the configuration, the clm-lib source, the guarded-clm source and the workload versions.

**`run` refuses** if the contract, its digest or a fingerprint differs; if another launcher holds the file lock; or while a stop is pending.

**State:** status files are written atomically, and `events.jsonl` is append-only. Each run is a recorder directory, `runs/<session>/r<k>`.

**Restart:**
- finished sessions are skipped;
- a session left `running` becomes `uncertain` if an attempt was in flight (the ledger charges it at the reservation), or `interrupted` otherwise;
- neither is redispatched without `resolve`.

**Outcomes:** `complete`, `arm_failure`, `provider_failure`, `spend_stopped`, `stopped` and `harness_defect`.

**`resolve`** reruns only non-results; the old run is kept.

**`export`** writes complete runs as verified exports and incomplete runs as partial exports (`export_session(partial=True)`), whose manifests declare what is missing.

**Status:** offline verification only (`tests/p015/test_launcher.py`, `test_live_config.py`). No live call has been made. See docs/results.md.

## 32. Strict ratio learning, explicit live settings and corrections (2026-10-16, offline-verified)

### 32.1 guarded-clm's `strict_ratio_learning` (superseded by §33, 2026-10-16; kept as the record of that revision)

**Where it comes from:** guarded-clm `0.2.0.dev0`, revision `db619a8`, adds `strict_ratio_learning` to `clm_context()` and `ClmContext`. Its default is False.

**What it changes:** only the shared estimator's new-text ratio. With the option on, that ratio learns only from a request that extends the last answered one, and it ignores observations below 0.5 characters per token. The overall ratio, the anchor, the guard and the records are unchanged (library SPEC 13.16, decisions P6).

**It is not behaviour-neutral:** estimates, and the guard and over-limit decisions that depend on them, can change after a step that does not extend the last request.

**How clm-lib passes it:**
- `GuardedSettings.strict_ratio_learning` has a **harness default of False**: the integration's earlier behaviour, independent of the library's default. It is always passed explicitly. An older spec that omits it runs with False.
- **The proposed 015 configuration** (`configs/p015/proposed-comparison.json`) sets it to **True** explicitly.
- **The value is recorded** in the launch contract (and so its configuration fingerprint), in `session.json` and in exports, beside the library's revision, version and source fingerprint.
- **Unchanged:** the library's trace variant label, and the paper and summary arms.

**The lockfile:** clm-lib's `uv.lock` records guarded-clm 0.2.0.dev0. It is still the editable sibling checkout, and the final live pin is pending.

### 32.2 Explicit live settings (`launcher.live_spec_problems`)

**A live plan must state:**
- **model:** `thinking`, `effort` (null for not sent), `max_tokens`, `cache_ttl`, `cache_breakpoints` (a subset of automatic, tool definitions and instructions) and `max_retries`;
- **retries:** `retry_policy: "p015-retry/1"`;
- **for the arms selected:**
  - guarded-clm's `strict_ratio_learning`;
  - the paper arm's `profile` and `reminder_before_tokens`, and `paper_rules`, which acknowledge the three rules the arm implements without a setting (only the implemented values are accepted);
  - the summariser allowance.

**Otherwise `plan` refuses** before writing anything. Scripted plans may use the code defaults, which are proposals.

**The contract** records `retry_policy`, `paper_rules` and an informational `settings_status`. `ModelConfig` gains `cache_breakpoints` and an optional `effort`. Both defaults equal the earlier behaviour. The live authorisation gate (§31.3, §31.4) is unchanged.

### 32.3 Corrections

- **§31.1's cache isolation is not complete:** the tool-definition breakpoint precedes the `run-ref` line, and summariser requests have none. The practical effect is not known without live evidence.
- **The paper and summary arms' request estimates** include replayed thinking blocks, through their serialised signatures and calibration on reported usage.
- **The summary arm's tail split** sizes units from visible text only. This is a limitation to observe, not changed.

**Status:** offline verification only (`tests/p015/test_strict_ratio.py`, `test_live_spec.py`). No live call has been made.

## 33. The accepted guarded-clm implementation (2026-10-16, offline-verified)

**The library:** guarded-clm `0.2.0.dev1`, revision `3f184734362c4c9e24d7f5e2a2d6de5fa18153dd`, source sha256 `51e6dbe8df7669ff8265d725976260f61891b6624d9c2201c7edc83dfcf2d9e4`.
- **The backstop is always on:** the shared estimator ignores a new-text observation below 0.5 characters per token; exactly 0.5 still counts.
- **`strict_ratio_learning` is removed.**
- **Everything else matches 0.1.0:** the learning rules, the guard and its conservative formula, and the records.
- **Not neutral:** estimates, and the limit and guard decisions that depend on them, can differ where a request would have taught a ratio below 0.5.

**Integration:**
- **The setting is gone:** `GuardedSettings` has no `strict_ratio_learning`, and nothing passes it.
- **Obsolete settings are refused:** `obsolete_setting_problems` reports a removed setting, and `plan`, in any mode, and `contract_problems` refuse it with a migration error, whatever its value. It is never ignored or translated.
- **Configurations:**
  - the strict-learning configuration is kept byte-identical in `configs/p015/superseded/`, and is refused;
  - its successor, `configs/p015/proposed-comparison-0.2.0.dev1.json`, states `library.guarded_clm` (version, revision and source fingerprint).
- **The pin:**
  - live plans with the guarded arm must state it;
  - any stated pin must match the loaded library (`library_pin_problems`), which must have no uncommitted package changes;
  - the contract records the pin, and the existing fingerprints refuse a contract made against another revision.
- **The lockfile** records 0.2.0.dev1.

**Unchanged:** the paper and summary arms, the workload, the scorer, the model settings, and the launcher's spending, contract and restart protections.

**Status:** offline verification only (`tests/p015/test_ratio_backstop.py`, `test_live_spec.py`, `test_equivalence.py`, `test_library_scenarios.py`). No live call has been made.

## 34. One event loop per p015 session (`p015-session-loop/1`; 2026-10-19, offline-verified)

**The defect (found in the partial live smoke `smoke-dev1`):**
- `GuardedSession.run_turn` and `ArmSession.run_turn` ran each user turn with `asyncio.run`, which creates and closes an event loop per turn;
- the session's model holds one `AsyncAnthropic` client for all turns, and the summary arm's summariser model shares it;
- that client's HTTP pool keeps connections bound to the loop that opened them, so from turn 2 on, the first request of each turn failed with "Event loop is closed". The bounded retry then succeeded.

The failed request can reach the server before the client fails, so a failed attempt may be billed. Charging it at the full reservation (§31) stays correct.

**Requirements:**
- **One loop per session:** a session owns one `asyncio.Runner` (`clm_lib.p015.loop.SessionLoop`), created at its first turn. Every turn, and every model request within it (summariser requests included), runs on that loop.
- **Shutdown:** `close()` on either session class closes the session's model clients on that loop, then closes the loop. A client shared by the agent and the summariser is closed once. A session that never ran a turn has nothing to close. `close()` is idempotent, and the sessions are context managers.
- **A closed session refuses turns,** rather than reusing a pool on a closed loop.
- **No pool sharing between sessions:** each launcher session builds its own model and client, as before.
- **The launcher** closes each session in a `finally`, however its turns end (completed, a provider failure, a spend refusal, a stop, an arm ending or an exception). The offline scorers and the test harness close sessions before scoring.

**Unchanged:** connection pooling, the retry policy (`p015-retry/1`), the request bodies, the accounting, guarded-clm, every arm's context management, the prompts, settings, workload, scorer and caching layout. Only the harness's event-loop lifecycle changed.

**Effect on existing launches:** the clm-lib source fingerprint changes, so `smoke-dev1`'s contract is refused for further runs (§31). That launch is not resumed. A corrected smoke needs a new launch id, a new freeze and a new authorisation.

**Verification (`tests/p015/test_session_loop.py`):** a loopback-only HTTP/1.1 keep-alive server returns synthetic Messages responses; no provider is contacted.
- the pre-fix pattern (`asyncio.run` per turn, one client) fails on the second turn with "Event loop is closed", after the server received the request;
- with the fix, for guarded-clm, `paper_clm_shell` and `token_tail_p015`: several turns over one connection, no failed or retried attempt, server requests equal to recorded attempts, and the client closed at the end;
- successive sessions each own their loop and client;
- summariser requests run on the session's loop;
- a provider error ending and a stop ending both close cleanly;
- request bodies equal those sent through the in-memory transport;
- a three-arm launch over the local server completes, with a temporary ledger and authorisation, and its export verifies.

**Status:** offline verification only. No live call has been made since the fix.

## 35. Clarified task, turn endings and output-limit truncation (2026-10-19, offline-verified)

A development revision after the partial smoke `smoke-dev1` and a review of generic benchmark conventions. It changes the instructions the model reads and the harness's ending rules. It does not change the scorer, the generated evidence, the tools, the budgets, any arm's context management, or guarded-clm.

### 35.1 The task wording (`docthread-task/2`)

**What stays fixed:** the same eight questions, fields and comparison rules (`docthread-score/1`), for every arm.

**What changes:**
- **The reply format** states, field by field, the form the scorer already requires:
  - an id, code or name on its own, exactly as written, without the rest of its record or line;
  - the journal's time;
  - a number;
  - one complete journal line, copied exactly.

  It also asks for no labels, notes or other text inside a value.
- **Question 4** asks for a journal line "that states the configuration setting behind the problem, with its value". That is what the quote rule checks (§29).

**What it does not reveal:** any setting, value, component or page that version 1 did not already show.

**Version 1 is kept** byte-identical, as the default for callers that state no version. It is what smoke-dev1 sent.

**The contract** records `task_version`, and the workload fingerprint uses it. Live plans must state it. An unsupported version is refused.

### 35.2 Turn endings (`turn_endings`)

**`p015-turn-endings/1`:** the first turn that does not complete ends the session, and its unfinished questions score as missing. This was the earlier behaviour.

**`p015-turn-endings/2`:** a revision of that policy, not a description of earlier runs. A turn that ends at the main model's step limit (`step_limit`) or call cap (`call_cap`) ends only that turn:
- its fields score as unanswered under the fixed denominator of 10;
- the next scheduled user turn runs in the same session, agent and conversation.

**Every other ending still stops the session:** context limits, spend refusals, operator stops, provider and harness failures, the summariser's cap and truncation.

**The outcome:** a session that continued past a limit-ended turn is an `arm_failure` result. It is never rerun.

**History after a limit-ended turn (guarded-clm):**
- The limit is raised before the library's own request hook. So the limited run's messages end with the last step's response and its tool results, with no dangling call.
- These messages become the next turn's history (`GuardedSession.continue_after_limit`). The library folds that step into the working context, as it does every step, and its new-turn rule records the unanswered question as a protected entry.
- Nothing is reset or synthesised, and the library's counters and rules are its own.
- The earlier completed turn's history must not be used: the library would record that turn's answer a second time, under the limited turn's number.
- **One visible artefact:** at the first request after continuing, the status line counts that folded step as one task step used in the new turn. Enforcement counts per run and is unaffected.

**Not supported by the paper and summary arms:** they keep their own copy of the conversation and take each request's new messages by position. After a limit-ended turn, the next question follows a tool-result request, and Pydantic AI merges the two when it next cleans the history, so the arm loses or repeats messages. Every history variant was checked offline. `plan` and `contract_problems` refuse `/2` for a launch that includes these arms, and `ArmSession.continue_after_limit` refuses with the reason. Supporting it needs a reviewed change to the arms' bookkeeping.

**Records:** each continuation is recorded as a `turn_continued` trace record and launch event. A refusal is recorded as a `continuation_refused` event, and the session then stops. Live plans must state `turn_endings`.

### 35.3 Output-limit truncation (`p015-truncation/1`)

**Detection:** Pydantic AI's Anthropic adapter reports the provider's `max_tokens` stop as `finish_reason == "length"`. It also maps `model_context_window_exceeded` to `length`, which is not an output limit and is not treated as one.

**What happens:** `AccountedModel` settles and records the attempt as usual, with its real usage and cost, marks it `output_truncated` (the finish and stop reasons), and raises `OutputTruncated`. This applies to agent and summariser requests alike.
- **The response is never retried** and never returned to the agent.
- **The ending is `output_truncated`,** distinct from context limits, transport failures and malformed output.
- **The session stops** under either turn-ending policy, with the outcome `output_truncated`.
- **Scoring:** earlier completed turns keep their scores. The affected turn and the unreached turns are unanswered under the fixed denominator.
- **Nothing is automatic:** the session is not retried, excluded or replaced, and `resolve --as rerun` refuses it.

**Its status:** this is a public protocol decision for development runs. The contract records `truncation`.

### 35.4 Verification and status

**Tests:** `tests/p015/test_turn_endings.py`, plus the updated `test_smoke_plan.py`, `test_live_spec.py` and `test_ratio_backstop.py`. They check:
- the version 1 wording digest;
- that version 2 states each form and reveals nothing new;
- that the stated forms are what the unchanged scorer accepts;
- that every arm receives the same version 2 prompts and scores 10/10 with the scripted investigators;
- guarded-clm continuation after a step and a call limit, with valid history and protected entries;
- launcher continuation with unanswered fields;
- the refusal for the two arms;
- that provider failure, truncation, stop and context-limit endings still stop;
- the adapter's `max_tokens` stop recorded, charged and not retried;
- truncation in agent and summariser requests, with earlier scores kept.

**The successor smoke configuration** (`configs/p015/smoke-dev2.json`) is smoke-dev1's settings with a new launch id and authorisation path, `docthread-task/2`, `p015-turn-endings/1`, and the guarded-clm checkout pin `cce58c1`. Its library source is unchanged from `3f18473` (sha256 `51e6dbe8…`). It is not authorised.

**Pins:** the closed `smoke-dev1.json` is kept byte-identical. Because the pin is exact, the README-only guarded-clm commit makes `plan` refuse it, and the proposed comparison configuration, until they are re-pinned.

No live call has been made.
