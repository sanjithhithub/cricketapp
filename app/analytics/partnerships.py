"""Partnership analysis for a match.

A partnership is the stand two batsmen put together between one wicket and the
next. Replaying the delivery log and cutting it at every wicket gives, for each
innings, the opening stand, the biggest stand and the runs, balls and run rate of
every stand in between - the figures a "who batted with whom" panel is built
from.

The winning-ball rule is mirrored from the engine: the delivery that first
reaches a chase target never registers its wicket, so the stand it would have
ended is reported unbroken, exactly as the scorecard reports it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.enums import ExtraType
from app.scoring.models import Innings

# Wides and no-balls are penalty deliveries: they never count as a ball of the
# over, so a partnership's ball count excludes them.
_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE.value, ExtraType.NO_BALL.value)


def _extra(delivery) -> str:
    value = delivery.extra_type
    return value.value if isinstance(value, ExtraType) else (value or ExtraType.NONE.value)


def _run_rate(runs: int, balls: int) -> float:
    return round(runs / (balls / 6), 2) if balls else 0.0


def _name(identities: dict, player_id: int | None) -> str | None:
    if player_id is None:
        return None
    first, last, _code = identities.get(player_id, (None, None, None))
    return full_name(first, last) or f"Player {player_id}"


def _winning_delivery_id(innings: Innings, deliveries) -> int | None:
    if innings.target is None:
        return None
    running = 0
    for d in deliveries:
        running += d.runs_batsman + d.runs_extras
        if running >= innings.target:
            return d.id
    return None


def _stands(innings: Innings, deliveries) -> list[dict]:
    """Cut the delivery log into stands, one per pair, in order."""
    winning_id = _winning_delivery_id(innings, deliveries)
    stands: list[dict] = []
    current: dict | None = None
    for d in deliveries:
        if current is None:
            current = {
                "batsman_1_id": d.striker_id,
                "batsman_2_id": d.non_striker_id,
                "runs": 0,
                "balls": 0,
                "player_out_id": None,
                "unbroken": True,
            }
        if _extra(d) not in _ILLEGAL_BALL_EXTRAS:
            current["balls"] += 1
        current["runs"] += d.runs_batsman + d.runs_extras
        if d.wicket_type is not None and d.id != winning_id:
            current["player_out_id"] = d.dismissed_player_id
            current["unbroken"] = False
            stands.append(current)
            current = None
    if current is not None:
        stands.append(current)
    return stands


def _entry(number: int, stand: dict, identities: dict) -> dict:
    return {
        "partnership_number": number,
        "batsman_1_id": stand["batsman_1_id"],
        "batsman_1_name": _name(identities, stand["batsman_1_id"]),
        "batsman_2_id": stand["batsman_2_id"],
        "batsman_2_name": _name(identities, stand["batsman_2_id"]),
        "runs": stand["runs"],
        "balls": stand["balls"],
        "run_rate": _run_rate(stand["runs"], stand["balls"]),
        # A completed stand is the fall for the wicket it ended on, and the
        # stands are numbered in that same order.
        "for_wicket": number,
        "unbroken": stand["unbroken"],
        "player_out_id": stand["player_out_id"],
        "player_out_name": _name(identities, stand["player_out_id"]),
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

        stands = _stands(innings, deliveries)
        entries = [_entry(number, stand, identities) for number, stand in enumerate(stands, 1)]
        legal_balls = sum(1 for d in deliveries if _extra(d) not in _ILLEGAL_BALL_EXTRAS)
        total = sum(d.runs_batsman + d.runs_extras for d in deliveries)
        wickets = sum(1 for stand in stands if not stand["unbroken"])

        highest = _biggest(entries)
        if highest is not None and (
            overall is None
            or (highest["runs"], highest["balls"]) > (overall["runs"], overall["balls"])
        ):
            overall = highest

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
                "partnerships": entries,
            }
        )

    return {"match_id": match.id, "highest_partnership": overall, "innings": frames}


__all__ = ["get_partnership_analysis"]
