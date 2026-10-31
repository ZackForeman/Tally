"""Turn messy read values into comparable keys."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

_COMPANY_SUFFIXES = re.compile(r"\b(ltd|limited|plc|llp)\b\.?")


def ticket_key(ticket_no: str | None) -> str | None:
    """Rule 1 key: strip spaces, dashes and leading zeros."""
    if not ticket_no:
        return None
    key = re.sub(r"[\s\-]", "", ticket_no).upper().lstrip("0")
    return key or None


def reg_key(reg: str | None) -> str | None:
    if not reg:
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", reg).upper()
    return key or None


def customer_key(name: str) -> str:
    name = _COMPANY_SUFFIXES.sub("", name.casefold())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def driver_key(name: str | None) -> str | None:
    return customer_key(name) if name else None


@dataclass(frozen=True)
class SnappedReg:
    raw: str | None
    reg: str | None  # fleet reg, or the cleaned raw reg if it couldn't be snapped
    snapped: bool  # the read differed from the fleet reg it was snapped to


def snap_reg(raw: str | None, fleet: list[str], max_distance: int = 1) -> SnappedReg:
    """Snap a read reg to the one fleet reg within ``max_distance`` edits.

    Handles misreads such as NX66AYE for NX66AYF. If two fleet regs are equally
    close the read is left alone, since guessing would hide a real ambiguity.
    """
    key = reg_key(raw)
    if key is None or not fleet:
        return SnappedReg(raw, key, False)
    fleet_keys = [reg_key(r) for r in fleet]
    if key in fleet_keys:
        return SnappedReg(raw, key, False)
    near = [f for f in fleet_keys if f and Levenshtein.distance(key, f) <= max_distance]
    if len(near) == 1:
        return SnappedReg(raw, near[0], True)
    return SnappedReg(raw, key, False)
