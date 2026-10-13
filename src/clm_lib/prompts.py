"""Frozen prompt text and request rendering.

Changing any text here changes ``PROMPT_VERSION``; comparative runs record it.
"""

from __future__ import annotations

import json

from .context import Entry

# 2026-10-05.1 adds the experiment-007 texts (CLM capability variants, the rounds task kind and
# its summariser instruction). Every earlier text is unchanged (tests pin their hashes).
# 2026-10-05.2 adds the revised experiment-007 clause (clm_reuse); all other texts unchanged.
# 2026-10-12.1 adds the optional experiment-008 Jev review notice (JEV_CLAUSE); all other texts
# unchanged.
# 2026-10-13.1 adds the experiment-009 update-review clauses (UPDATE_REVIEW_CLAUSE for both arms,
# UPDATE_REVIEW_JEV_CLAUSE for the assisted arm); all other texts unchanged.
# 2026-10-13.2 adds the experiment-010 initiated-review clauses (INITIATED_REVIEW_CLAUSE for both
# arms, INITIATED_REVIEW_JEV_CLAUSE for the assisted arm); all other texts unchanged.
# 2026-10-13.3 adds the experiment-010 v2 required-review clauses (INITIATED_REVIEW_V2_CLAUSE for
# both review conditions, INITIATED_REVIEW_V2_JEV_CLAUSE for the Jev condition); all other texts
# unchanged.
# 2026-10-13.4 adds the experiment-010 v3 clauses (INITIATED_REVIEW_V3_CLAUSE for both review
# conditions, INITIATED_REVIEW_V3_JEV_CLAUSE for the Jev condition); all other texts unchanged.
# 2026-10-13.5 adds the experiment-011 pilot clauses (EDIT_CHECK_CLAUSE for both checking
# conditions, EDIT_CHECK_JEV_CLAUSE for the Jev condition); all other texts unchanged.
# 2026-10-13.6 adds the experiment-012 handbook task kind (AGENT_ROLE/FINAL_EXAMPLE entries) and
# SEARCH_SCREEN_CLAUSE for the screening condition; all other texts unchanged.
PROMPT_VERSION = "2026-10-13.6"

PROTOCOL = """You are {agent_role} working through a strict JSON protocol.

Each request contains three sections:
- <task>: the fixed task. It is never edited and always present.
- <runtime_status>: limits and budget occupancy computed by the runtime.
- <working_context>: your transcript so far, one JSON object per line with id, role, body.
  This is the only memory you have between steps (apart from files you write).

Reply with exactly one JSON object and nothing else:
  {{"action": "execute", "thought": "<optional, one or two sentences>", "code": "<python source>"}}
{final_example}

"execute" runs your code as a new Python 3.12 process (standard library only) in a sandbox with
no network and a {exec_timeout}s time limit. The working directory /task/workspace is writable and
persists between steps; task files are read-only under /task/fixtures. Only stdout and stderr
come back, truncated to {output_cap} characters per stream, as an observation entry. Print only
what you need: search and filter (with line numbers) instead of printing whole files.
"final" ends the task; you get one final answer.
"""

# Task-kind specific protocol fragments. The "incident" values reproduce the original text
# byte for byte, so incident-task prompts (experiments 001-003) are unchanged.
AGENT_ROLE = {
    "incident": "an investigation agent",
    "coding": "a software engineering agent",
    "rounds": "an investigation agent",
    "handbook": "a research agent",
}
FINAL_EXAMPLE = {
    "incident": (
        '  {"action": "final", "answer": {"root_cause": "...", "required_value": "...", "remedy": "...",\n'
        '                                 "evidence_refs": ["path:line", ...]}}'
    ),
    "coding": '  {"action": "final", "answer": {"summary": "<what you implemented and changed>"}}',
    "rounds": (
        '  {"action": "final", "answer": {"incidents": [...], "unresolved": [...],\n'
        '                                 "summary": "..."}}   (fields as specified in <task>)'
    ),
    "handbook": (
        '  {"action": "final", "answer": {"answers": [{"question": "Q1", "value": "...",\n'
        '                                              "citations": ["HB-07 §2", ...]}, ...]}}'
    ),
}

