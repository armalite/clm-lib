"""tool-output-cap/1 (programme 015): 8,000 Unicode code points per task-tool result."""

from __future__ import annotations

from pathlib import Path

import pytest

from clm_lib.executor import DockerExecutor, ExecResult
from clm_lib.output_cap import DEFAULT_CAP, cap_exec, cap_text, required_capture_bytes
from clm_lib.runner import _fmt_observation

CAPTURE = required_capture_bytes()


def ex(
    stdout: str = "",
    stderr: str = "",
    *,
    out_bytes: int | None = None,
    err_bytes: int | None = None,
    code: int = 0,
) -> ExecResult:
    return ExecResult(
        code,
        stdout,
        stderr,
        False,
        len(stdout.encode()) if out_bytes is None else out_bytes,
        len(stderr.encode()) if err_bytes is None else err_bytes,
        0.2,
    )


def test_text_under_and_at_the_cap_is_whole() -> None:
    for n in (0, 10, DEFAULT_CAP):
        out, rec = cap_text("é" * n)
        assert out == "é" * n and not rec.truncated
        assert rec.raw_codepoints == rec.delivered_content_codepoints == n
        assert rec.raw_bytes == 2 * n and rec.framing_codepoints == 0


def test_text_over_the_cap_counts_code_points_not_bytes() -> None:
    text = "😀" * 5000 + "a" * 5000  # 4-byte and 1-byte code points
    out, rec = cap_text(text)
    assert rec.truncated and rec.raw_codepoints == 10_000 and rec.raw_bytes == 25_000
    assert rec.delivered_content_codepoints == DEFAULT_CAP
    assert out.startswith("😀" * 5000 + "a" * 3000)
    notice = out[DEFAULT_CAP:]
    assert notice == "\n[result truncated: 10000 code points produced, first 8000 shown]"
    assert rec.framing_codepoints == len(notice) and rec.delivered_codepoints == len(out)
    out.encode("utf-8")  # valid text: no lone surrogates


def test_combining_marks_may_split_but_text_stays_valid() -> None:
    text = "é" * 4001  # 8,002 code points, 4,001 graphemes
    out, rec = cap_text(text)
    assert rec.delivered_content_codepoints == 8000
    assert out[:8000].encode("utf-8").decode("utf-8") == out[:8000]


def test_exec_streams_share_the_cap_with_stderr_reserve_and_tail() -> None:
    out_text = "o" * 9000
    err_text = "".join(f"line {i}\n" for i in range(600)) + "ValueError: boom"
    rendered, rec = cap_exec(ex(out_text, err_text, code=1), CAPTURE)
    s = rec.streams
    assert s["stderr"]["delivered"] == 2000 and s["stdout"]["delivered"] == 6000
    assert rec.delivered_content_codepoints == 8000
    assert rendered.endswith(
        f"[stderr truncated: {len(err_text)} code points produced, last 2000 shown]"
    )
    assert "ValueError: boom\n[stderr truncated" in rendered  # the tail keeps the exception
    assert "[stdout truncated: 9000 code points produced, first 6000 shown]" in rendered
    assert rec.framing_codepoints + rec.delivered_content_codepoints == len(rendered)
    assert rendered.startswith("exit_code=1 duration=0.2s\n--- stdout ---\n")


def test_exec_small_stderr_is_whole_and_stdout_gets_the_rest() -> None:
    _, rec = cap_exec(ex("x" * 9000, "short error"), CAPTURE)
    assert rec.streams["stderr"] == {
        "raw": 11,
        "raw_bytes": 11,
        "capture_complete": True,
        "delivered": 11,
        "truncated": False,
    }
    assert rec.streams["stdout"]["delivered"] == 8000 - 11
    assert rec.delivered_content_codepoints == 8000
    rendered2, rec2 = cap_exec(ex("fine"), CAPTURE)
    assert rendered2 == "exit_code=0 duration=0.2s\n--- stdout ---\nfine" and not rec2.truncated


def test_exec_incomplete_capture_is_reported() -> None:
    rendered, rec = cap_exec(ex("y" * 9000, out_bytes=50_000), CAPTURE)
    assert not rec.capture_complete and rec.raw_bytes == 50_000
    assert "[stdout truncated: 9000+ code points produced, first 8000 shown]" in rendered
    with pytest.raises(ValueError, match="needs at least"):
        cap_exec(ex("z"), 6000)


def test_historical_observation_format_is_unchanged() -> None:
    """The byte-based historical path (runner._fmt_observation) keeps its exact output."""
    res = ExecResult(0, "a" * 6000, "", False, 9000, 0, 0.5)
    assert _fmt_observation(res, 6000) == (
        "exit_code=0 duration=0.5s\n--- stdout ---\n"
        + "a" * 6000
        + "\n[stdout truncated: 9000 bytes produced, first 6000 chars shown]"
    )
    assert DockerExecutor().output_cap_bytes == 6000


@pytest.mark.docker
def test_cap_on_real_sandbox_output(tmp_path: Path) -> None:
    """Multi-byte output from the real sandbox (no network), capped by code points."""
    ws, fx = tmp_path / "ws", tmp_path / "fx"
    ws.mkdir()
    fx.mkdir()
    code = (
        "import sys\n"
        "sys.stdout.write('€' * 12000)\n"  # 3 bytes each: 36,000 bytes, beyond the capture
        "sys.stderr.write('Traceback...\\n' + 'ä' * 3000 + '\\nKeyError: missing')\n"
    )
    executor = DockerExecutor(output_cap_bytes=CAPTURE)
    res = executor.run(code, ws, fx)
    assert res.exit_code == 0 and res.stdout_bytes == 36_000
    rendered, rec = cap_exec(res, executor.output_cap_bytes)
    assert rec.streams["stdout"]["capture_complete"] is False
    assert rec.streams["stderr"]["capture_complete"] is True
    assert rec.streams["stderr"]["delivered"] == 2000
    assert rec.streams["stdout"]["delivered"] == 6000
    assert "�" not in rendered  # no character split by the byte capture is delivered
    assert rendered.rstrip().endswith(
        "KeyError: missing\n[stderr truncated: 3031 code points produced, last 2000 shown]"
    )
    assert "[stdout truncated: 10668+ code points produced, first 6000 shown]" in rendered
    assert rec.delivered_codepoints == len(rendered)
