"""The docthread task (`docthread-task/1`): eight questions in one conversation, ten scored values.

Turns 5, 7 and 8 ask for facts that were visible only in tool outputs the investigation read in turns 2, 1 and 3,
which those turns did not ask about; nothing earlier says they will be needed. All evidence stays readable
throughout, so refetching is always possible (and is counted). Each user turn ends with the same answer instruction,
for every arm.
"""

from __future__ import annotations

import dataclasses

from .fixture import INCIDENT_HOUR, Fixture

TASK_VERSION = "docthread-task/1"
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


def user_message(q: Question) -> str:
    """The full user turn: the question, then the reply format (the same for every arm)."""
    keys = ", ".join(a.key for a in q.asks)
    return (
        f"{q.text}\n\n"
        f"Reply format: after any explanation, put a fenced ```json block containing one object with the fields "
        f"{keys}, and nothing else. Values must be verbatim from the tool output (no rounding or reformatting)."
    )


def questions(fx: Fixture) -> list[Question]:
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
            f"Quote one line from that component's {hour} journal that shows what is actually going wrong.",
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
