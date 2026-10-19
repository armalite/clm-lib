"""The confirmation-instance checks (`p015-instance-check/1`), exercised on the development instances and on
scripted generators. No confirmation instance is generated: tests use development seeds, other names, or stubs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from clm_lib.p015.docthread import confirm as C
from clm_lib.p015.docthread.fixture import DEV_INSTANCES, instance, mentions


def other_dev_ids(name: str) -> set[str]:
    return set().union(*(C.identifiers(instance(n)) for n in DEV_INSTANCES if n != name))


@pytest.mark.parametrize("name", list(DEV_INSTANCES))
@pytest.mark.parametrize("reading", list(C.SIZE_READINGS))
def test_the_development_instances_pass_every_check(name: str, reading: str) -> None:
    seed, mechanism = DEV_INSTANCES[name]
    row = C.check(name, seed, mechanism, C.SIZE_READINGS[reading], other_dev_ids(name))
    assert row["valid"] and row["failed"] == [] and set(row["checks"]) == set(C.CHECKS)
    assert row["evidence_chars"] == C.SIZE_READINGS[reading].measure(instance(name))


def test_the_size_readings_and_their_bounds_are_explicit() -> None:
    assert C.SIZE_READINGS["plan"].bounds() == (
        95_923,
        123_358,
    )  # dev 106,581 and 112,144, widened by 10%
    assert C.SIZE_READINGS["plan_with_recall"].bounds() == (
        102_928,
        131_799,
    )  # dev 114,364 and 119,817
    assert C.SIZE_READINGS["stated"].bounds() == (100_000, 139_000)


def test_an_instance_sharing_a_development_identifier_fails() -> None:
    seed, mechanism = DEV_INSTANCES["docthread-dev-1"]
    row = C.check(
        "docthread-dev-1",
        seed,
        mechanism,
        C.SIZE_READINGS["plan"],
        C.identifiers(instance("docthread-dev-1")),
    )
    assert not row["valid"] and row["failed"] == ["no_dev_identifiers"]


def test_the_manifest_holds_no_answer_identity_or_location() -> None:
    fx = instance("docthread-dev-2")
    manifest = C.check_all("plan", count=1, prefix="synthetic", seed_list=lambda k: [9302],
                           make=lambda n, s, m: fx, dev=("docthread-dev-1",))  # fmt: skip
    text = json.dumps(manifest)
    assert manifest["all_valid"] and manifest["config_instances"] == [
        {"name": "synthetic-1", "seed": 9302, "mechanism": "lease"}]  # fmt: skip
    hidden = [
        *map(str, fx.facts.values()),
        *fx.components,
        *fx.names.values(),
        fx.facts["evidence_event"][:40],
    ]
    assert not [
        h for h in hidden if mentions(text, h)
    ]  # as whole tokens, as the scorer looks for facts
    assert not {"layout", "first_rejection_page", "evidence_page", "facts", "sources"} & set(
        json.dumps(manifest["instances"][0]["tried"][0]).replace('"', " ").split()
    )


def test_invalid_seeds_are_skipped_by_the_declared_rule() -> None:
    assert C.seeds_for(1)[:3] == [7401, 7407, 7413] and C.seeds_for(6)[:2] == [7406, 7412]
    assert {s for k in range(1, 7) for s in C.seeds_for(k)}.isdisjoint({7301, 7302})
    good = instance("docthread-dev-1")
    calls: list[int] = []

    def make(name: str, seed: int, mechanism: str) -> Any:
        calls.append(seed)
        if seed in (1, 2):
            raise RuntimeError("could not generate")
        return good

    manifest = C.check_all(
        "plan", count=1, prefix="synthetic", seed_list=lambda k: [1, 2, 3, 4], make=make, dev=()
    )
    row = manifest["instances"][0]
    assert calls == [1, 2, 3] and row["seed"] == 3 and row["skipped"] == [1, 2]
    assert [t["failed"] for t in row["tried"][:2]] == [["generates"], ["generates"]]


def test_a_failed_check_is_recorded_by_name_only() -> None:
    good = instance("docthread-dev-1")

    class Broken:  # the journal layout outside the declared range; everything else as the real instance
        def __getattr__(self, attr: str) -> Any:
            return getattr(good, attr)

        layout: ClassVar[dict[str, Any]] = {**good.layout, "first_rejection_page": 3}

    row = C.check(
        "synthetic", 1, "lease", C.SIZE_READINGS["plan"], set(), make=lambda n, s, m: Broken()
    )
    assert row["failed"] == ["journal_layout"] and "first_rejection_page" not in json.dumps(row)


def test_the_command_line_refuses_without_the_freeze_flag_or_a_size_reading(tmp_path: Path) -> None:
    assert C.main([str(tmp_path / "m.json"), "--size-reading", "plan"]) == 2
    assert not (tmp_path / "m.json").exists()
    with pytest.raises(SystemExit):
        C.main([str(tmp_path / "m.json"), "--confirm-freeze"])  # the reading must be chosen
    (tmp_path / "m.json").write_text("{}")
    assert (
        C.main([str(tmp_path / "m.json"), "--size-reading", "plan", "--confirm-freeze"]) == 2
    )  # written once
