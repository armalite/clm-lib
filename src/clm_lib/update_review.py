"""Experiment 009 (strategy 1): the update payload for update-triggered context review.

``round-files/1``: when a round of a ``rounds`` task is released, the payload is that round's
incident-board and change-log lines, verbatim and in file order, each prefixed with its
``round-NN/<file>:<line>`` reference. The only processing is mechanical: blank lines and Markdown
heading lines (``#``) are dropped, and a payload longer than the limit is cut at a line boundary
with a marker. Logs are never included (they are large; the model reads them from the files).
The same payload is shown to both arms; in the assisted arm it is also the Jev ``update``.
"""

from __future__ import annotations

PAYLOAD_VERSION = "round-files/1"
PAYLOAD_FILES = ("board.md", "changes.md")  # in this order


def build_payload(released: dict[str, str], max_chars: int) -> tuple[str, list[str], bool]:
    """(payload text, source files used, truncated) for one released round."""
    lines: list[str] = []
    sources: list[str] = []
    for name in PAYLOAD_FILES:
        for path in sorted(p for p in released if p.endswith(f"/{name}") and "/logs/" not in p):
            sources.append(path)
            for i, line in enumerate(released[path].splitlines(), start=1):
                if line.strip() and not line.lstrip().startswith("#"):
                    lines.append(f"{path}:{i}: {line.strip()}")
    out: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars:
            out.append(f"[payload cut at {max_chars} characters; the full files are released]")
            return "\n".join(out), sources, True
        out.append(line)
        size += len(line) + 1
    return "\n".join(out), sources, False