CLM_INSTRUCTIONS = """
CONTEXT EDITING (you control your working context):
- /task/workspace/context.json is the authoritative working context. Whatever it contains after
  your step becomes <working_context> in the next request. Format:
  {{"format": "clm-context/v1", "entries": [{{"id": "...", "role": "...", "body": "..."}}, ...]}}
  ids: unique, 1-64 chars of [A-Za-z0-9_.:~-]; roles: assistant, observation, note, summary, receipt.
  Limits: {max_entries} entries, {max_body} chars per body.
- Your code may read and rewrite this file with any logic you choose: delete entries, rewrite or
  shorten bodies, merge entries, reorder them, or add notes. You may also write reusable helper
  modules in /task/workspace and import them in later steps (the working directory is importable).
- Ordering rule: the file you see at the start of a step holds the context up to the previous
  step. After your step, the runtime validates the file, then appends this step's action and
  observation (and a short receipt if you wrote the file). Edits therefore cannot remove the
  current step's output; you can remove it in a later step.
- Invalid files (bad JSON, duplicate ids, other roles, too large, symlinks) are rejected and the
  previous context is kept. The task and these instructions are outside the file.
- Editing is optional. When <runtime_status> reports pressure, manage the context before it runs
  out. Keep exact values, file:line references and open questions you still need.
"""

# Experiment 007: the same CLM capabilities with one condition-specific clause each. The shared
# part is CLM_INSTRUCTIONS without its sentence about reusable helper modules.
CLM_SHARED = CLM_INSTRUCTIONS.replace(
    " You may also write reusable helper\n  modules in /task/workspace and import them in later steps (the working directory is importable).",
    "",
)
CLM_CLAUSE = {
    "clm_direct": """- Context-management code (this run): write the code that inspects or rewrites context.json
  within each step. You may define functions inside a step's code, but do not save
  context-management functions or scripts to files for importing, running or exec-ing in later
  steps. Saving notes and other non-executable files is fine, and so is saving code that only
  analyses task files.
""",
    "clm_helpers": """- Context-management code (this run): you may also save reusable context-management functions
  in /task/workspace (for example as a Python module), call them in later steps and revise them.
  The working directory is importable. This is optional.
""",
    # Revised experiment 007: the reusable-function editing strategy is instructed, not optional.
    "clm_reuse": """- Context-management code (this run): perform working-context edits through a reusable Python
  module that you create in /task/workspace (the working directory is importable). Define
  functions for the editing operations you need, and invoke the saved functions when you choose
  to edit context. Reuse them for later edits and revise them if useful. You decide what
  information to retain and how the functions work. You do not need to edit on every step.
""",
}

SUMMARY_INSTRUCTIONS = """
CONTEXT MANAGEMENT (automatic):
- When the request grows past {pressure_pct}% of the budget, the runtime asks a summariser to
  replace older working-context entries with a summary, keeping the newest {tail} entries verbatim.
- You may write ordinary files in /task/workspace (for example notes or helper scripts) and read
  them back in later steps; they are not shown to you unless you print them.
"""

SUMMARY_INSTRUCTIONS_TOKEN_TAIL = """
CONTEXT MANAGEMENT (automatic):
- When the request grows past {pressure_pct}% of the budget, the runtime asks a summariser to
  replace older working-context entries with a summary. The most recent entries are kept
  verbatim up to about {tail_tokens} tokens (the newest entry is kept even if larger, when it
  fits); older entries, and recent entries too large for that allowance, are summarised.
  Summaries are sized to leave room for further work.
- You may write ordinary files in /task/workspace (for example notes or helper scripts) and read
  them back in later steps; they are not shown to you unless you print them.
"""

