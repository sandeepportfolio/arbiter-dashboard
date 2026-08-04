"""Auto-expire confirmed mappings whose embedded event date has passed.

The discovery pipeline walks ``status='candidate'`` records and promotes
them to ``confirmed``, but no existing path demotes a confirmed mapping
once the underlying event has settled. For sports games — which currently
make up the bulk of confirmed mappings — that leaves a growing tail of
``allow_auto_trade=True`` rows pointing at past-dated markets the venues
have already resolved. This module provides a date-parsing helper plus an
``expire_settled_confirmed_mappings`` sweep that demotes those rows to
status=EXPIRED with allow_auto_trade=False.

Date parsing is conservative: only the ``PREFIX_LEAGUE_YYYYMMDD_TEAM_...``
shape used by sports-game canonical_ids is recognized. Election-control
ids (``GOP_HOUSE_2026``), crypto-strike ids (``CRYPT_D_26APR2207_T...``),
and any future format that lacks an unambiguous 8-digit settlement date
are skipped — better to leave them alone than to mis-expire a market that
is still active.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from .market_map import MappingStatus

logger = logging.getLogger("arbiter.mapping.expire_settled")

# Sports canonical_ids carry the event date in US Eastern time, but the
# container clock is UTC — the calendar flips at 00:00 UTC (8 PM ET) while
# that evening's slate is still being played. Expiring at the naive UTC
# date boundary demoted every same-day game mapping mid-slate, exactly the
# in-game window where live arbs cluster. The grace pushes the cutoff so a
# date-D event survives until D+1 09:00 UTC (~4-5 AM ET), safely past the
# last West Coast finish (~06:30 UTC).
_SETTLED_GRACE_HOURS = 9


def settled_cutoff_date(now: Optional[datetime] = None) -> date:
    """The date before which an event is considered settled."""
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(hours=_SETTLED_GRACE_HOURS)).date()


# Anchored to the start of the id; the date token must be exactly 8 digits
# and sit between two underscored segments. This rejects single-token
# prefixes (no underscore before the date) and ids whose 8-digit run is
# embedded in a longer numeric token.
_DATED_CANONICAL_ID_RE = re.compile(r"^[A-Z]+(?:_[A-Z]+)+_(\d{8})_[A-Z]")


def parse_settled_date(canonical_id: str) -> Optional[date]:
    """Return the YYYYMMDD date embedded in a sports-game canonical_id.

    Returns ``None`` for any id whose shape does not match the strict
    sports-game pattern (PREFIX_LEAGUE_YYYYMMDD_TEAM_...), or whose
    8-digit run does not parse as a valid calendar date.
    """
    if not canonical_id:
        return None
    match = _DATED_CANONICAL_ID_RE.match(canonical_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


async def expire_settled_confirmed_mappings(
    store,
    *,
    today: Optional[date] = None,
) -> List[str]:
    """Demote confirmed mappings whose embedded event date is strictly
    before ``today`` (default: ``settled_cutoff_date()``, which lags the
    UTC calendar by ``_SETTLED_GRACE_HOURS`` so same-day evening games
    survive the 00:00 UTC date flip).

    Walks ``store.iter_confirmed()`` (no auto-trade filter — we want to
    catch confirmed-but-disabled rows too), parses the event date, and
    for any with date < today flips status → EXPIRED and
    allow_auto_trade → False via ``store.update_status``.

    Per-mapping update failures are logged and swallowed so a single bad
    row cannot abort the whole sweep. Returns the list of canonical_ids
    that were successfully demoted.
    """
    cutoff = today or settled_cutoff_date()
    expired: List[str] = []

    async for canonical_id, _mapping in store.iter_confirmed():
        event_date = parse_settled_date(canonical_id)
        if event_date is None or event_date >= cutoff:
            continue
        note = (
            f"Auto-expired {cutoff.isoformat()}: event date "
            f"{event_date.isoformat()} is in the past."
        )
        try:
            await store.update_status(
                canonical_id,
                MappingStatus.EXPIRED,
                review_note=note,
                allow_auto_trade=False,
            )
            expired.append(canonical_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "expire_settled: update_status failed for %s: %s",
                canonical_id, exc,
            )

    if expired:
        logger.info(
            "expire_settled: demoted %d settled-date mapping(s) to EXPIRED",
            len(expired),
        )
    return expired
