"""Endings raised by the paper and summary arms (guarded-clm raises its own). Each maps to one ending kind."""

from __future__ import annotations


class ArmContextLimit(RuntimeError):
    """The request could not be brought within the context limit by the arm's own rules (`context_limit`)."""


class ArmStepLimit(RuntimeError):
    """The turn reached its task-step limit (`step_limit`)."""


class SummaryCallCap(RuntimeError):
    """The turn reached its summariser-call cap (`summary_call_cap`)."""


class ArmRequestIncompatible(RuntimeError):
    """The arm could not build a request that keeps the provider's documented structure for the turn in progress
    (`request_incompatible`, a harness outcome): nothing was dispatched."""
