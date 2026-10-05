"""Frozen prompt text and request rendering.

Changing any text here changes ``PROMPT_VERSION``; comparative runs record it.
"""

from __future__ import annotations

import json

from .context import Entry

# 2026-10-05.1 adds the experiment-007 texts (CLM capability variants, the rounds task kind and
# its summariser instruction). Every earlier text is unchanged (tests pin their hashes).
# 2026-10-05.2 adds the revised experiment-007 clause (clm_reuse); all other texts unchanged.
PROMPT_VERSION = "2026-10-05.2"

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
