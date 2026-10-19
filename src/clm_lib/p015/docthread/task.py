"""The docthread task: eight questions in one conversation, ten scored values.

Turns 5, 7 and 8 ask for facts that were visible only in tool outputs the investigation read in turns 2, 1 and 3,
which those turns did not ask about; nothing earlier says they will be needed. All evidence stays readable
throughout, so refetching is always possible (and is counted). Each user turn ends with the same answer instruction,
for every arm.

Two versions of the wording exist; the facts, fields, scorer and evidence are the same in both.
- `docthread-task/1` (historical, kept for reproduction): one generic reply-format sentence, and question 4 asks for
  a line "that shows what is actually going wrong".
- `docthread-task/2` (2026-10-19): the reply format says, field by field, what form the scorer already requires (an
  id, code or name on its own, the journal's time, a number, one whole journal line), and question 4 asks for a line
  that states the configuration setting behind the problem with its value, which is what the quote rule checks. It
  names no setting, value, component or page.
"""

from __future__ import annotations

import dataclasses

from .fixture import INCIDENT_HOUR, Fixture

TASK_V1 = "docthread-task/1"
TASK_V2 = "docthread-task/2"
TASK_VERSIONS = (TASK_V1, TASK_V2)
TASK_VERSION = TASK_V1  # the default for callers that state no version (historical reproduction)
SYSTEM_PROMPT = (
    "The support desk of a document-processing service is working through a live incident with you, one question "
    "at a time. Your tools give read-only access to the service's job-event journals, its configuration change "
    "history, queue statistics and the operations playbook. Use them to establish the facts."
)


@dataclasses.dataclass(frozen=True)
class Ask:
    """One scored value a question asks for. `check` names the comparison rule (see scoring); `recall_from` is set
    when the value was only visible in a tool output read during that earlier question."""

    key: str
    check: str
    recall_from: int | None = None


@dataclasses.dataclass(frozen=True)
class Question:
    number: int
    text: str
    asks: tuple[Ask, ...]


# docthread-task/2: the form each comparison rule (scoring.judge) accepts, stated without any answer.
FIELD_FORMS = {
    "name": "the name on its own, as the tool output writes it",
    "id": "the identifier on its own, exactly as the tool output writes it, without the rest of its record or line",
    "code": "the code on its own, exactly as the tool output writes it",
    "time": "the time exactly as the journal writes it",
    "count": "the number",
    "seconds": "the number of seconds",
    "quote": "one complete journal line, copied exactly",
}


def check_version(version: str) -> str:
    if version not in TASK_VERSIONS:
        raise ValueError(
            f"unsupported task version {version!r}; supported: {', '.join(TASK_VERSIONS)}"
        )
    return version


def user_message(q: Question, version: str = TASK_V1) -> str:
    """The full user turn: the question, then the reply format (the same for every arm)."""
    if check_version(version) == TASK_V2:
        fields = "\n".join(f"- {a.key}: {FIELD_FORMS[a.check]}" for a in q.asks)
        return (
            f"{q.text}\n\n"
            "Reply format: after any explanation, put a fenced ```json block containing one object with exactly "
            f"these fields, and nothing else:\n{fields}\n"
            "Copy values verbatim from the tool output (no rounding or reformatting), and put no labels, notes or "
            "other text inside a value."
        )
    keys = ", ".join(a.key for a in q.asks)
    return (
        f"{q.text}\n\n"
        f"Reply format: after any explanation, put a fenced ```json block containing one object with the fields "
        f"{keys}, and nothing else. Values must be verbatim from the tool output (no rounding or reformatting)."
    )


def questions(fx: Fixture, version: str = TASK_V1) -> list[Question]:
    """The eight questions. Only question 4's wording differs between the versions; the asks are identical."""
    end = f"{int(INCIDENT_HOUR[:2]) + 1:02d}:00"
    hour = f"{INCIDENT_HOUR}-{end} UTC"
    adjacent = fx.names[fx.roles["adjacent"]]
    return [
        Question(
            1,
            "Since a little after 11:00 UTC customers say their uploaded documents sit in processing and then come "
            "back rejected. Which pipeline component is rejecting the jobs, and at what time, as that component's "
            "journal records it, did it reject the first one?",
            (Ask("rejecting_component", "name"), Ask("first_rejection_time", "time")),
        ),
        Question(
            2,
            "Go through the configuration change log of every component for the two hours before that first "
            "rejection. Which change is responsible?",
            (Ask("faulty_change", "id"),),
        ),
        Question(
            3,
            f"According to the queue statistics for the rejecting component's queue, how many jobs were rejected in "
            f"the {hour} hour?",
            (Ask("rejected_jobs", "count"),),
        ),
        Question(
            4,
            f"Quote one line from that component's {hour} journal that shows what is actually going wrong."
            if check_version(version) == TASK_V1
            else f"Quote one line from that component's {hour} journal that states the configuration setting "
            "behind the problem, with its value.",
            (Ask("evidence_event", "quote"),),
        ),
        Question(
            5,
            f"The {adjacent} owners want to rule out their own change, which went out shortly before the incident. "
            "What is that change's id?",
            (Ask("adjacent_change", "id", recall_from=2),),
        ),
        Question(
            6,
            "Which symptom in the operations playbook matches this incident, and which step id restores normal "
            "processing?",
            (Ask("symptom_code", "code"), Ask("procedure_step", "id")),
        ),
        Question(
            7,
            "For the customer timeline we need the internal job id of the very first job that was rejected.",
            (Ask("first_rejected_job", "id", recall_from=1),),
        ),
        Question(
            8,
            f"Last one: in that queue during the {hour} hour, what was the longest wait of a queued job, in seconds?",
            (Ask("peak_queue_wait_s", "seconds", recall_from=3),),
        ),
    ]


def asks(fx: Fixture) -> list[Ask]:
    return [a for q in questions(fx) for a in q.asks]
