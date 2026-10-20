"""`paper_clm_tools` (`paper-clm-tools/1`): a structured-tools adaptation of the paper's model-managed context.

**This is not the paper's shell interface.** The model changes its context through two tools instead of a file:
- `context_read(from_block, to_block, page)` shows the context as numbered blocks, in pages that fit the tool-output
  cap;
- `context_apply(ops)` stages one set of operations, checked at once and applied all-or-nothing when the next
  request is built.

**The editable context** is every message part after the pinned ones: the system prompt, the first user message
(the task) and any rollback note, which are fixed. Each part is a block: a user message, a note, the model's reply
text, a tool call (its JSON arguments) or a tool result. The runtime owns each block's kind, tool and call id; the
model changes only text.

**The operations:**
- `replace_text` replaces text that occurs exactly once in a block;
- `replace_block` replaces a block's whole text;
- `delete` removes blocks, a tool call and its result together; the current step's calls cannot be deleted;
- `substitute` is a regular-expression substitution in the listed blocks or, without a list, in every text block as
  staged;
- `insert` adds a block of user text under a role the model names, placed in the request holding (or following) the
  anchor block, after that request's tool results.

A tool call's text must stay a JSON object. The rebuilt conversation drops emptied messages, merges same-kind
neighbours, keeps the request being built, must start with a request, and may not leave a call without its result.
Anything else is refused with a reason, and nothing changes. The fit gate checks the result against the limit at
staging and again at the commit.

**The limit:** the budget minus a 2,048-token reserve, checked on the request as sent, including the readouts and
notes. Over the limit:
1. the newest replies are undone until 2,048 tokens are free, and a pinned note names the calls whose output did not
   fit;
2. this happens up to `max_retries` times per **session** (across turns);
3. once they are used, the note tells the model this is its last turn;
4. any later over-limit request ends the turn (`ArmContextLimit`).

With `max_retries=0`, the first over-limit request ends the turn.

**Notices**, added to the request and kept in the history like any other user text:
- every tool result in the request ends with the context's size (`readouts`);
- one reminder when the context comes within 2,048 tokens of the limit, re-armed when it falls below;
- the finalize notice when at most 3 steps or calls are left.

**Steps:** 60 task steps and 144 model calls per user turn. A response whose only calls are `context_apply`, all of
them accepted, is a free step; `context_read` and a refused edit are task steps.

**Scope:** the mechanics follow an existing structured-tools implementation of this design, used as an engineering
reference; the code and the wording are clm-lib's own, and the size estimate is clm-lib's (`p015-ratio-estimate/1`).
The differences from it and from the shell arm are listed in clm-lib-test-results 015 `paper-clm-tools.md`.
"""

from __future__ import annotations

import dataclasses
import json
import operator
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.toolsets import FunctionToolset

from .arm_errors import ArmContextLimit, ArmStepLimit
from .estimate import ESTIMATE_VERSION, RatioEstimator, request_chars

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

TOOLS_VERSION = "paper-clm-tools/1"
EDIT_TOOL, READ_TOOL = "context_apply", "context_read"
NOTE = "[context manager]"
ACCEPTED, REFUSED = (
    "Accepted:",
    "REJECTED:",
)  # receipt prefixes; only a step whose edits were all accepted is free
OVERFLOW = f"{NOTE} Context limit:"  # a rollback note's start; such notes are pinned
UNDONE = f"{NOTE} (step undone)"  # a stand-in while a rollback empties the final request
EDITABLE = frozenset({"user", "note", "assistant_text", "tool_result", "tool_call"})
OPERATIONS = ("replace_text", "replace_block", "delete", "substitute", "insert")
ROLE_NAME = re.compile(r"[A-Za-z][\w-]{0,30}")
ROLE_HEADER = re.compile(r"^\[(" + ROLE_NAME.pattern + r")\]\n")
READOUT = re.compile(r"\n\[context: about [\d,]+ of [\d,]+ tokens\]$")

Where = tuple[int, int]  # (message index, part index)


class EditRefused(ValueError):
    """An operation set that cannot be applied as a whole; nothing changes."""


# ---------------------------------------------------------------------- reading the context


@dataclasses.dataclass(frozen=True)
class Block:
    number: int
    where: Where
    kind: str
    label: str
    text: str
    tool: str | None
    call_id: str | None
    current: bool  # in or after the latest reply: the step being taken now

    @property
    def message(self) -> int:
        return self.where[0]


