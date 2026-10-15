"""Task-tool output cap ``tool-output-cap/1`` (programme 015). Opt-in.

The historical executor path is unchanged: ``DockerExecutor`` keeps the first
``output_cap_bytes`` bytes per stream and ``runner._fmt_observation`` frames them, for every
existing configuration.

Rules of this policy (one task-tool result at a time):

- **Unit:** Unicode code points (Python ``str`` length), not bytes, tokens or graphemes.
  Truncation never produces invalid text; it may split a grapheme cluster (a base character
  and its combining marks).
- **What counts:** the result's *content*: a tool's result text, or for a command (a shell call)
  its stdout and stderr text together. An error is content: an error message counts like any
  other text. At most ``cap`` (default 8,000) content code points are delivered per result.
- **What does not count:** fixed framing (the ``exit_code=...`` line and ``--- stdout ---`` /
  ``--- stderr ---`` labels) and the truncation notice. They are bounded templates; their size
  is recorded separately (``framing_codepoints``), so ``delivered_codepoints`` = content +
  framing.
- **Streams:** stdout and stderr share the cap. If both fit, both are whole. Otherwise stderr
  keeps up to ``stderr_reserve`` (2,000) code points, or more if stdout leaves room, and stdout
  gets the rest. stdout keeps its **head**; stderr keeps its **tail** (a traceback ends with the
  exception), each followed by a notice giving produced and shown sizes.
- **Single-text results** (search, page-read) keep their head, followed by a notice.
- **Raw versus delivered sizes** are recorded per result: raw code points and UTF-8 bytes of the
  text the tool produced (for a command, bytes the process wrote, which may exceed what was
  captured), delivered content code points, framing code points, and whether the raw capture
  itself was complete.
- **Capture:** for commands, the executor must capture at least ``4 * cap + 4`` bytes per
  stream (``required_capture_bytes``; ``cap_exec`` refuses less), so the delivered stdout head
  is always decoded from complete UTF-8 and the policy, not the byte capture, decides what the
  model sees. A stream that wrote more than the capture is marked incomplete; its stderr "tail"
  is then the tail of what was captured, and the notice says the produced size is a lower bound.

Context-management tool results (``context_read``) are not covered: their limits are an arm
decision. This is not v3_1's later output guard either; that guard is not implemented.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .executor import ExecResult

CAP_VERSION = "tool-output-cap/1"
DEFAULT_CAP = 8000
STDERR_RESERVE = 2000


def required_capture_bytes(cap: int = DEFAULT_CAP) -> int:
    """Per-stream executor capture needed so the delivered text never depends on where the byte
    capture cut a multi-byte character (UTF-8 uses at most 4 bytes per code point)."""
    return 4 * cap + 4


@dataclass(frozen=True)
class CapRecord:
    version: str
    cap: int
    raw_codepoints: int  # of the captured text (all streams)
    raw_bytes: int  # UTF-8 bytes produced (for commands: written by the process)
    capture_complete: bool  # False: the executor captured less than was produced
    delivered_content_codepoints: int
    framing_codepoints: int
    delivered_codepoints: int
    truncated: bool
    streams: dict[str, dict[str, int | bool]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _notice(label: str, produced: int, shown: int, kept: str) -> str:
    return f"\n[{label} truncated: {produced} code points produced, {kept} {shown} shown]"


def cap_text(text: str, cap: int = DEFAULT_CAP) -> tuple[str, CapRecord]:
    """Cap a single-text tool result (keeps the head)."""
    if cap < 1:
        raise ValueError("cap must be positive")
    raw = len(text)
    kept = text[:cap]
    notice = _notice("result", raw, len(kept), "first") if raw > cap else ""
    rec = CapRecord(
        CAP_VERSION,
        cap,
        raw,
        len(text.encode("utf-8")),
        True,
        len(kept),
        len(notice),
        len(kept) + len(notice),
        raw > cap,
        {"result": {"raw": raw, "delivered": len(kept), "truncated": raw > cap}},
    )
    return kept + notice, rec


def cap_exec(
    res: ExecResult,
    capture_bytes: int,
    cap: int = DEFAULT_CAP,
    stderr_reserve: int = STDERR_RESERVE,
) -> tuple[str, CapRecord]:
    """Cap a command result (stdout and stderr share ``cap``). ``capture_bytes`` is the
    executor's per-stream capture (``DockerExecutor.output_cap_bytes``): a stream whose process
    wrote more bytes than that was captured incompletely, which the record and notice state."""
    if cap < 1 or not 0 <= stderr_reserve <= cap:
        raise ValueError("need cap >= 1 and 0 <= stderr_reserve <= cap")
    if capture_bytes < required_capture_bytes(cap):
        raise ValueError(
            f"executor captures {capture_bytes} bytes per stream; {CAP_VERSION} needs at least "
            f"{required_capture_bytes(cap)}"
        )
    out, err = res.stdout, res.stderr
    if len(out) + len(err) <= cap:
        out_keep, err_keep = len(out), len(err)
    else:
        err_keep = min(len(err), max(stderr_reserve, cap - len(out)))
        out_keep = min(len(out), cap - err_keep)
    head = f"exit_code={res.exit_code} duration={res.duration_s:.1f}s"
    if res.timed_out:
        head += " TIMED OUT"
    if res.error:
        head += f" error={res.error}"
    framing = len(head)
    parts = [head]
    content = 0
    streams: dict[str, dict[str, int | bool]] = {}
    complete = True
    for label, text, keep, produced_bytes in (
        ("stdout", out, out_keep, res.stdout_bytes),
        ("stderr", err, err_keep, res.stderr_bytes),
    ):
        captured_complete = produced_bytes <= capture_bytes
        complete = complete and captured_complete
        if not text and produced_bytes == 0:
            continue
        shown = text[:keep] if label == "stdout" else text[len(text) - keep :]
        cut = keep < len(text) or not captured_complete
        label_line = f"--- {label} ---\n"
        parts.append(label_line + shown)
        framing += 1 + len(label_line)  # newline joining the part, plus the label
        content += len(shown)
        if cut:
            produced = f"{len(text)}+" if not captured_complete else str(len(text))
            notice = (
                f"[{label} truncated: {produced} code points produced, "
                f"{'first' if label == 'stdout' else 'last'} {len(shown)} shown]"
            )
            parts.append(notice)
            framing += 1 + len(notice)
        streams[label] = {
            "raw": len(text),
            "raw_bytes": produced_bytes,
            "capture_complete": captured_complete,
            "delivered": len(shown),
            "truncated": cut,
        }
    rendered = "\n".join(parts)
    rec = CapRecord(
        CAP_VERSION,
        cap,
        len(out) + len(err),
        res.stdout_bytes + res.stderr_bytes,
        complete,
        content,
        framing,
        len(rendered),
        any(bool(s["truncated"]) for s in streams.values()),
        streams,
    )
    assert content + framing == len(rendered)
    return rendered, rec
