"""Value types shared across the service.

All frozen: a CableType read from Halo, a Plan derived from it, and a
Reservation confirmed against Halo are three distinct things, and the
transitions between them are the only places the counter can move. Making
them immutable keeps "has this been reserved yet?" answerable by type
rather than by reading the surrounding code.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CableType:
    """One Halo asset record defining a family of cables (spec 3.1)."""

    id: int
    prefix: str
    next_id: int
    qty: int
    name: str = ""
    # For the printer's job list: the colour field's text, if the tenant has
    # one, and the asset's type as Halo names it ("CAT6 Cable").
    color: str = ""
    type_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """A validated, NOT yet reserved intention to issue a block of numbers.

    Holding a Plan means every check that can be made without writing to
    Halo has passed. It does not mean any number has been consumed -- that
    only happens when allocator.reserve() turns this into a Reservation.
    """

    asset_id: int
    prefix: str
    first_number: int
    last_number: int
    cable_count: int
    labels_per_cable: int
    requested_qty: int
    clamped: bool = False
    color: str = ""
    type_name: str = ""

    @property
    def next_counter_value(self) -> int:
        """What Next Cable ID becomes once this block is reserved."""
        return self.last_number + 1

    @property
    def label_count(self) -> int:
        return self.cable_count * self.labels_per_cable

    @property
    def numbers(self) -> list[int]:
        return list(range(self.first_number, self.last_number + 1))


@dataclass(frozen=True)
class Reservation:
    """A Plan whose numbers are now consumed in Halo and verified by
    read-back. Numbers in a Reservation can never be issued again; if the
    job that follows fails, the correct outcome is a gap (spec 4.1)."""

    plan: Plan
    identifiers: tuple[str, ...]
    reserved_utc: str

    @property
    def asset_id(self) -> int:
        return self.plan.asset_id

    @property
    def prefix(self) -> str:
        return self.plan.prefix

    @property
    def label_sequence(self) -> list[str]:
        """Every label to print, in printing order: adjacent pairs, so the
        operator takes two in a row for the two ends of one cable (5.4)."""
        out: list[str] = []
        for identifier in self.identifiers:
            out.extend([identifier] * self.plan.labels_per_cable)
        return out