def _prompt_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    return "".join(item for item in part.content if isinstance(item, str))


def classify(part: Any) -> tuple[str, str, str | None, str | None]:
    """(kind, text, tool name, call id) of a message part."""
    match part:
        case SystemPromptPart():
            return "system", part.content, None, None
        case UserPromptPart():
            text = _prompt_text(part)
            return ("note" if text.startswith(NOTE) else "user"), text, None, None
        case ToolReturnPart():
            return "tool_result", part.model_response_str(), part.tool_name, part.tool_call_id
        case RetryPromptPart():
            text = part.content if isinstance(part.content, str) else part.model_response()
            if part.tool_name:
                return "tool_result", text, part.tool_name, part.tool_call_id
            return "note", text, None, None
        case TextPart():
            return "assistant_text", part.content, None, None
        case ToolCallPart():
            return "tool_call", part.args_as_json_str(), part.tool_name, part.tool_call_id
    return "other", str(getattr(part, "content", "")), None, None


def _fixed(messages: Sequence[ModelMessage]) -> set[Where]:
    """Positions the model cannot edit: system prompts, the first user message and rollback notes."""
    fixed: set[Where] = set()
    seen_task = False
    for m, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        for p, part in enumerate(message.parts):
            if isinstance(part, SystemPromptPart):
                fixed.add((m, p))
            elif isinstance(part, UserPromptPart):
                if not seen_task or _prompt_text(part).startswith(OVERFLOW):
                    fixed.add((m, p))
                seen_task = True
    return fixed


def _last_reply(messages: Sequence[ModelMessage], default: int) -> int:
    replies = [i for i, m in enumerate(messages) if isinstance(m, ModelResponse)]
    return replies[-1] if replies else default


def context_view(messages: Sequence[ModelMessage]) -> list[Block]:
    """The editable context, one block per message part, numbered from 1 in order."""
    fixed = _fixed(messages)
    latest = _last_reply(messages, len(messages))
    view: list[Block] = []
    for m, message in enumerate(messages):
        for p, part in enumerate(message.parts):
            if (m, p) in fixed:
                continue
            kind, text, tool, call_id = classify(part)
            if kind == "tool_call":
                label = f"tool call {tool}"
            elif kind == "tool_result":
                label = f"tool result {tool}"
            elif kind == "assistant_text":
                label = "assistant"
            else:
                header = ROLE_HEADER.match(text) if kind == "user" else None
                label = header.group(1) if header else kind
            view.append(Block(len(view) + 1, (m, p), kind, label, text, tool, call_id, m >= latest))
    return view


def render_view(view: list[Block], first: int = 1, last: int | None = None) -> str:
    shown = [b for b in view if first <= b.number and (last is None or b.number <= last)]
    if not shown:
        return "No blocks."
    out = [f"Blocks {shown[0].number}-{shown[-1].number} of {len(view)}."]
    for b in shown:
        out.append(f"=== block {b.number} | {b.label} ===")
        out.append(b.text)
    return "\n".join(out)


