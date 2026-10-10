"""Ball-by-ball analytics for an innings.

Turns the stored delivery log into everything a scorecard's "how did it
happen" view needs, without changing how a delivery is scored. The engine
remains the single source of truth for totals, wickets and the winning ball;
this module replays the same log from zero and reports it chronologically:

- every delivery in order, with runs, extras, the dismissal it caused and the
  running score / run rate after it,
- over-by-over totals and the run rate of each over,
- the scoring pattern (dots, singles, twos, threes, fours, sixes, boundaries),
- an extras breakdown (wides, no-balls, byes, leg byes),
- the progression of the cumulative run rate through the innings.

The winning-ball rule is deliberately mirrored from the engine: the delivery
that first reaches the chase target is not allowed to register a wicket, so
the analytics wickets always agree with the scorecard.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.scoring.crud import ScoringRepository
from app.scoring.enums import ExtraType, MatchFormat
from app.scoring.models import Innings

_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE.value, ExtraType.NO_BALL.value)
_MAX_OVERS_BY_FORMAT = {
    MatchFormat.T20.value: 20,
    MatchFormat.ODI.value: 50,
    MatchFormat.TEST.value: None,
}


def _extra(delivery) -> str:
    value = delivery.extra_type
    return value.value if isinstance(value, ExtraType) else (value or ExtraType.NONE.value)


def _wicket(delivery) -> str | None:
    value = delivery.wicket_type
    if not value:
        return None
    return value.value if hasattr(value, "value") else value


def _name(identities: dict, player_id: int | None) -> str | None:
    if player_id is None:
        return None
    first, last, _code = identities.get(player_id, (None, None, None))
    if first or last:
        return f"{first or ''} {last or ''}".strip()
    return f"Player {player_id}"


def _overs_str(legal_balls: int) -> str:
    full, rem = divmod(legal_balls, 6)
    return f"{full}.{rem}"


def _run_rate(runs: int, legal_balls: int) -> float:
    return round(runs / (legal_balls / 6), 2) if legal_balls else 0.0


def _display(delivery, extra: str, wicket: str | None) -> str:
    if wicket:
        return "W"
    if extra == ExtraType.WIDE.value:
        return f"{delivery.runs_extras}wd"
    if extra == ExtraType.NO_BALL.value:
        return f"{delivery.runs_batsman + delivery.runs_extras}nb"
    if extra == ExtraType.BYE.value:
        return f"{delivery.runs_extras}b"
    if extra == ExtraType.LEG_BYE.value:
        return f"{delivery.runs_extras}lb"
    return str(delivery.runs_batsman)


def _winning_delivery_id(innings, deliveries) -> int | None:
    """The id of the delivery that first reaches the chase target, if any.

    Mirrors the engine: the run chase ends the instant the target is reached,
    and a wicket on that same delivery is never registered.
    """
    if innings.target is None:
        return None
    running = 0
    for d in deliveries:
        running += d.runs_batsman + d.runs_extras
        if running >= innings.target:
            return d.id
    return None


def build_innings_analytics(
    innings,
    deliveries,
    identities: dict[int, tuple],
    max_overs: int | None = None,
) -> dict:
    winning_id = _winning_delivery_id(innings, deliveries)

    total = 0
    wickets = 0
    legal_balls = 0
    events: list[dict] = []

    over_runs: dict[int, int] = {}
    over_wickets: dict[int, int] = {}
    over_legal: dict[int, int] = {}
    over_extras: dict[int, int] = {}
    over_fours: dict[int, int] = {}
    over_sixes: dict[int, int] = {}
    over_dots: dict[int, int] = {}
    over_bowler: dict[int, int] = {}
    over_order: list[int] = []
    seen_overs: set[int] = set()

    extras = {"wides": 0, "no_balls": 0, "byes": 0, "leg_byes": 0}
    pattern = {
        "deliveries": 0,
        "legal_balls": 0,
        "dot_balls": 0,
        "singles": 0,
        "doubles": 0,
        "triples": 0,
        "fours": 0,
        "sixes": 0,
        "runs_off_bat": 0,
    }

    for d in deliveries:
        extra = _extra(d)
        wicket = _wicket(d)
        count_wicket = wicket is not None and d.id != winning_id
        runs = d.runs_batsman + d.runs_extras
        is_legal = extra not in _ILLEGAL_BALL_EXTRAS
        is_four = d.runs_batsman == 4
        is_six = d.runs_batsman == 6
        is_boundary = is_four or is_six
        is_dot = is_legal and runs == 0

        total += runs
        if count_wicket:
            wickets += 1
        if is_legal:
            legal_balls += 1

        if d.over_number not in seen_overs:
            seen_overs.add(d.over_number)
            over_order.append(d.over_number)
        over_runs[d.over_number] = over_runs.get(d.over_number, 0) + runs
        over_wickets[d.over_number] = over_wickets.get(d.over_number, 0) + (
            1 if count_wicket else 0
        )
        over_legal[d.over_number] = over_legal.get(d.over_number, 0) + (1 if is_legal else 0)
        over_extras[d.over_number] = over_extras.get(d.over_number, 0) + d.runs_extras
        over_fours[d.over_number] = over_fours.get(d.over_number, 0) + (1 if is_four else 0)
        over_sixes[d.over_number] = over_sixes.get(d.over_number, 0) + (1 if is_six else 0)
        over_dots[d.over_number] = over_dots.get(d.over_number, 0) + (1 if is_dot else 0)
        if d.over_number not in over_bowler:
            over_bowler[d.over_number] = d.bowler_id

        pattern["deliveries"] += 1
        if is_legal:
            pattern["legal_balls"] += 1
        if is_dot:
            pattern["dot_balls"] += 1
        pattern["runs_off_bat"] += d.runs_batsman
        if d.runs_batsman == 1:
            pattern["singles"] += 1
        elif d.runs_batsman == 2:
            pattern["doubles"] += 1
        elif d.runs_batsman == 3:
            pattern["triples"] += 1
        elif is_four:
            pattern["fours"] += 1
        elif is_six:
            pattern["sixes"] += 1

        if extra == ExtraType.WIDE.value:
            extras["wides"] += d.runs_extras
        elif extra == ExtraType.NO_BALL.value:
            extras["no_balls"] += d.runs_extras
        elif extra == ExtraType.BYE.value:
            extras["byes"] += d.runs_extras
        elif extra == ExtraType.LEG_BYE.value:
            extras["leg_byes"] += d.runs_extras

        events.append(
            {
                "over_number": d.over_number,
                "ball_number": d.ball_number,
                "ball_label": f"{d.over_number - 1}.{d.ball_number}",
                "striker_id": d.striker_id,
                "striker_name": _name(identities, d.striker_id),
                "non_striker_id": d.non_striker_id,
                "non_striker_name": _name(identities, d.non_striker_id),
                "bowler_id": d.bowler_id,
                "bowler_name": _name(identities, d.bowler_id),
                "runs_batsman": d.runs_batsman,
                "runs_extras": d.runs_extras,
                "total_runs": runs,
                "extra_type": extra,
                "is_legal_ball": is_legal,
                "is_boundary": is_boundary,
                "is_four": is_four,
                "is_six": is_six,
                "is_dot_ball": is_dot,
                "wicket_type": wicket,
                "dismissed_player_id": d.dismissed_player_id if count_wicket else None,
                "dismissed_player_name": (
                    _name(identities, d.dismissed_player_id) if count_wicket else None
                ),
                "team_total": total,
                "team_wickets": wickets,
                "legal_balls": legal_balls,
                "run_rate": _run_rate(total, legal_balls),
                "display": _display(d, extra, wicket if count_wicket else None),
            }
        )

    over_by_over: list[dict] = []
    run_rate_progression: list[dict] = []
    cum_runs = 0
    cum_wickets = 0
    cum_legal = 0
    for over in sorted(over_order):
        o_runs = over_runs[over]
        o_wickets = over_wickets[over]
        o_legal = over_legal[over]
        cum_runs += o_runs
        cum_wickets += o_wickets
        cum_legal += o_legal
        bowler_id = over_bowler.get(over)
        over_by_over.append(
            {
                "over_number": over,
                "bowler_id": bowler_id,
                "bowler_name": _name(identities, bowler_id),
                "legal_balls": o_legal,
                "runs": o_runs,
                "wickets": o_wickets,
                "extras": over_extras[over],
                "fours": over_fours[over],
                "sixes": over_sixes[over],
                "dot_balls": over_dots[over],
                "run_rate": _run_rate(o_runs, o_legal),
                "cumulative_runs": cum_runs,
                "cumulative_wickets": cum_wickets,
                "cumulative_run_rate": _run_rate(cum_runs, cum_legal),
            }
        )
        required = None
        if innings.target is not None and max_overs:
            remaining_balls = max_overs * 6 - cum_legal
            if remaining_balls > 0:
                required = round((innings.target - cum_runs) / (remaining_balls / 6), 2)
        run_rate_progression.append(
            {
                "over_number": over,
                "legal_balls": cum_legal,
                "cumulative_runs": cum_runs,
                "cumulative_wickets": cum_wickets,
                "run_rate": _run_rate(cum_runs, cum_legal),
                "required_run_rate": required,
            }
        )

    return {
        "innings_id": innings.id,
        "innings_number": innings.innings_number,
        "batting_team_id": innings.batting_team_id,
        "bowling_team_id": innings.bowling_team_id,
        "is_super_over": innings.is_super_over,
        "total": total,
        "wickets": wickets,
        "legal_balls": legal_balls,
        "overs_bowled": round(legal_balls / 6, 2),
        "overs_bowled_str": _overs_str(legal_balls),
        "run_rate": _run_rate(total, legal_balls),
        "target": innings.target,
        "extras": {
            "wides": extras["wides"],
            "no_balls": extras["no_balls"],
            "byes": extras["byes"],
            "leg_byes": extras["leg_byes"],
            "total": extras["wides"] + extras["no_balls"] + extras["byes"] + extras["leg_byes"],
        },
        "scoring_pattern": {
            **pattern,
            "boundaries": pattern["fours"] + pattern["sixes"],
            "boundary_runs": pattern["fours"] * 4 + pattern["sixes"] * 6,
            "extras": sum(extras.values()),
        },
        "deliveries": events,
        "over_by_over": over_by_over,
        "run_rate_progression": run_rate_progression,
    }


def _max_overs_for(match: Match, innings) -> int | None:
    if innings.is_super_over:
        return 1
    try:
        return _MAX_OVERS_BY_FORMAT.get(MatchFormat(str(match.match_type).upper()).value)
    except ValueError:
        return None


async def _list_innings(
    db: AsyncSession, match_id: int, innings_number: int | None = None
) -> list[Innings]:
    stmt = select(Innings).where(Innings.match_id == match_id)
    if innings_number is not None:
        stmt = stmt.where(Innings.innings_number == innings_number)
    result = await db.execute(stmt.order_by(Innings.innings_number, Innings.id))
    return list(result.scalars().all())


async def get_match_analytics(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Build the ball-by-ball analytics for every innings of ``match``.

    ``innings_number`` narrows the payload to a single innings (including a
    Super Over, which is just another numbered innings).
    """
    innings_list = await _list_innings(db, match.id, innings_number)
    repo = ScoringRepository(db)

    output = []
    for innings in innings_list:
        deliveries = await repo.list_deliveries(innings.id)
        player_ids = set()
        for d in deliveries:
            player_ids.update((d.striker_id, d.non_striker_id, d.bowler_id, d.dismissed_player_id))
        identities = await repo.player_identities(player_ids)
        output.append(
            build_innings_analytics(innings, deliveries, identities, _max_overs_for(match, innings))
        )

    return {"match_id": match.id, "innings": output}


__all__ = [
    "build_innings_analytics",
    "get_match_analytics",
]