GUIDED_INSTRUCTIONS = """
DEMONSTRATION REQUEST (this run only): as part of solving the task, create your own reusable
context-management helper module under /task/workspace/helpers/ (design it yourself), invoke it
in at least two separate steps to manage context.json, and revise it if you find that useful.
"""

SUMMARY_SYSTEM = """You compress an agent's working transcript. The agent is investigating a
production incident from local files. Write a summary that will replace the older transcript
entries. It must preserve, exactly and verbatim where relevant:
- every concrete value, identifier, timestamp, setting name and file:line reference found so far;
- which sources were authoritative versus stale or superseded, and why;
- hypotheses confirmed, hypotheses ruled out, approaches that failed, and open questions;
- what the agent planned to do next.
Drop repetition and raw log dumps. Use compact bullet points. Plain text only."""


SUMMARY_SYSTEM_TOKEN_TAIL = """You compress an agent's working transcript. The agent is
investigating a production incident from local files, and will keep working after your summary
replaces the entries below. The transcript may include an earlier summary: carry its facts
forward. Preserve, exactly and verbatim where relevant:
- every concrete value, identifier, timestamp, setting name and file:line reference found so far;
- for each setting or claim, which value is CURRENT and which values were superseded, and by which
  record (APPLIED records override release notes, config files and notes; later APPLIED wins);
- hypotheses confirmed, hypotheses ruled out, approaches that failed;
- unresolved questions and the agent's next planned steps, including evidence stages still to read.
Drop repetition and raw log dumps. Use compact bullet points. Plain text only. Stay within the
requested length; it is set so the agent has room to continue."""


SUMMARY_SYSTEM_TOKEN_TAIL_CODING = """You compress an agent's working transcript. The agent is
implementing a Python package whose requirements arrive in stages, and will keep working after your
summary replaces the entries below. The transcript may include an earlier summary: carry its facts
forward. Preserve, exactly where relevant:
- every requirement currently in force, with the stage that introduced or last changed it;
- every superseded rule, what replaced it, and in which stage;
- exact numbers, thresholds, rates, formats, function names and signatures;
- which files and functions exist and what they implement;
- the latest test results (which tests fail and why) and remaining work, including stages still
  to be released.
Drop repetition, full code listings and raw test dumps (the code is in the workspace files). Use
compact bullet points. Plain text only. Stay within the requested length; it is set so the agent has
room to continue."""


# Experiment 008: neutral availability notice for the optional Jev context review action.
JEV_CLAUSE = """
CONTEXT REVIEW ADVICE (optional, this run):
- Reply {{"action": "review_context", "revision": <context revision number from <runtime_status>>,
  "entry_ids": ["<id>", ...], "update": "<the new information>"}} to ask a fast classifier (Jev)
  whether that update requires reconsidering a current assertion or planned action in each listed
  entry of that context revision. Instead of "update" you may give "update_entry_id": the id of
  an entry in the same revision that contains the update.
- The runtime sends only the update text and the listed entries' text. The next observation gives
  one probability per entry. The advice is probabilistic and can be wrong; it never edits your
  context, and you decide whether and how to edit. A review uses one of your model calls (at most
  {max_reviews} reviews per task); if the service fails you get a notice instead of advice.
"""


# Experiment 009 (strategy 1, update-review/1): identical in both arms of the comparison.
UPDATE_REVIEW_CLAUSE = """
UPDATE REVIEW (this run):
- When a new round is released, the observation also shows that round's incident-board and
  change-log lines verbatim, with their file:line references (logs are not included; read them
  from the files as usual), and the context revision that was current at the release.
- At each such update, consider whether it affects your existing working context. You may leave
  the context unchanged, edit part of an entry, keep facts as history, consolidate or remove
  entries, or use your own helpers. No edit is required.
"""

# Experiment 009, assisted arm only: explains the advice that accompanies each update.
UPDATE_REVIEW_JEV_CLAUSE = """- With each update the runtime also asks a fast classifier (Jev), for every entry of that context
  revision, for the probability that the update requires reconsidering a current assertion or
  planned action in the entry. The advice is probabilistic and can be wrong, and it never edits
  your context. A low probability is not a reason to delete an entry, and a high one does not mean
  the entry is wrong. You decide whether and how to edit.
"""