def page_of(text: str, page: int, cap: int) -> str:
    """One page of a rendered view; each page, with its heading, fits within `cap` characters."""
    size = cap - 100
    count = max(1, (len(text) + size - 1) // size)
    if not 1 <= page <= count:
        return f"There is no page {page}; this view has {count}."
    nxt = f"; continue with page={page + 1}" if page < count else ""
    return f"[page {page} of {count}{nxt}]\n" + text[(page - 1) * size : page * size]


def named_blocks(op: dict[str, Any]) -> list[int]:
    """The block numbers an operation names, for its receipt."""
    listed = op.get("blocks")
    named: list[Any] = listed if isinstance(listed, list) else [op.get("block"), op.get("after")]
    return [v for v in named if type(v) is int and v > 0]


def unpaired(messages: Sequence[ModelMessage]) -> set[str]:
    """Call ids with a result but no call, or with a call and no result outside the latest reply."""
    call_at: dict[str, int] = {}
    answered: set[str] = set()
    for i, message in enumerate(messages):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                call_at[part.tool_call_id] = i
            elif isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_name:
                answered.add(part.tool_call_id)
    latest = _last_reply(messages, -1)
    open_calls = {cid for cid, i in call_at.items() if i != latest and cid not in answered}
    return open_calls | (answered - call_at.keys())


# ---------------------------------------------------------------------- changing the context


def _block_number(value: object, name: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise EditRefused(f"{name} must be a block number; got {value!r}.")


@dataclasses.dataclass
class _Plan:
    """What one operation set does, gathered before anything is built."""

    view: list[Block]
    texts: dict[int, str] = dataclasses.field(default_factory=dict)  # block -> its new text
    deleted: set[int] = dataclasses.field(default_factory=set)
    notes: list[tuple[int, str]] = dataclasses.field(default_factory=list)  # (after block, text)

    def block(self, value: object) -> Block:
        n = _block_number(value, "block")
        if not 1 <= n <= len(self.view):
            raise EditRefused(f"Block {n} does not exist; the context has {len(self.view)} blocks.")
        return self.view[n - 1]

    def editable(self, value: object) -> Block:
        b = self.block(value)
        if b.kind not in EDITABLE:
            raise EditRefused(
                f"Block {b.number} ({b.label}) has no editable text; it can only be deleted."
            )
        if b.number in self.deleted:
            raise EditRefused(f"Block {b.number} cannot be both deleted and changed.")
        return b

    def text(self, b: Block) -> str:
        return self.texts.get(b.number, b.text)

    # one method per operation

    def replace_text(self, op: dict[str, Any]) -> None:
        b = self.editable(op.get("block"))
        old, new = str(op.get("old", "")), str(op.get("new", ""))
        if not old or self.text(b).count(old) != 1:
            raise EditRefused(f"The text to replace must occur exactly once in block {b.number}.")
        self.texts[b.number] = self.text(b).replace(old, new, 1)

    def replace_block(self, op: dict[str, Any]) -> None:
        self.texts[self.editable(op.get("block")).number] = str(op.get("text", ""))

    def delete(self, op: dict[str, Any]) -> None:
        numbers = op.get("blocks")
        if not isinstance(numbers, list) or not numbers:
            raise EditRefused('A delete needs "blocks", a non-empty list of block numbers.')
        for value in numbers:
            b = self.block(value)
            if b.kind == "tool_call" and b.current:
                raise EditRefused(
                    f"Block {b.number} is a tool call of the current step; it can be deleted later."
                )
            if b.number in self.texts:
                raise EditRefused(f"Block {b.number} cannot be both changed and deleted.")
            self.deleted.add(b.number)

    def substitute(self, op: dict[str, Any]) -> None:
        try:
            pattern = re.compile(str(op.get("pattern", "")))
        except re.error as error:
            raise EditRefused(f"The pattern is not a valid regular expression: {error}.") from None
        if not pattern.pattern:
            raise EditRefused("The pattern is empty.")
        numbers = op.get("blocks")
        if isinstance(numbers, list):
            targets = [self.editable(v) for v in numbers]
        else:
            targets = [b for b in self.view if b.kind in EDITABLE and b.number not in self.deleted]
        replacement = str(op.get("replacement", ""))
        for b in targets:
            result = pattern.sub(replacement, self.text(b))
            if result != self.text(b):
                self.texts[b.number] = result

    def insert(self, op: dict[str, Any]) -> None:
        after = _block_number(op.get("after"), "after")
        if after:
            self.block(after)
        role = str(op.get("role") or "notes").strip()
        if not ROLE_NAME.fullmatch(role):
            raise EditRefused(
                "A role is a short name: a letter, then up to 30 letters, digits, '_' or '-'."
            )
        body = str(op.get("text", ""))
        if not body.strip():
            raise EditRefused("An inserted block needs some text.")
        self.notes.append((after, f"[{role}]\n{body}"))


def _edited(part: Any, text: str) -> Any:
    if isinstance(part, ToolCallPart):
        try:
            args = json.loads(text)
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            raise EditRefused(f"The {part.tool_name} call's text must remain a JSON object.")
        return dataclasses.replace(part, args=args)
    if isinstance(part, UserPromptPart | TextPart | ToolReturnPart | RetryPromptPart):
        return dataclasses.replace(part, content=text)
    raise EditRefused(f"A {part.part_kind} part cannot be edited.")


def _note_targets(messages: Sequence[ModelMessage], view: list[Block], notes: list[tuple[int, str]]) -> dict[int, list[Any]]:  # fmt: skip
    """Each note goes at the end of the request that holds, or else follows, its anchor block (0: the first)."""
    out: dict[int, list[Any]] = {}
    for after, text in notes:
        index = view[after - 1].message if after else 0
        while index < len(messages) - 1 and not isinstance(messages[index], ModelRequest):
            index += 1
        out.setdefault(index, []).append(UserPromptPart(text))
    return out


def _with_parts(message: ModelMessage, parts: Sequence[Any]) -> ModelMessage:
    if isinstance(message, ModelRequest):
        return dataclasses.replace(message, parts=list(parts))
    return dataclasses.replace(message, parts=list(parts))


def _reassemble(messages: Sequence[ModelMessage], parts_by_message: list[list[Any]], *, in_place: bool) -> list[ModelMessage]:  # fmt: skip
    """Build the conversation from new part lists: emptied messages go, neighbours of one kind merge, the trailing
    request joins the final request (the one being sent), and the structure is checked."""
    final = messages[-1]
    if not isinstance(final, ModelRequest):
        raise EditRefused("The conversation must end with a request.")
    built: list[ModelMessage] = []
    for message, parts in zip(messages[:-1], parts_by_message[:-1], strict=True):
        if not parts:
            continue
        unchanged = len(parts) == len(message.parts) and all(
            map(operator.is_, parts, message.parts)
        )
        if built and type(built[-1]) is type(message):
            built[-1] = _with_parts(message, [*built[-1].parts, *parts])
        else:
            built.append(message if unchanged else _with_parts(message, parts))
    leading: list[Any] = (
        list(built.pop().parts) if built and isinstance(built[-1], ModelRequest) else []
    )
    final_parts = [*leading, *parts_by_message[-1]]
    if not final_parts:
        raise EditRefused("Nothing would be left to send.")
    candidate: list[ModelMessage] = [*built, dataclasses.replace(final, parts=final_parts)]
    if not isinstance(candidate[0], ModelRequest):
        raise EditRefused("The conversation would start with a reply.")
    if unpaired(candidate) - unpaired(messages):
        raise EditRefused("A tool call would be left without its result.")
    if not in_place:
        return candidate
    final.parts = final_parts
    return [*built, final]


def apply_edit(
    messages: Sequence[ModelMessage], ops: Iterable[Any], *, in_place: bool
) -> list[ModelMessage]:
    """Apply one operation set, all or nothing; block numbers are those of `context_view(messages)`.

    Raises `EditRefused` without changing anything when any operation is invalid. The final message is the request
    being built: with `in_place`, and only once every check has passed, its parts are replaced on the object itself.
    """
    ops = list(ops)
    if not ops:
        raise EditRefused("The operation list is empty.")
    plan = _Plan(context_view(messages))
    for op in ops:
        name = op.get("op") if isinstance(op, dict) else None
        if name not in OPERATIONS:
            raise EditRefused(
                f"Unknown operation {name!r}; the operations are {', '.join(OPERATIONS)}."
            )
        getattr(plan, name)(op)
    for n, text in plan.texts.items():
        if not text.strip():
            raise EditRefused(f"Block {n} would have no text left; delete it instead.")
    changed = {n: t for n, t in plan.texts.items() if t != plan.view[n - 1].text}
    if not (changed or plan.deleted or plan.notes):
        raise EditRefused("The operations would change nothing.")
    # A deleted call takes its results with it, and a deleted result takes its call.
    pairs = {plan.view[n - 1].call_id for n in plan.deleted} - {None}
    removed: set[Where] = set()
    for b in plan.view:
        if b.number in plan.deleted or (b.call_id and b.call_id in pairs):
            if b.number in changed:
                raise EditRefused(
                    f"Block {b.number} goes with a deleted call or result, so it cannot be changed."
                )
            if b.kind == "tool_call" and b.current:
                raise EditRefused(
                    f"Block {b.number} is a tool call of the current step; it can be removed later."
                )
            removed.add(b.where)
    new_text = {plan.view[n - 1].where: t for n, t in changed.items()}
    added = _note_targets(messages, plan.view, plan.notes)
    parts_by_message = [
        [_edited(part, new_text[(m, p)]) if (m, p) in new_text else part
         for p, part in enumerate(message.parts) if (m, p) not in removed] + added.get(m, [])
        for m, message in enumerate(messages)
    ]  # fmt: skip
    return _reassemble(messages, parts_by_message, in_place=in_place)


def undo_latest(messages: Sequence[ModelMessage]) -> tuple[list[ModelMessage], list[str]] | None:
    """The conversation without its latest reply and that reply's tool results, and the calls undone. None when
    nothing can be undone."""
    head = _last_reply(messages, 0)
    reply = messages[head]
    if head == 0 or not isinstance(reply, ModelResponse):
        return None
    calls = [p for p in reply.parts if isinstance(p, ToolCallPart)]
    ids = {p.tool_call_id for p in calls}
    undone = [f"{p.tool_name}({json.dumps(p.args_as_dict())[:80]})" for p in calls] or [
        "your last reply"
    ]
    parts_by_message: list[list[Any]] = []
    for m, message in enumerate(messages):
        if m == head:
            parts_by_message.append([])
        elif m < head:
            parts_by_message.append(list(message.parts))
        else:
            parts_by_message.append([p for p in message.parts
                                     if not (isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_call_id in ids)])  # fmt: skip
    if not parts_by_message[-1]:
        parts_by_message[-1] = [UserPromptPart(UNDONE)]  # the caller replaces it with its note
    try:
        return _reassemble(messages, parts_by_message, in_place=False), undone
    except EditRefused:
        return None


# ---------------------------------------------------------------------- the capability


@dataclasses.dataclass(frozen=True)
class ToolsSettings:
    budget_tokens: int = 32_000
    reserve_tokens: int = (
        2_048  # the limit is the budget minus this; also the rollback margin and reminder point
    )
    max_steps: int = 60
    call_cap: int | None = None  # 2 * max_steps + 24 by default
    max_retries: int = (
        6  # rollback retries per session, across its turns (0: an over-limit request ends the turn)
    )
    readouts: bool = True
    max_output_chars: int = 8_000  # context_read pages
    finalize_steps: int = 3
    version: str = TOOLS_VERSION

    def __post_init__(self) -> None:
        if self.version != TOOLS_VERSION:
            raise ValueError(
                f"unknown paper_clm_tools version {self.version!r}; expected {TOOLS_VERSION!r}"
            )

    @property
    def limit(self) -> int:
        return self.budget_tokens - self.reserve_tokens

    @property
    def cap(self) -> int:
        return self.call_cap if self.call_cap is not None else 2 * self.max_steps + 24


@dataclasses.dataclass
class ToolsState:
    # per run (one user turn)
    staged: list[dict[str, Any]] | None = None
    steps: int = 0
    calls: int = 0
    pending: set[str] = dataclasses.field(
        default_factory=set
    )  # an edit-only step's call ids, awaiting receipts
    free_steps: int = 0
    # per session
    retries: int = 0
    final_turn: bool = False
    reminded: bool = False
    edits_staged: int = 0
    edits_committed: int = 0
    edits_rejected: int = 0
    rollbacks_total: int = 0
    params: ModelRequestParameters | None = None
    model: Model | None = None
    last_chars: int = 0


def instructions(settings: ToolsSettings) -> str:
    """The model-facing description of the mechanism only: no advice on what to keep."""
    if settings.max_retries:
        over = (
            "When a request would not fit, the newest steps are taken back out until there is room, and you are "
            f"asked to free space; this can happen {settings.max_retries} times in this conversation, and after that "
            "you have one last turn to answer."
        )
    else:
        over = "When a request would not fit, the turn ends."
    readout = (
        " Each tool result is followed by the current size of your context."
        if settings.readouts
        else ""
    )
    return (
        "Managing your context. What you are sent on every call, apart from the system prompt and the first task "
        "message, is your context, and it is yours to change. It is a sequence of numbered blocks: user messages, "
        "notes, your replies, your tool calls and their results.\n"
        f"- {READ_TOOL}(from_block, to_block, page) prints blocks as text (all of them if you give no range). A "
        f"result holds up to {settings.max_output_chars:,} characters, so a long view is split into pages.\n"
        f"- {EDIT_TOOL}(ops) changes your context in one action, with any number of operations:\n"
        '  - {"op": "replace_text", "block": n, "old": "...", "new": "..."} replaces text found exactly once in a '
        "block;\n"
        '  - {"op": "replace_block", "block": n, "text": "..."} replaces all of a block\'s text;\n'
        '  - {"op": "delete", "blocks": [n, ...]} removes blocks; a tool call and its result are removed as a pair;\n'
        '  - {"op": "substitute", "pattern": "...", "replacement": "...", "blocks": [n, ...]} applies a regular '
        'expression to the listed blocks, or to every block if "blocks" is left out;\n'
        '  - {"op": "insert", "after": n, "role": "notes", "text": "..."} adds a block after block n (0 means at '
        "the start), labelled with the role you give.\n"
        "The operations of one call are checked together and take effect on your next call, all or none, and the "
        "call returns a short receipt. Block numbers are those of your context right now; they shift once an edit "
        "takes effect. The text of a tool call has to remain a JSON object. A response that only calls "
        f"{EDIT_TOOL}, with every edit accepted, does not count as a task step. The system prompt and the first "
        "task message cannot be edited.\n"
        f"Your context may hold up to {settings.limit:,} tokens.{readout} {over}"
    )


def _append(request: ModelMessage, text: str) -> None:
    assert isinstance(request, ModelRequest)
    request.parts = [*request.parts, UserPromptPart(text)]


class PaperToolsContext(AbstractCapability[Any]):
    """One capability for the whole arm: the step limit and the context, in that order within each hook."""

    def __init__(self, settings: ToolsSettings | None = None, *,
                 on_record: Callable[[dict[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.id = "paper_clm_tools"
        self.description = (
            "Model-managed context through context_read and context_apply (paper-clm-tools/1)."
        )
        self.defer_loading = False
        self.settings = settings or ToolsSettings()
        self.on_record = on_record
        self.state = ToolsState()
        self.estimator = RatioEstimator()

    def record(self, entry: dict[str, Any]) -> None:
        if self.on_record is not None:
            self.on_record({"variant": TOOLS_VERSION, **entry})

    def describe(self) -> dict[str, Any]:
        return {"variant": TOOLS_VERSION, "estimate": ESTIMATE_VERSION, "settings": dataclasses.asdict(self.settings),
                "limit": self.settings.limit, "call_cap": self.settings.cap}  # fmt: skip

    async def for_run(self, ctx: RunContext[Any]) -> PaperToolsContext:
        del ctx  # a new user turn: fresh steps and calls; the session's retries and flags carry on
        st = self.state
        st.staged, st.steps, st.calls, st.pending = None, 0, 0, set()
        return self

    def get_instructions(self) -> str | None:
        return instructions(self.settings)

    def get_toolset(self) -> FunctionToolset[Any] | None:
        def context_read(ctx: RunContext[Any], from_block: int = 1, to_block: int | None = None, page: int = 1) -> str:  # fmt: skip
            """Print blocks of your context as text.

            Args:
                from_block: Number of the first block to print.
                to_block: Number of the last block to print; omit it to print through the last block.
                page: The page to print when the view is longer than one result holds.
            """
            view = render_view(context_view(ctx.messages), from_block, to_block)
            return page_of(view, page, self.settings.max_output_chars)

        def context_apply(ctx: RunContext[Any], ops: list[dict[str, Any]]) -> str:
            """Stage changes to your context; they take effect together on your next call.

            Args:
                ops: A list of operations in the forms given in your instructions.
            """
            return self.stage(ctx.messages, ops)

        return FunctionToolset(tools=[context_read, context_apply], id="paper_clm_tools")

    # ------------------------------------------------------------------ sizes

    def _size(self, messages: list[ModelMessage]) -> tuple[int, int]:
        """(characters, estimated tokens) of the request these messages would make."""
        st = self.state
        assert st.model is not None
        chars = request_chars(messages, st.params)
        return chars, self.estimator.tokens(st.model, chars)

    # ------------------------------------------------------------------ editing

    def stage(self, messages: Sequence[ModelMessage], ops: list[dict[str, Any]]) -> str:
        """`context_apply`: check a set against the context as it stands (the calling reply included, with a
        stand-in for this step's results) and stage it for the next request."""
        st, s = self.state, self.settings
        if st.staged is not None:
            return f"{REFUSED} this step already has a staged edit; give all its operations in a single {EDIT_TOOL} call."  # fmt: skip
        current = [
            *messages,
            ModelRequest(parts=[UserPromptPart(f"{NOTE} (results of this step)")]),
        ]
        view = context_view(current)
        # A substitution without a list covers the text blocks the model can see now, not this step's results.
        visible = [b.number for b in view[:-1] if b.kind in EDITABLE]
        ops = [{**op, "blocks": visible} if isinstance(op, dict) and op.get("op") == "substitute" and "blocks" not in op
               else op for op in ops]  # fmt: skip

        def refuse(reason: str, **extra: Any) -> str:
            st.edits_rejected += 1
            self.record({"type": "tools_edit", "status": "rejected", "reason": reason, **extra})
            return f"{REFUSED} {reason} Your context is unchanged."

        try:
            result = apply_edit(current, ops, in_place=False)
        except EditRefused as error:
            return refuse(str(error))
        projected = (
            self._size(result)[1] if st.params is not None and st.model is not None else None
        )
        if projected is not None and projected > s.limit:
            return refuse(f"Afterwards your context would be about {projected:,} tokens, more than your "
                          f"{s.limit:,}-token limit.", projected=projected)  # fmt: skip
        st.staged = list(ops)
        st.edits_staged += 1
        self.record({"type": "tools_edit", "status": "staged", "ops": ops, "projected": projected})
        named = sorted({n for op in ops if isinstance(op, dict) for n in named_blocks(op)})
        on = f" on block(s) {', '.join(map(str, named))}" if named else ""
        size = (
            f"; your context will be about {projected:,} of {s.limit:,} tokens"
            if projected is not None
            else ""
        )
        return f"{ACCEPTED} {len(ops)} operation(s){on}{size}. They take effect on your next call."

    def commit(self, messages: list[ModelMessage]) -> tuple[list[ModelMessage], bool]:
        """Apply the staged set to the request being built, checking it and its fit again."""
        st, s = self.state, self.settings
        ops, st.staged = st.staged or [], None
        try:
            projected = self._size(apply_edit(messages, ops, in_place=False))[1]
        except EditRefused as error:
            _append(messages[-1], f"{NOTE} Your staged edit did not take effect: {error}")
            self.record({"type": "tools_commit_rejected", "reason": str(error)})
            return messages, False
        if projected > s.limit:
            _append(messages[-1], f"{NOTE} Your staged edit did not take effect: afterwards your context would be "
                                  f"about {projected:,} tokens, more than your {s.limit:,}-token limit.")  # fmt: skip
            self.record(
                {
                    "type": "tools_commit_rejected",
                    "reason": "over the limit",
                    "projected": projected,
                }
            )
            return messages, False
        before = request_chars(messages, st.params)
        applied = apply_edit(messages, ops, in_place=True)
        st.edits_committed += 1
        self.record({"type": "tools_commit", "ops": ops, "chars_before": before,
                     "chars_after": request_chars(applied, st.params),
                     "blocks_after": len(context_view(applied))})  # fmt: skip
        return applied, True

    # ------------------------------------------------------------------ over the limit

    def recover(self, messages: list[ModelMessage], tokens: int) -> tuple[list[ModelMessage], int]:
        """Undo the newest replies until the margin is free and say so, or end the turn."""
        st, s = self.state, self.settings

        def end(why: str) -> ArmContextLimit:
            self.record(
                {"type": "context_limit", "estimated_tokens": tokens, "limit_tokens": s.limit}
            )
            return ArmContextLimit(why)

        if s.max_retries == 0 or st.final_turn:
            raise end(f"the request (about {tokens} tokens) would exceed the {s.limit}-token limit")
        candidate, steps = messages, 0
        undone: list[str] = []
        while tokens > s.limit - s.reserve_tokens:
            step = undo_latest(candidate)
            if step is None:
                raise end(f"nothing left to roll back; the request is about {tokens} tokens")
            candidate, calls = step
            undone, steps = [*calls, *undone], steps + 1
            tokens = self._size(candidate)[1]
        if st.retries < s.max_retries:
            st.retries += 1
            ask = f"Free some space with {EDIT_TOOL} and carry on (retry {st.retries} of {s.max_retries})."
        else:
            st.final_turn = True
            ask = (
                "There are no retries left: this is your last turn, so give your final answer now."
            )
        note = (f"{OVERFLOW} the request was over your {s.limit:,}-token limit, so the newest step was taken back "
                f"out; the output of {', '.join(undone)} did not fit. {ask}")  # fmt: skip
        final = messages[-1]  # the framework's own request object carries the result
        assert isinstance(final, ModelRequest)
        last = candidate[-1]
        assert isinstance(last, ModelRequest)
        final.parts = [
            p for p in last.parts if not (isinstance(p, UserPromptPart) and p.content == UNDONE)
        ]
        _append(final, note)
        out = [*candidate[:-1], final]
        st.rollbacks_total += 1
        self.record({"type": "tools_rollback", "undone": undone, "steps_undone": steps, "retry": st.retries,
                     "final_turn": st.final_turn, "tokens_after": tokens})  # fmt: skip
        return out, self._size(out)[1]

    # ------------------------------------------------------------------ notices

    def _notices(self, messages: list[ModelMessage], tokens: int) -> None:
        """The size readout on each tool result of the request, and the reminder near the limit."""
        st, s = self.state, self.settings
        final = messages[-1]
        assert isinstance(final, ModelRequest)
        if s.readouts:
            mark = f"\n[context: about {tokens:,} of {s.limit:,} tokens]"
            final.parts = [dataclasses.replace(p, content=p.model_response_str() + mark)
                           if isinstance(p, ToolReturnPart) and not READOUT.search(p.model_response_str()) else p
                           for p in final.parts]  # fmt: skip
        if tokens < s.limit - s.reserve_tokens:
            st.reminded = False
        elif not st.reminded:
            st.reminded = True
            _append(final, f"{NOTE} Your context is now about {tokens:,} tokens, less than "
                           f"{s.reserve_tokens:,} tokens below your {s.limit:,}-token limit.")  # fmt: skip
            self.record({"type": "tools_reminder", "estimated_tokens": tokens})

    # ------------------------------------------------------------------ the hooks

    def _check_steps(self, messages: list[ModelMessage]) -> None:
        """The previous edit-only step is free if every receipt was an acceptance; then the step and call limits,
        and the finalize notice."""
        st, s = self.state, self.settings
        if st.pending:
            receipts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts
                        if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_call_id in st.pending]  # fmt: skip
            free = bool(receipts) and all(
                isinstance(p, ToolReturnPart) and p.model_response_str().startswith(ACCEPTED)
                for p in receipts
            )
            st.free_steps += free
            st.steps += not free
            st.pending = set()
        if st.steps >= s.max_steps:
            self.record({"type": "step_limit", "steps": st.steps, "max_steps": s.max_steps})
            raise ArmStepLimit(f"the task-step limit of {s.max_steps} was reached")
        if st.calls >= s.cap:
            self.record({"type": "call_cap", "calls": st.calls, "call_cap": s.cap})
            raise UsageLimitExceeded(f"the model-call cap of {s.cap} was reached")
        left = min(s.max_steps - st.steps, s.cap - st.calls)
        if left <= s.finalize_steps and isinstance(messages[-1], ModelRequest):
            _append(
                messages[-1], f"{NOTE} {left} step(s) remain: finish now with your final answer."
            )

    async def before_model_request(self, ctx: RunContext[Any], request_context: ModelRequestContext) -> ModelRequestContext:  # fmt: skip
        st, s = self.state, self.settings
        st.params, st.model = request_context.model_request_parameters, ctx.model
        messages = list(request_context.messages)
        self._check_steps(messages)
        edited = False
        if st.staged is not None:
            messages, edited = self.commit(messages)
        tokens = self._size(messages)[1]
        if tokens > s.limit:
            messages, tokens = self.recover(messages, tokens)
            edited = True
        self._notices(messages, tokens)
        chars, sent = self._size(messages)  # the request as it will be sent, notices included
        if sent > s.limit:
            messages, tokens = self.recover(messages, sent)
            edited = True
            self._notices(messages, tokens)
            chars, sent = self._size(messages)
        request_context.messages = messages
        st.last_chars = chars
        self.record({"type": "tools_request", "estimated_tokens": sent, "chars": chars, "edited": edited,
                     "steps": st.steps, "free_steps": st.free_steps, "calls": st.calls, "retries": st.retries,
                     "final_turn": st.final_turn})  # fmt: skip
        return request_context

    async def after_model_request(self, ctx: RunContext[Any], *, request_context: ModelRequestContext,
                                  response: ModelResponse) -> ModelResponse:  # fmt: skip
        del request_context
        st = self.state
        st.calls += 1
        self.estimator.observe(ctx.model, st.last_chars, response.usage.input_tokens)
        calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        if calls and all(p.tool_name == EDIT_TOOL for p in calls):
            st.pending = {
                p.tool_call_id for p in calls
            }  # judged at the next request, from the receipts
        else:
            st.steps += 1
        return response
