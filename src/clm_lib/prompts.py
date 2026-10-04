"""Frozen prompt text and request rendering.

Changing any text here changes ``PROMPT_VERSION``; comparative runs record it.
"""

from __future__ import annotations

import json

from .context import Entry

PROMPT_VERSION = "2026-10-04.1"

PROTOCOL = """You are an investigation agent working through a strict JSON protocol.

Each request contains three sections:
- <task>: the fixed task. It is never edited and always present.
- <runtime_status>: limits and budget occupancy computed by the runtime.
- <working_context>: your transcript so far, one JSON object per line with id, role, body.
  This is the only memory you have between steps (apart from files you write).

Reply with exactly one JSON object and nothing else:
  {{"action": "execute", "thought": "<optional, one or two sentences>", "code": "<python source>"}}
  {{"action": "final", "answer": {{"root_cause": "...", "required_value": "...", "remedy": "...",
                                 "evidence_refs": ["path:line", ...]}}}}

"execute" runs your code as a new Python 3.12 process (standard library only) in a sandbox with
no network and a {exec_timeout}s time limit. The working directory /task/workspace is writable and
persists between steps; task files are read-only under /task/fixtures. Only stdout and stderr
come back, truncated to {output_cap} characters per stream, as an observation entry. Print only
what you need: search and filter (with line numbers) instead of printing whole files.
"final" ends the task; you get one final answer.
"""

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

SUMMARY_INSTRUCTIONS = """
CONTEXT MANAGEMENT (automatic):
- When the request grows past {pressure_pct}% of the budget, the runtime asks a summariser to
  replace older working-context entries with a summary, keeping the newest {tail} entries verbatim.
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


def system_prompt(
    mode: str,
    *,
    exec_timeout: int,
    output_cap: int,
    pressure_pct: int,
    tail: int,
    max_entries: int,
    max_body: int,
) -> str:
    text = PROTOCOL.format(exec_timeout=exec_timeout, output_cap=output_cap)
    if mode in ("clm", "guided"):
        text += CLM_INSTRUCTIONS.format(max_entries=max_entries, max_body=max_body)
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