# Experiment 010 (initiated-review/1): identical in both arms of the comparison.
INITIATED_REVIEW_CLAUSE = """
CONTEXT REVIEW (this run):
- When you judge that new information may have made parts of your working context out of date,
  you can start a context review: reply {"action": "begin_review", "new_information": "<the new
  information, quoted or summarised>"}. Routine edits (for example removing step entries you no
  longer need) do not need a review.
- The runtime replies with the context revision and the entries to review (your notes and
  observations). In your next step, decide whether and how to edit context.json; you may also
  leave it unchanged. A review uses one model call. You can edit context.json at any time without
  a review.
"""

# Experiment 010, assisted arm only.
INITIATED_REVIEW_JEV_CLAUSE = """- In this run, each review also asks a fast classifier (Jev), for each listed entry, for the
  probability that the new information requires reconsidering a current assertion or planned
  action in it. The advice is probabilistic and can be wrong, and it never edits your context. A
  low probability is not a reason to delete an entry, and a high one does not mean the entry is
  wrong. You decide whether and how to edit.
"""

# Experiment 010 v2 (initiated-review/2): identical in both review conditions (B and C).
INITIATED_REVIEW_V2_CLAUSE = """
CONTEXT EDITS NEED A REVIEW FIRST (this run):
- Before you edit context.json, start a context review. If new information may have made parts
  of your working context out of date, reply {"action": "begin_review", "purpose": "update",
  "new_information": "<that information, quoted or summarised>"}. If you only want to compact or
  reorganise the context, reply {"action": "begin_review", "purpose": "compaction"}.
- The runtime replies with the context revision and the entries to review (your notes and
  observations). The review authorises one context.json edit in a later step, until you make that
  edit, start another review, or the next round is released. You may also decide not to edit.
- An edit to context.json without a valid review is rejected and the previous context is kept;
  other effects of that code are not undone. A review uses one model call. Exception: when the
  runtime status says OVER LIMIT, edit context.json directly in that step; no review is needed.
"""

# Experiment 010 v2, Jev condition only.
INITIATED_REVIEW_V2_JEV_CLAUSE = """- In this run, each update review also asks a fast classifier (Jev), for each listed entry, for
  the probability that the new information requires reconsidering a current assertion or planned
  action in it (compaction reviews get no advice). The advice is probabilistic and can be wrong,
  and it never edits your context. A low probability is not a reason to delete an entry, and a
  high one does not mean the entry is wrong. You decide whether and how to edit.
"""

# Experiment 010 v3 (initiated-review/3): identical in both review conditions (B and C). The
# review's update is supplied by the runtime; Claude's optional label is recorded only.
INITIATED_REVIEW_V3_CLAUSE = """
CONTEXT EDITS NEED A REVIEW FIRST (this run):
- Before you edit context.json, start a context review: reply {"action": "begin_review"}. You may
  add "purpose" ("update" or "compaction") and a short "new_information" note; both are recorded
  only.
- The runtime replies with the context revision, the entries to review (your notes and
  observations) and the update this review uses: the incident-board and change-log lines of the
  latest released round. The review authorises one context.json edit in a later step, until you
  make that edit, start another review, or the next round is released. You may also decide not
  to edit.
- An edit to context.json without a valid review is rejected and the previous context is kept;
  other effects of that code are not undone. A review uses one model call. Exception: when the
  runtime status says OVER LIMIT, edit context.json directly in that step; no review is needed.
"""

# Experiment 010 v3, Jev condition only.
INITIATED_REVIEW_V3_JEV_CLAUSE = """- In this run, each review also asks a fast classifier (Jev), for each listed entry, for the
  probability that the latest round's board and change-log lines require reconsidering a current
  assertion or planned action in it. The advice is probabilistic and can be wrong, and it never
  edits your context. A low probability is not a reason to delete an entry, and a high one does
  not mean the entry is wrong. You decide whether and how to edit.
"""

