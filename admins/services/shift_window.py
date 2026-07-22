"""Canonical effective window for shift-owned reporting.

An absent ``end_time`` means "running" only while the shift itself is ACTIVE.
Legacy, repaired, or corrupt non-active rows must fail closed to an empty
window; otherwise they keep claiming every later order from the same cashier.
"""

from django.utils import timezone


def effective_shift_end(shift, *, now=None):
    """Return the half-open reporting end for ``shift``.

    Stored ends are authoritative.  A genuinely active shift runs to the
    supplied shared ``now`` (or the current time).  Any other null-end shift is
    treated as a zero-length window at its start, matching the core shift list
    and preventing unfrozen history from absorbing later sales.
    """
    if shift.end_time is not None:
        return shift.end_time
    if shift.status == 'ACTIVE':
        return now or timezone.now()
    return shift.start_time
