"""Error types, kept in one place so the poll loop's except-clauses read as
the decision table they actually are.

The distinction that matters most is between errors raised BEFORE any
number is consumed and errors raised after. A ValidationError costs
nothing and leaves the asset flagged for a human to fix. A LayoutError or
QueueError raised after a reserve has already burned a block of numbers,
and must be logged with the range and the recovery command (spec 5.3).
"""
from __future__ import annotations


class StartupError(ValueError):
    """Bad config found at startup. The service must not start."""


class ValidationError(ValueError):
    """The asset cannot be labelled as it stands. Nothing was written to
    Halo, no numbers were consumed, and the asset stays flagged."""


class ReserveError(RuntimeError):
    """The reservation did not complete or could not be verified. Treated
    as "no numbers consumed" -- the operation is retried next poll."""


class LayoutError(ValueError):
    """The identifier cannot be laid out legibly on the configured media.

    Raised by layout.check_fits during validation (costing nothing), and
    defensively by the renderer afterwards. If it escapes the renderer, a
    block has already been reserved.
    """