# Experiment 011 agent pilot, both checking conditions (B and C).
EDIT_CHECK_CLAUSE = """
CONTEXT CHANGES ARE PROPOSED, THEN CONFIRMED (this run; this replaces the rule that a change to
context.json takes effect after your step):
- A valid change you write to context.json is not applied at once. The runtime holds it as a
  proposal (P1, P2, ...), keeps the live context unchanged, and describes the proposal in that
  step's receipt. The full proposed file is /task/workspace/proposed_context.json.
- To apply it, reply {"action": "confirm_edit", "proposal": "<id>", "decision": "accept"}.
  Entries the runtime appended meanwhile (actions, observations, receipts) are kept after the
  accepted entries. To discard it, use "decision": "withdraw". To revise it, write context.json
  again in a later step; the new proposal replaces the pending one.
- Only an explicit accept applies a proposal. Any other action, including final, leaves it
  unapplied; a stage release discards it; a proposal whose base context has since changed in
  another way cannot be accepted.
- Accepting or withdrawing affects only the context; other files your code wrote stay as they
  are. A decision uses one model call. Exception: when the runtime status says OVER LIMIT, a
  change you write in that step is applied directly.
"""

# Experiment 011 agent pilot, Jev condition only (C).
EDIT_CHECK_JEV_CLAUSE = """- In this run, each proposal is also checked by a fast classifier (Jev) before your next step.
  It sees the task, the live context, the latest stage's requirements, what your proposing code
  printed and any requirement lines the proposal newly cites, and answers four questions: does
  the proposal drop an active obligation or exception, state something differently without
  support, or remove or reassign evidence; and is the supplied material insufficient to check
  it. The scores are advice and can be wrong. A flag means "inspect", not "wrong" (missing
  evidence may explain it), and a clear result does not guarantee the change is safe. You decide
  whether to accept, revise or withdraw; you never need to change a proposal or lower a score.
"""

# Experiment 012, screening condition only.
SEARCH_SCREEN_CLAUSE = """
SEARCH RESULTS ARE SCREENED (this run):
- A search result with 3 or more passages and at least 2,000 characters is screened by a fast
  classifier (Jev) before it reaches you: passages it judges unrelated to your query are
  omitted, and the result ends with a notice listing the omitted passage ids.
- Omitted passages are not lost: show(ids) and the saved result file named in the result header
  print them unscreened, and documents can be read directly. Screening can omit passages you
  need, and it never adds text.
"""

SUMMARY_SYSTEM_TOKEN_TAIL_ROUNDS = """You compress an agent's working transcript. The agent is
investigating a multi-service production incident whose evidence arrives in hourly rounds, and will
keep working after your summary replaces the entries below. The transcript may include an earlier
summary: carry its facts forward. Preserve, exactly and verbatim where relevant:
- each incident thread: its current cause and any superseded cause, affected services, current
  status and the record that set it;
- follow-up items opened and closed, with their codes;
- early facts that may matter later (change records, builds, configuration values);
- every file:line reference needed as evidence, and observed log formats;
- open questions and the next planned steps, including rounds still to be released.
Drop repetition and raw log dumps. Use compact bullet points. Plain text only. Stay within the
requested length; it is set so the agent has room to continue."""


