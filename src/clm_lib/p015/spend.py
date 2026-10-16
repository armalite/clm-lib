"""Spending controls for the 015 launcher (`p015-spend/1`): a programme ceiling and a block stop, both read from an
authorisation file that is reread before every user turn and every provider attempt.

The authorisation file (JSON), written by the maintainer, never by this code:

    {"experiment": "015", "launch_id": "<launch>", "programme_ceiling_usd": 0.0, "block_stop_usd": 0.0,
     "live": false, "granted_by": "...", "granted_at": "...", "reason": "..."}

- **Programme spend:** every ledger entry of kind `p015:*`, whichever launch made it, plus open reservations.
- **Block spend:** the entries of this launch (run ids starting `<launch_id>.`), plus open reservations.
- **Open reservations:** this process's, and any another process has persisted as pending in the ledger file.
- A refusal is raised **before** the ledger reservation, so a refused attempt never reaches the model.
- Confirmed cost (provider usage at the dated price) is kept apart from charges assumed at the reservation (unknown
  outcomes) and from reservations still open.

An authorisation is for one experiment and one launch: a file naming another launch is refused, so unspent
authorisation never carries over. Nothing here grants, raises or writes an authorisation or a ledger ceiling.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..budget import Ledger
from .accounting import LaunchStopped, SpendRefused

SPEND_VERSION = "p015-spend/1"
PROGRAMME_KIND = "p015:"


@dataclass
class SpendGate:
    ledger: Ledger
    authorisation_path: Path
    launch_id: str
    experiment: str = "015"
    stop_path: Path | None = None  # a file whose content `now` stops before the next attempt

    def authorisation(self) -> dict[str, Any]:
        try:
            auth = json.loads(self.authorisation_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise SpendRefused(
                f"no readable authorisation at {self.authorisation_path.name}", "not_authorised"
            ) from exc
        if auth.get("experiment") != self.experiment or auth.get("launch_id") != self.launch_id:
            raise SpendRefused(
                "the authorisation is for another experiment or launch", "not_authorised"
            )
        for key in ("programme_ceiling_usd", "block_stop_usd"):
            if not isinstance(auth.get(key), int | float) or auth[key] < 0:
                raise SpendRefused(f"the authorisation has no valid {key}", "not_authorised")
        return auth

    def _other_pending(self) -> float:
        try:
            data = json.loads(self.ledger.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return 0.0
        return sum(
            float(p["reserved_usd"])
            for p in data.get("pending", [])
            if int(p.get("pid", -1)) != os.getpid()
        )

    def position(self) -> dict[str, Any]:
        entries = self.ledger.entries
        programme = [e for e in entries if str(e.get("kind", "")).startswith(PROGRAMME_KIND)]
        block = [e for e in programme if str(e.get("run_id", "")).startswith(f"{self.launch_id}.")]

        def split(items: list[dict[str, Any]]) -> dict[str, float]:
            confirmed = sum(
                float(e["charged_usd"]) for e in items if e.get("cost_basis") == "provider_usage"
            )
            assumed = sum(
                float(e["charged_usd"]) for e in items if e.get("cost_basis") != "provider_usage"
            )
            return {"confirmed_usd": round(confirmed, 6), "assumed_usd": round(assumed, 6)}

        open_usd = self.ledger.reserved_open_usd + self._other_pending()
        return {
            "programme": split(programme),
            "block": split(block),
            "open_reservations_usd": round(open_usd, 6),
            "ledger_remaining_usd": round(self.ledger.remaining_usd, 6),
        }

    def _stop_requested(self) -> bool:
        if self.stop_path is None or not self.stop_path.exists():
            return False
        return self.stop_path.read_text(encoding="utf-8").strip() == "now"

    def check(self, amount_usd: float, category: str) -> None:
        """Before a provider attempt: refuse if it cannot fit both the programme ceiling and the block stop."""
        if self._stop_requested():
            raise LaunchStopped("a stop was requested before the next provider attempt")
        auth = self.authorisation()
        pos = self.position()
        open_usd = pos["open_reservations_usd"]
        programme_spent = pos["programme"]["confirmed_usd"] + pos["programme"]["assumed_usd"]
        block_spent = pos["block"]["confirmed_usd"] + pos["block"]["assumed_usd"]
        programme_room = float(auth["programme_ceiling_usd"]) - programme_spent - open_usd
        block_room = float(auth["block_stop_usd"]) - block_spent - open_usd
        if amount_usd > programme_room + 1e-12:
            raise SpendRefused(
                f"a {category} attempt needs ${amount_usd:.4f}; the programme ceiling leaves ${programme_room:.4f}",
                "programme_ceiling",
            )
        if amount_usd > block_room + 1e-12:
            raise SpendRefused(
                f"a {category} attempt needs ${amount_usd:.4f}; the block stop leaves ${block_room:.4f}",
                "block_stop",
            )

    def check_turn(self) -> None:
        """Before a user turn: an authorisation must exist and leave room in both allowances."""
        if self._stop_requested():
            raise LaunchStopped("a stop was requested before the next turn")
        self.check(0.0, "turn")
        auth = self.authorisation()
        pos = self.position()
        for scope, limit_key in (
            ("programme", "programme_ceiling_usd"),
            ("block", "block_stop_usd"),
        ):
            spent = (
                pos[scope]["confirmed_usd"]
                + pos[scope]["assumed_usd"]
                + pos["open_reservations_usd"]
            )
            if spent >= float(auth[limit_key]):
                raise SpendRefused(f"the {scope} allowance is used up", f"{scope}_exhausted")
