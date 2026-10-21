"""Partnership analysis for a match.

A partnership is the stand two batsmen put together between one wicket and the
next. The cricket rules live in one place - :mod:`app.scoring.partnerships`, the
same module the live scorecard reads - and this builder only shapes the result
for the endpoint: for each innings, the opening stand, the biggest stand and the
runs, balls, run rate and per-batsman contribution of every stand in between.

The winning-ball rule is mirrored there too: the delivery that first reaches a
chase target never registers its wicket, so the stand it would have ended is
reported unbroken, exactly as the scorecard reports it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.models import Innings
from app.scoring.partnerships import ILLEGAL_BALL_EXTRAS, Partnership, compute_partnerships


def _extra(delivery) -> str:
    value = delivery.extra_type
    return value.value if hasattr(value, "value") else (value or "none")


def _run_rate(runs: int, balls: int) -> float:
    return round(runs / (balls / 6), 2) if balls else 0.0


def _name(identities: dict, player_id: int | None) -> str | None:
    if player_id is None:
        return None
    first, last, _code = identities.get(player_id, (None, None, None))
    return full_name(first, last) or f"Player {player_id}"


def _batter(identities: dict, stand: Partnership, player_id: int) -> dict:
    batter = stand.batter_by_id(player_id)
    return {
        "player_id": player_id,
        "name": _name(identities, player_id),
        "runs": batter.runs if batter is not None else 0,
        "balls": batter.balls if batter is not None else 0,
        "contribution_percentage": stand.contribution_of(player_id),
    }


def _entry(number: int, stand: Partnership, identities: dict) -> dict:
    return {
        "partnership_number": number,
        "batsman_1_id": stand.batter1.player_id,
        "batsman_1_name": _name(identities, stand.batter1.player_id),
        "batsman_2_id": stand.batter2.player_id,
        "batsman_2_name": _name(identities, stand.batter2.player_id),
        "runs": stand.total_runs,
        "balls": stand.total_balls,
        "run_rate": stand.run_rate,
        # A completed stand is the fall for the wicket it ended on, and the
        # stands are numbered in that same order.
        "for_wicket": number,
        "unbroken": stand.unbroken,
        "player_out_id": stand.player_out_id,
        "player_out_name": _name(identities, stand.player_out_id),
        # Per-batsman breakdown. ``total_runs`` mirrors ``runs`` and is the stand
        # total including extras, so it need not equal the two batsmen's sum.
        "total_runs": stand.total_runs,
        "total_balls": stand.total_balls,
        "batter1": _batter(identities, stand, stand.batter1.player_id),
        "batter2": _batter(identities, stand, stand.batter2.player_id),
    }


def _biggest(entries: list[dict]) -> dict | None:
    if not entries:
        return None
    return max(entries, key=lambda e: (e["runs"], e["balls"], -e["partnership_number"]))


async def get_partnership_analysis(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Build the partnership payload for every innings of ``match``."""
    stmt = select(Innings).where(Innings.match_id == match.id)
    if innings_number is not None:
        stmt = stmt.where(Innings.innings_number == innings_number)
    result = await db.execute(stmt.order_by(Innings.innings_number, Innings.id))
    innings_list = list(result.scalars().all())

    repo = ScoringRepository(db)
    frames: list[dict] = []
    overall: dict | None = None

    for innings in innings_list:
        deliveries = await repo.list_deliveries(innings.id)
        player_ids: set[int] = set()
        for d in deliveries:
            player_ids.update((d.striker_id, d.non_striker_id))
            if d.dismissed_player_id is not None:
                player_ids.add(d.dismissed_player_id)
        identities = await repo.player_identities(player_ids)

        stands = compute_partnerships(deliveries, innings.target)
        entries = [_entry(number, stand, identities) for number, stand in enumerate(stands, 1)]
        legal_balls = sum(1 for d in deliveries if _extra(d) not in ILLEGAL_BALL_EXTRAS)
        total = sum(d.runs_batsman + d.runs_extras for d in deliveries)
        wickets = sum(1 for stand in stands if not stand.unbroken)

        highest = _biggest(entries)
        if highest is not None and (
            overall is None
            or (highest["runs"], highest["balls"]) > (overall["runs"], overall["balls"])
        ):
            overall = highest

        # The stand still at the crease, if any: the trailing unbroken entry.
        current = entries[-1] if entries and entries[-1]["unbroken"] else None

        frames.append(
            {
                "innings_id": innings.id,
                "innings_number": innings.innings_number,
                "batting_team_id": innings.batting_team_id,
                "bowling_team_id": innings.bowling_team_id,
                "is_super_over": innings.is_super_over,
                "total": total,
                "wickets": wickets,
                "legal_balls": legal_balls,
                "overs_bowled_str": f"{legal_balls // 6}.{legal_balls % 6}",
                "partnership_count": len(entries),
                "opening_partnership": entries[0] if entries else None,
                "highest_partnership": highest,
                "current_partnership": current,
                "partnerships": entries,
            }
        )

    return {"match_id": match.id, "highest_partnership": overall, "innings": frames}


__all__ = ["get_partnership_analysis"]
