"""The resumable launcher (`p015-launch/1`) and its spending controls (`p015-spend/1`), offline: scripted models,
temporary ledgers and authorisations only, network blocked in-process. Subprocess tests run the real CLI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from clm_lib.budget import Ledger
from clm_lib.p015 import launcher as L
from clm_lib.p015.accounting import SpendRefused
from clm_lib.p015.export import verify_export
from clm_lib.p015.records import read_jsonl
from clm_lib.p015.spend import SpendGate

REPO = Path(__file__).resolve().parents[2]


def authorise(path: Path, launch_id: str, block: float = 20.0, programme: float = 50.0) -> None:
    path.write_text(json.dumps({"experiment": "015", "launch_id": launch_id, "programme_ceiling_usd": programme,
                                "block_stop_usd": block, "live": False, "granted_by": "test",
                                "reason": "temporary offline ledger"}), encoding="utf-8")  # fmt: skip


def planned(tmp: Path, launch_id: str = "t1", **spec: Any) -> Path:
    authorise(tmp / "auth.json", launch_id)
    base = {"launch_id": launch_id, "instances": ["docthread-dev-1"], "arms": ["guarded_clm", "token_tail_p015"],
            "questions": 2, "ledger": str(tmp / "ledger.json"), "ledger_ceiling_usd": 100.0,
            "authorisation": str(tmp / "auth.json")}  # fmt: skip
    L.plan(base | spec, tmp / "L")
    return tmp / "L"


def states(launch: Path) -> dict[str, str]:
    return {r["session"]: r["state"] for r in L.status(launch)["sessions"]}


def started(launch: Path) -> list[dict[str, Any]]:
    return [
        a
        for p in sorted(launch.glob("runs/*/r*/attempts.jsonl"))
        for a in read_jsonl(p)
        if a["event"] == "started"
    ]


def cli(*args: str, wait: bool = True) -> Any:
    cmd = [sys.executable, "-m", "clm_lib.p015.launcher", *args]
    if not wait:
        return subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=600, check=False)


def wait_for(cond: Any, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        time.sleep(0.1)


# ---------------------------------------------------------------------- planning, order, restart


def test_balanced_order_and_immutable_identities(tmp_path: Path) -> None:
    order = L.balanced_order([f"i{k}" for k in range(6)], list(L.ARMS))
    for arm in L.ARMS:  # each arm holds each position twice over six instances
        positions = [k % 3 for k, (_, a) in enumerate(order) if a == arm]
        assert sorted(positions) == [0, 0, 1, 1, 2, 2]
    launch = planned(tmp_path)
    contract = json.loads((launch / "contract.json").read_text())
    assert [s["session_id"] for s in contract["sessions"]] == [
        "s01-docthread-dev-1-guarded_clm", "s02-docthread-dev-1-token_tail_p015"]  # fmt: skip
    assert len({s["tag"] for s in contract["sessions"]}) == 2
    with pytest.raises(FileExistsError):
        L.plan({"launch_id": "t1", "instances": [], "ledger": "x", "authorisation": "y"}, launch)


def test_restart_skips_completed_sessions_and_repeats_nothing(tmp_path: Path) -> None:
    launch = planned(tmp_path)
    assert L.run(launch, max_sessions=1) == L.EXIT_OK
    assert states(launch) == {
        "s01-docthread-dev-1-guarded_clm": "finished",
        "s02-docthread-dev-1-token_tail_p015": "planned",
    }
    first = len(started(launch))
    assert L.run(launch) == L.EXIT_OK
    assert set(states(launch).values()) == {"finished"}
    second = len(started(launch))
    assert L.run(launch) == L.EXIT_OK and len(started(launch)) == second > first  # nothing repeated
    assert all(len(L.read_status(launch, s)["runs"]) == 1 for s in states(launch))
    rows = L.status(launch)
    assert all(r["outcome"] == "complete" and r["points"] == 3 for r in rows["sessions"])
    assert rows["block_spend"]["assumed_usd"] == 0 and rows["block_spend"]["confirmed_usd"] > 0
    session = json.loads(
        (launch / "runs/s01-docthread-dev-1-guarded_clm/r1/session.json").read_text()
    )
    assert session["launch"]["session_id"] == "s01-docthread-dev-1-guarded_clm"
    # The per-session cache tag leads the system prompt of every request of that session only.
    tag = json.loads((launch / "contract.json").read_text())["sessions"][0]["tag"]
    first_request = started(launch)[0]["messages"][0]["parts"][0]["content"]
    assert first_request.startswith(f"run-ref: {tag}\n\n")


# ---------------------------------------------------------------------- the launch contract


def test_changed_contract_or_fingerprint_is_refused(tmp_path: Path) -> None:
    launch = planned(tmp_path)
    text = (launch / "contract.json").read_text()
    (launch / "contract.json").write_text(text.replace('"questions": 2', '"questions": 3'))
    assert L.run(launch) == L.EXIT_CONTRACT
    contract = json.loads(text)
    contract["fingerprints"]["clm_lib_source_sha256"] = "0" * 64
    new = json.dumps(contract, indent=1, sort_keys=True) + "\n"
    (launch / "contract.json").write_text(new)
    import hashlib

    (launch / "contract.sha256").write_text(hashlib.sha256(new.encode()).hexdigest() + "\n")
    assert L.run(launch) == L.EXIT_CONTRACT
    events = read_jsonl(launch / "events.jsonl")
    assert [e["event"] for e in events].count("refused_contract") == 2
    assert (
        "fingerprint clm_lib_source_sha256 differs from the launch contract"
        in events[-1]["problems"]
    )
    assert not started(launch) and set(states(launch).values()) == {"planned"}


# ---------------------------------------------------------------------- spending controls


def test_gate_counts_programme_block_open_and_unknown_spend(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "ledger.json", 100.0)
    authorise(tmp_path / "auth.json", "b1", block=1.0, programme=2.0)
    gate = SpendGate(ledger, tmp_path / "auth.json", "b1")
    r = ledger.reserve(0.5, "b1.s01.r1", "p015:main")
    ledger.settle(r, actual_usd=0.25, status="ok")
    r = ledger.reserve(0.3, "b1.s01.r1", "p015:summary")
    ledger.settle(
        r, actual_usd=None, status="error:http_503"
    )  # unknown outcome: charged at the reservation
    r = ledger.reserve(0.4, "other.s01.r1", "p015:main")  # another launch: programme, not block
    ledger.settle(r, actual_usd=0.4, status="ok")
    pos = gate.position()
    assert pos["block"] == {"confirmed_usd": 0.25, "assumed_usd": 0.3}
    assert pos["programme"] == {"confirmed_usd": 0.65, "assumed_usd": 0.3}
    gate.check(0.44, "main")  # block room 1.0 - 0.55 = 0.45
    with pytest.raises(SpendRefused) as exc:
        gate.check(0.46, "summary")
    assert exc.value.kind == "block_stop"
    held = ledger.reserve(
        0.2, "b1.s01.r1", "p015:main"
    )  # an in-flight reservation counts against both
    with pytest.raises(SpendRefused):
        gate.check(0.3, "main")
    ledger.settle(held, actual_usd=0.0, status="error:http_429")
    # Another process's pending reservation, persisted in the ledger file, also counts.
    data = json.loads((tmp_path / "ledger.json").read_text())
    data["pending"] = [
        {
            "id": "x",
            "pid": 999_999_999,
            "run_id": "b1.s02.r1",
            "kind": "p015:main",
            "reserved_usd": 0.4,
        }
    ]
    (tmp_path / "ledger.json").write_text(json.dumps(data))
    with pytest.raises(SpendRefused):
        gate.check(0.1, "main")
    authorise(tmp_path / "auth.json", "another-launch")
    with pytest.raises(SpendRefused) as exc:
        gate.check(0.01, "main")
    assert exc.value.kind == "not_authorised"  # authorisation never carries over between launches
    (tmp_path / "auth.json").unlink()
    with pytest.raises(SpendRefused):
        gate.check_turn()


def test_a_lowered_block_stop_refuses_the_next_attempt_before_dispatch(tmp_path: Path) -> None:
    launch = planned(tmp_path)
    calls: list[tuple[str, int]] = []

    def hook(sid: str, index: int, messages: Any) -> None:
        calls.append((sid, index))
        if (
            index == 4
        ):  # after this call is dispatched, the maintainer lowers the block stop to what is spent
            ledger = json.loads((tmp_path / "ledger.json").read_text())
            spent = sum(e["charged_usd"] for e in ledger["entries"])
            authorise(tmp_path / "auth.json", "t1", block=spent + 0.0001)

    assert L.run(launch, hook=hook) == L.EXIT_SPEND
    assert calls == [
        ("s01-docthread-dev-1-guarded_clm", i) for i in range(5)
    ]  # the refused attempt never ran
    att = read_jsonl(launch / "runs/s01-docthread-dev-1-guarded_clm/r1/attempts.jsonl")
    refused = [a for a in att if a["event"] == "finished" and a["outcome"] == "refused"]
    assert (
        len(refused) == 1 and refused[0]["refusal"] == "block_stop" and refused[0]["attempt"] == 5
    )
    assert not any(a["event"] == "started" and a["attempt"] == 5 for a in att)
    st = L.read_status(launch, "s01-docthread-dev-1-guarded_clm")
    assert st["state"] == "finished" and st["runs"][0]["outcome"] == "spend_stopped"
    assert (
        states(launch)["s02-docthread-dev-1-token_tail_p015"] == "planned"
    )  # the launcher stopped


def test_summariser_spend_uses_the_same_block_allowance(tmp_path: Path) -> None:
    from clm_lib.p015.summary.capability import SUMMARY_SYSTEM_P015

    def is_summary(messages: Any) -> bool:
        return any(getattr(p, "content", None) == SUMMARY_SYSTEM_P015 for p in messages[0].parts)

    # A full run: the summariser's charges are part of this launch's block spend.
    (tmp_path / "a").mkdir()
    full = planned(tmp_path / "a", launch_id="ta", arms=["token_tail_p015"])
    seen: list[bool] = []
    assert L.run(full, hook=lambda s, i, m: seen.append(is_summary(m))) == L.EXIT_OK
    first = seen.index(True)
    ledger = json.loads((tmp_path / "a" / "ledger.json").read_text())
    assert {e["kind"] for e in ledger["entries"] if e["run_id"].startswith("ta.")} == {
        "p015:main",
        "p015:summary",
    }
    # The same session again, with the block stop lowered to what is spent just before the first summariser call:
    # that call is refused before dispatch, as a summary attempt.
    (tmp_path / "b").mkdir()
    launch = planned(tmp_path / "b", launch_id="tb", arms=["token_tail_p015"])
    calls: list[bool] = []

    def hook(sid: str, index: int, messages: Any) -> None:
        calls.append(is_summary(messages))
        if index == first - 1:
            led = json.loads((tmp_path / "b" / "ledger.json").read_text())
            spent = sum(
                e["charged_usd"] for e in led["entries"]
            )  # settled spend; this call settles above it
            authorise(tmp_path / "b" / "auth.json", "tb", block=spent + 0.001)

    assert L.run(launch, hook=hook) == L.EXIT_SPEND
    assert True not in calls  # the summariser was never called
    att = read_jsonl(launch / "runs/s01-docthread-dev-1-token_tail_p015/r1/attempts.jsonl")
    refused = [a for a in att if a["event"] == "finished" and a["outcome"] == "refused"]
    assert (
        len(refused) == 1
        and refused[0]["category"] == "summary"
        and refused[0]["refusal"] == "block_stop"
    )


# ---------------------------------------------------------------------- stopping, interruption, uncertainty


def test_stop_now_ends_the_session_before_the_next_attempt(tmp_path: Path) -> None:
    launch = planned(tmp_path)
    calls: list[int] = []

    def hook(sid: str, index: int, messages: Any) -> None:
        calls.append(index)
        if index == 3:
            (launch / "STOP").write_text("now\n")

    assert L.run(launch, hook=hook) == L.EXIT_STOPPED
    assert calls == [0, 1, 2, 3]
    assert states(launch)["s01-docthread-dev-1-guarded_clm"] == "stopped"
    assert L.run(launch) == L.EXIT_STOPPED  # a pending stop must be cleared explicitly
    (launch / "STOP").unlink()
    assert L.run(launch) == L.EXIT_OK
    assert states(launch) == {
        "s01-docthread-dev-1-guarded_clm": "stopped",
        "s02-docthread-dev-1-token_tail_p015": "finished",
    }
    L.resolve(
        launch, "s01-docthread-dev-1-guarded_clm", "rerun", "stopped by the operator in a test"
    )
    assert L.run(launch) == L.EXIT_OK
    st = L.read_status(launch, "s01-docthread-dev-1-guarded_clm")
    assert st["state"] == "finished" and [r["index"] for r in st["runs"]] == [
        1,
        2,
    ]  # the stopped run is kept
    assert (launch / "runs/s01-docthread-dev-1-guarded_clm/r1/attempts.jsonl").exists()
    with pytest.raises(ValueError):  # a result is never rerun
        L.resolve(launch, "s02-docthread-dev-1-token_tail_p015", "rerun", "want another try")


def test_interruption_with_an_attempt_in_flight_is_uncertain_and_not_redispatched(
    tmp_path: Path,
) -> None:
    launch = planned(
        tmp_path,
        scripted={"faults": {"s02-docthread-dev-1-token_tail_p015": {"hang_at_attempt": 3}}},
    )
    proc = cli("run", str(launch), wait=False)
    att = launch / "runs/s02-docthread-dev-1-token_tail_p015/r1/attempts.jsonl"
    wait_for(
        lambda: att.exists() and sum(1 for a in read_jsonl(att) if a["event"] == "started") == 4
    )
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert len(ledger["pending"]) == 1  # the in-flight reservation, persisted before dispatch
    assert L.run(launch) == L.EXIT_OK
    st = L.read_status(launch, "s02-docthread-dev-1-token_tail_p015")
    assert (
        st["state"] == "uncertain"
        and st["runs"][0]["in_flight_attempts"] == [3]
        and len(st["runs"]) == 1
    )
    assert (
        sum(1 for a in read_jsonl(att) if a["event"] == "started") == 4
    )  # nothing was redispatched
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["pending"] == []
    unknown = [e for e in ledger["entries"] if e["status"] == "unresolved_after_restart"]
    assert len(unknown) == 1 and unknown[0]["cost_basis"] == "reservation_assumed"
    assert L.status(launch)["block_spend"]["assumed_usd"] == unknown[0]["charged_usd"]
    with pytest.raises(ValueError):
        L.resolve(launch, "s02-docthread-dev-1-token_tail_p015", "abandoned", " ")
    L.resolve(
        launch,
        "s02-docthread-dev-1-token_tail_p015",
        "abandoned",
        "in-flight outcome unknown (test)",
    )
    assert states(launch)["s02-docthread-dev-1-token_tail_p015"] == "abandoned"
    # Exports: the complete run verifies in full; the uncertain one is a partial export that declares its gap.
    manifest = L.export_launch(launch, tmp_path / "export")
    assert all(not r["verify_problems"] for r in manifest["runs"]) and manifest["problems"] == []
    partial = json.loads(
        (
            tmp_path / "export/sessions/t1.s02-docthread-dev-1-token_tail_p015.r1/manifest.json"
        ).read_text()
    )
    assert any("started attempts" in p for p in partial["incomplete"])
    complete = json.loads(
        (
            tmp_path / "export/sessions/t1.s01-docthread-dev-1-guarded_clm.r1/manifest.json"
        ).read_text()
    )
    assert (
        "incomplete" not in complete
        and verify_export(tmp_path / "export/sessions/t1.s01-docthread-dev-1-guarded_clm.r1") == []
    )


def test_a_running_session_without_an_attempt_in_flight_is_interrupted(tmp_path: Path) -> None:
    launch = planned(tmp_path)
    assert L.run(launch, max_sessions=1) == L.EXIT_OK
    st = L.read_status(launch, "s01-docthread-dev-1-guarded_clm")
    st["state"] = "running"  # as a launcher killed between attempts would leave it
    L.write_status(launch, st)
    assert L.recover(launch) == ["s01-docthread-dev-1-guarded_clm"]
    assert L.read_status(launch, "s01-docthread-dev-1-guarded_clm")["state"] == "interrupted"


def test_two_launchers_never_dispatch_the_same_session(tmp_path: Path) -> None:
    launch = planned(
        tmp_path, scripted={"faults": {"s01-docthread-dev-1-guarded_clm": {"delay_s": 0.05}}}
    )
    first = cli("run", str(launch), wait=False)
    wait_for(lambda: states(launch)["s01-docthread-dev-1-guarded_clm"] == "running")
    second = cli("run", str(launch))
    assert second.returncode == L.EXIT_LOCKED and "another launcher holds the lock" in second.stderr
    assert first.wait(timeout=300) == L.EXIT_OK
    assert set(states(launch).values()) == {"finished"}
    for sid in states(launch):
        assert len(L.read_status(launch, sid)["runs"]) == 1
    for path in launch.glob("runs/*/r*/attempts.jsonl"):  # no attempt was dispatched twice
        numbers = [a["attempt"] for a in read_jsonl(path) if a["event"] == "started"]
        assert len(numbers) == len(set(numbers))
    pids = {
        e["pid"] for e in read_jsonl(launch / "events.jsonl") if e["event"] == "session_started"
    }
    assert len(pids) == 1


# ---------------------------------------------------------------------- the three arms


@pytest.mark.docker
def test_small_three_arm_matrix_runs_exports_and_restarts(tmp_path: Path) -> None:
    launch = planned(tmp_path, instances=["docthread-dev-1", "docthread-dev-2"], arms=list(L.ARMS),
                     scripted={"policies": {"paper_clm_shell": "tidy"}})  # fmt: skip
    proc = cli("run", str(launch))
    assert proc.returncode == L.EXIT_OK, proc.stderr
    rows = L.status(launch)["sessions"]
    assert [r["session"].split("-", 1)[1] for r in rows] == [
        "docthread-dev-1-guarded_clm", "docthread-dev-1-paper_clm_shell", "docthread-dev-1-token_tail_p015",
        "docthread-dev-2-paper_clm_shell", "docthread-dev-2-token_tail_p015", "docthread-dev-2-guarded_clm"]  # fmt: skip
    assert all(
        r["state"] == "finished" and r["outcome"] == "complete" and r["points"] == 3 for r in rows
    )
    before = len(started(launch))
    assert cli("run", str(launch)).returncode == L.EXIT_OK and len(started(launch)) == before
    manifest = L.export_launch(launch, tmp_path / "export")
    assert len(manifest["runs"]) == 6 and all(
        not r["verify_problems"] and not r["partial"] for r in manifest["runs"]
    )
    paper = json.loads(
        (launch / "runs/s02-docthread-dev-1-paper_clm_shell/r1/session.json").read_text()
    )
    assert paper["settings"]["settings"]["profile"] == "paper"
    assert os.path.exists(tmp_path / "export" / "SHA256SUMS")