def system_prompt(
    mode: str,
    *,
    exec_timeout: int,
    output_cap: int,
    pressure_pct: int,
    tail: int,
    max_entries: int,
    max_body: int,
    summary_policy: str = "fixed-tail/1",
    tail_tokens: int = 0,
    task_kind: str = "incident",
) -> str:
    text = PROTOCOL.format(
        exec_timeout=exec_timeout,
        output_cap=output_cap,
        agent_role=AGENT_ROLE[task_kind],
        final_example=FINAL_EXAMPLE[task_kind],
    )
    if mode in ("clm", "guided"):
        text += CLM_INSTRUCTIONS.format(max_entries=max_entries, max_body=max_body)
    elif mode in CLM_CLAUSE:
        shared = CLM_SHARED.format(max_entries=max_entries, max_body=max_body)
        marker = "- Ordering rule:"
        before, after = shared.split(marker, 1)
        text += before + CLM_CLAUSE[mode] + marker + after
    else:
        if summary_policy == "token-tail/1":
            text += SUMMARY_INSTRUCTIONS_TOKEN_TAIL.format(
                pressure_pct=pressure_pct, tail_tokens=tail_tokens
            )
        else:
            text += SUMMARY_INSTRUCTIONS.format(pressure_pct=pressure_pct, tail=tail)
    if mode == "guided":
        text += GUIDED_INSTRUCTIONS
    return text


def render_entries(entries: tuple[Entry, ...] | list[Entry]) -> str:
    """JSON lines with '<' escaped so entry text cannot forge section tags."""
    lines = []
    for e in entries:
        line = json.dumps({"id": e.id, "role": e.role, "body": e.body}, ensure_ascii=False)
        lines.append(line.replace("<", "\\u003c"))
    return "\n".join(lines)


RUN_TAG_FORMAT = "run-ref: {tag}\n"  # tag: 32 random hex chars, so the block length is fixed


def run_tag_block(tag: str) -> str:
    if len(tag) != 32 or any(c not in "0123456789abcdef" for c in tag):
        raise ValueError("run tag must be 32 lowercase hex characters")
    return RUN_TAG_FORMAT.format(tag=tag)


def entry_lines(entries: tuple[Entry, ...] | list[Entry]) -> list[str]:
    """One escaped JSON line per entry (same text as render_entries, without joining)."""
    return render_entries(entries).split("\n") if entries else []


def render_user_blocks(
    task: str, status: str, entries: tuple[Entry, ...] | list[Entry]
) -> tuple[list[str], list[int]]:
    """Layout blocks/1: (text blocks, indices of cache breakpoint candidates).

    Stable content first: the task block (which opens <working_context>), then one block per
    context entry, then the changing runtime status. Breakpoint candidates are the task block
    and the last entry block, so an unchanged prefix of entries can be reused as it grows.
    """
    blocks = [f"<task>\n{task.strip()}\n</task>\n\n<working_context>\n"]
    blocks += [line + "\n" for line in entry_lines(entries)]
    breakpoints = [0] if len(blocks) == 1 else [0, len(blocks) - 1]
    blocks.append(f"</working_context>\n\n<runtime_status>\n{status.strip()}\n</runtime_status>\n")
    return blocks, breakpoints


def summary_user_blocks(
    task: str, entries: list[Entry], max_chars: int
) -> tuple[list[str], list[int]]:
    """Layout blocks/1 for summary requests, with the same breakpoint convention."""
    blocks = [f"<task>\n{task.strip()}\n</task>\n\n<transcript_to_summarise>\n"]
    blocks += [line + "\n" for line in entry_lines(entries)]
    breakpoints = [0] if len(blocks) == 1 else [0, len(blocks) - 1]
    blocks.append(
        "</transcript_to_summarise>\n\n"
        f"Write the replacement summary now, at most {max_chars} characters."
    )
    return blocks, breakpoints


def render_user(task: str, status: str, entries: tuple[Entry, ...] | list[Entry]) -> str:
    body = render_entries(entries)
    return (
        f"<task>\n{task.strip()}\n</task>\n\n"
        f"<runtime_status>\n{status.strip()}\n</runtime_status>\n\n"
        f"<working_context>\n{body}\n</working_context>\n"
    )


def summary_user(task: str, entries: list[Entry], max_chars: int) -> str:
    return (
        f"<task>\n{task.strip()}\n</task>\n\n"
        f"<transcript_to_summarise>\n{render_entries(entries)}\n</transcript_to_summarise>\n\n"
        f"Write the replacement summary now, at most {max_chars} characters."
    )
