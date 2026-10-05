# clm-lib: Technical Specification

Version: 1.8 | 5 October 2026

Repository: `clm-lib` | Python package: `clm_lib` | CLI: `clm-lib`

This file defines the implementation requirements and acceptance criteria. User-facing setup and usage live in `README.md`; measured outcomes and experiment write-ups live in the results repository (§13), with a short overview in `docs/results.md`; implementation design lives in `docs/architecture.md`.

Version history:
- 1.2: the original specification. Its evaluation protocol (§8.2) was executed as experiment 001.
- 1.3: adds experiment 002, a staged incident task under sustained context pressure (§12), and the results export (§13). Requirements in §1–§11 are unchanged except where §12 extends them.
- 1.4: experiment write-ups move to the results repository, and the exporter preserves human-maintained documents (§13). The blog notes and detailed results move there too (§9).
- 1.5: adds a selectable, versioned summary-baseline policy (§14) for experiment 003. The original policy stays the default.
- 1.6: adds a coding task family with changing requirements and a sandboxed evaluator (§15), and an explicit, recorded ledger-ceiling change.
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
