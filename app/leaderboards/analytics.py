"""Account-wide batting and bowling leaderboards.

Ranks every player across all of an account's completed matches, not inside one
competition. The batting boards answer six questions about batting: who scores
the most, whose strike rate is highest, who hits the most fours and sixes, whose
best single knock is the biggest, and who bats with the best average. The
bowling boards ask five of a bowler: most wickets, best economy, best spell,
most dot balls and most maidens.

All figures come from the same score engine every scorecard is built with, on
the same "only completed matches count" rule the rest of the app's aggregates
use: a live match has no settled figures and its totals would move under the
reader. A player is credited to the team of his most recent innings; Super Over
innings are excluded (they are one-over sides that would wreck an "average
sample"), and a batsman who never faced a ball - and was never dismissed - does
not enter the totals at all.

Three disciplines straight from this module's only job - ranking - need a guard
against tiny samples. A bat who nudged a two-ball cameo would top the
strike-rate board on a sample too small to mean anything, so ``min_balls_faced``
keeps him off it; an average anchored on one lucky dismissal is equally
unrankable, so ``min_dismissals`` protects the average board; and a bowler with
one tight over would top the economy board, so ``min_overs`` keeps him off it.
All three are query parameters with sensible defaults, and none is a hard rule
about what counts, just a floor for what a ranking may be built from.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import MatchStatus
from app.matches.models import Match
from app.players.identity import full_name
from app.players.models import Player
from app.scoring.crud import ScoringRepository
from app.scoring.engine import BatsmanCard, BowlerCard, ScoreEngine, ScoreEngineError
from app.scoring.enums import ExtraType, WicketType
from app.scoring.models import Delivery, Innings
from app.teams.models import Team

# Only these dismissals can be credited to a fielder. bowled, lbw and hit wicket
# are the bowler's alone; a run out can be effected without a throw reaching
# anyone, but when a fielder is named he is the one credited.
_FIELDING_WICKETS = {
    WicketType.CAUGHT.value: "catches",
    WicketType.RUN_OUT.value: "run_outs",
    WicketType.STUMPED.value: "stumpings",
}

# Wides and no-balls are penalty deliveries: they concede at least one run to the
# side and never count as a ball, so neither can be a dot ball.
_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE.value, ExtraType.NO_BALL.value)


def _new_accumulator(b: BatsmanCard) -> dict:
    return {
        "first_name": b.first_name,
        "last_name": b.last_name,
        "player_code": b.player_code,
        "matches": set(),
        "innings": 0,
        "not_outs": 0,
        "dismissals": 0,
        "runs": 0,
        "balls_faced": 0,
        "fours": 0,
        "sixes": 0,
        "highest_score": 0,
        "highest_score_not_out": False,
        "last_match_id": -1,
        "team_id": None,
    }


async def _completed_match_ids(db: AsyncSession, user_id: int) -> list[int]:
    result = await db.execute(
        select(Match.id)
        .where(Match.user_id == user_id, Match.status == MatchStatus.COMPLETED.value)
        .order_by(Match.id)
    )
    return list(result.scalars().all())


async def _match_innings(db: AsyncSession, match_id: int) -> list[Innings]:
    result = await db.execute(
        select(Innings)
        .where(Innings.match_id == match_id)
        .order_by(Innings.innings_number, Innings.id)
    )
    return list(result.scalars().all())


async def _scorecards(
    engine: ScoreEngine, innings_list: list[Innings]
) -> dict[int, object]:
    cards: dict[int, object] = {}
    for innings in innings_list:
        try:
            cards[innings.id] = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            cards[innings.id] = None
    return cards


def _player_name(first: str | None, last: str | None, player_id: int) -> str:
    return full_name(first, last) or f"Player {player_id}"


def _slice(
    rows: list[dict],
    top: int,
    sort_key,
    metric,
    *,
    qualified=None,
) -> list[dict]:
    """The ranked slice of ``rows`` for one board.

    ``qualified`` (when given) is the eligibility filter applied before sorting -
    the strike rate and average floors. Each returned row carries its weighted
    ``position`` and ``value`` - the metric this particular board ranked by - so a
    client can render "1st, 10 pts" without re-deriving the rule from the sorted
    list. The full set of stats travels with every entry, so one list answers the
    board question and the "who are these people" question at once.
    """
    pool = rows if qualified is None else [row for row in rows if qualified(row)]
    ordered = sorted(pool, key=sort_key)
    return [
        {**row, "position": position, "value": float(metric(row))}
        for position, row in enumerate(ordered[:top], start=1)
    ]


async def build_batting_leaderboards(
    db: AsyncSession,
    user_id: int,
    *,
    top: int = 10,
    min_balls_faced: int = 30,
    min_dismissals: int = 3,
) -> dict:
    """Build the six batting boards across an account's completed matches."""
    match_ids = await _completed_match_ids(db, user_id)
    engine = ScoreEngine(ScoringRepository(db))

    stats: dict[int, dict] = {}
    # Batch load innings
    if match_ids:
        innings_query = await db.execute(
            select(Innings.id, Innings.match_id, Innings.is_super_over, Innings.batting_team_id)
            .where(Innings.match_id.in_(match_ids))
            .order_by(Innings.id)
        )
        all_innings = innings_query.all()
    else:
        all_innings = []

    for innings_data in all_innings:
        innings_id, match_id, is_super_over, batting_team_id = innings_data
        if is_super_over:
            continue
        card = await engine.get_scorecard(innings_id)
        if card is None:
            continue
        for b in card.batsmen:
            if b.did_not_bat or (b.balls_faced == 0 and not b.out):
                continue
            acc = stats.setdefault(b.player_id, _new_accumulator(b))
            acc["matches"].add(match_id)
            acc["innings"] += 1
            acc["runs"] += b.runs
            acc["balls_faced"] += b.balls_faced
            acc["fours"] += b.fours
            acc["sixes"] += b.sixes
            if b.out:
                acc["dismissals"] += 1
            else:
                acc["not_outs"] += 1
            if b.runs > acc["highest_score"]:
                acc["highest_score"] = b.runs
                acc["highest_score_not_out"] = not b.out
            if match_id > acc["last_match_id"]:
                acc["last_match_id"] = match_id
                acc["team_id"] = batting_team_id

    rows: list[dict] = []
    for player_id, acc in stats.items():
        average = round(acc["runs"] / acc["dismissals"], 2) if acc["dismissals"] else None
        strike_rate = (
            round(acc["runs"] * 100 / acc["balls_faced"], 2) if acc["balls_faced"] else 0.0
        )
        rows.append(
            {
                "player_id": player_id,
                "player_code": acc["player_code"],
                "first_name": acc["first_name"],
                "last_name": acc["last_name"],
                "full_name": _player_name(acc["first_name"], acc["last_name"], player_id),
                "team_id": acc["team_id"],
                "team_name": None,
                "team_short_name": None,
                "matches": len(acc["matches"]),
                "innings": acc["innings"],
                "not_outs": acc["not_outs"],
                "dismissals": acc["dismissals"],
                "runs": acc["runs"],
                "balls_faced": acc["balls_faced"],
                "fours": acc["fours"],
                "sixes": acc["sixes"],
                "highest_score": acc["highest_score"],
                "highest_score_not_out": acc["highest_score_not_out"],
                "highest_score_display": (
                    f"{acc['highest_score']}{'*' if acc['highest_score_not_out'] else ''}"
                ),
                "average": average,
                "strike_rate": strike_rate,
            }
        )

    team_ids = {row["team_id"] for row in rows if row["team_id"] is not None}
    if team_ids:
        result = await db.execute(
            select(Team.id, Team.name, Team.short_name).where(Team.id.in_(team_ids))
        )
        names = {tid: (name, short) for tid, name, short in result.all()}
        for row in rows:
            if row["team_id"] is not None and row["team_id"] in names:
                row["team_name"], row["team_short_name"] = names[row["team_id"]]

    return {
        "scope": "account",
        "matches_considered": len(match_ids),
        "min_balls_faced": min_balls_faced,
        "min_dismissals": min_dismissals,
        "most_runs": _slice(
            rows,
            top,
            lambda r: (-r["runs"], -r["highest_score"], r["balls_faced"], r["player_id"]),
            lambda r: r["runs"],
        ),
        "highest_strike_rate": _slice(
            rows,
            top,
            lambda r: (-r["strike_rate"], -r["runs"], r["balls_faced"], r["player_id"]),
            lambda r: r["strike_rate"],
            qualified=lambda r: r["balls_faced"] >= min_balls_faced,
        ),
        "most_fours": _slice(
            rows,
            top,
            lambda r: (-r["fours"], -r["runs"], r["balls_faced"], r["player_id"]),
            lambda r: r["fours"],
        ),
        "most_sixes": _slice(
            rows,
            top,
            lambda r: (-r["sixes"], -r["runs"], r["balls_faced"], r["player_id"]),
            lambda r: r["sixes"],
        ),
        "highest_score": _slice(
            rows,
            top,
            lambda r: (-r["highest_score"], r["balls_faced"], r["player_id"]),
            lambda r: r["highest_score"],
        ),
        "best_batting_average": _slice(
            rows,
            top,
            lambda r: (-r["average"], -r["runs"], r["balls_faced"], r["player_id"]),
            lambda r: r["average"],
            qualified=lambda r: r["dismissals"] >= min_dismissals and r["average"] is not None,
        ),
    }


def _new_bowler_accumulator(c: BowlerCard) -> dict:
    return {
        "first_name": c.first_name,
        "last_name": c.last_name,
        "player_code": c.player_code,
        "matches": set(),
        "innings": 0,
        "balls_bowled": 0,
        "runs_conceded": 0,
        "maidens": 0,
        "wickets": 0,
        "wides": 0,
        "no_balls": 0,
        "dot_balls": 0,
        # The best spell is "wickets/runs", with the cheaper over breaking a tie,
        # so it keeps its own key distinct from a board's sort key.
        "best_key": (-1, 0, 0),
        "best_figures_wickets": 0,
        "best_figures_runs": 0,
        "best_figures_balls": 0,
        "last_match_id": -1,
        "team_id": None,
    }


async def _innings_dot_balls(db: AsyncSession, innings_id: int, card: object) -> dict[int, int]:
    """Legal, run-less deliveries per fielding-side bowler of one innings.

    A dot ball is a delivery the batting side scores nothing from, off bat or
    extras, that still counts as a ball - so wides and no-balls are never dots.
    Dot balls are not on the engine's bowler card, so they are folded in here
    from the raw deliveries. Only bowlers already on the card are counted: a
    delivery recorded against a batting-side player is corrupt data, ignored the
    same way the engine ignores it.
    """
    allowed = {b.player_id for b in card.bowlers}
    result = await db.execute(
        select(
            Delivery.bowler_id,
            Delivery.runs_batsman,
            Delivery.runs_extras,
            Delivery.extra_type,
        ).where(Delivery.innings_id == innings_id)
    )
    dots: dict[int, int] = {}
    for bowler_id, runs_batsman, runs_extras, extra_type in result.all():
        if bowler_id not in allowed or extra_type in _ILLEGAL_BALL_EXTRAS:
            continue
        if runs_batsman + runs_extras == 0:
            dots[bowler_id] = dots.get(bowler_id, 0) + 1
    return dots


async def build_bowling_leaderboards(
    db: AsyncSession,
    user_id: int,
    *,
    top: int = 10,
    min_overs: float = 3.0,
) -> dict:
    """Build the five bowling boards across an account's completed matches."""
    match_ids = await _completed_match_ids(db, user_id)
    engine = ScoreEngine(ScoringRepository(db))

    stats: dict[int, dict] = {}
    # Batch load all data for efficiency
    if match_ids:
        innings_query = await db.execute(
            select(Innings.id, Innings.match_id, Innings.is_super_over, Innings.bowling_team_id)
            .where(Innings.match_id.in_(match_ids))
            .order_by(Innings.id)
        )
        all_innings = innings_query.all()
        all_innings_ids = [i[0] for i in all_innings]
        # Get all scorecards in one go by loading deliveries and computing? But scorecards use engine
        # For now, still call engine per innings but we'll batch dots
    else:
        all_innings = []
        all_innings_ids = []

    # Batch load all deliveries for dot ball calculation
    dots_by_innings_bowler = {}
    if all_innings_ids:
        deliv_query = await db.execute(
            select(Delivery.innings_id, Delivery.bowler_id, Delivery.runs_batsman,
                   Delivery.runs_extras, Delivery.extra_type)
            .where(Delivery.innings_id.in_(all_innings_ids))
        )
        for inn_id, bowler_id, runs_b, runs_e, ext in deliv_query.all():
            key = (inn_id, bowler_id)
            if ext in _ILLEGAL_BALL_EXTRAS:
                continue
            if runs_b + runs_e == 0:
                dots_by_innings_bowler[key] = dots_by_innings_bowler.get(key, 0) + 1

    # Build innings objects map if needed - but easier to work with IDs
    for innings_data in all_innings:
        innings_id, match_id, is_super_over, bowling_team_id = innings_data
        if is_super_over:
            continue
        card = await engine.get_scorecard(innings_id)
        if not card or not hasattr(card, 'bowlers'):
            continue
        for c in card.bowlers:
            acc = stats.setdefault(c.player_id, _new_bowler_accumulator(c))
            acc["matches"].add(match_id)
            acc["innings"] += 1
            acc["balls_bowled"] += c.balls_bowled
            acc["runs_conceded"] += c.runs_conceded
            acc["maidens"] += c.maidens
            acc["wickets"] += c.wickets
            acc["wides"] += c.wides
            acc["no_balls"] += c.no_balls
            acc["dot_balls"] += dots_by_innings_bowler.get((innings_id, c.player_id), 0)
            if match_id > acc["last_match_id"]:
                acc["last_match_id"] = match_id
                acc["team_id"] = bowling_team_id
            best_key = (c.wickets, -c.runs_conceded, -c.balls_bowled)
            if best_key > acc["best_key"]:
                acc["best_key"] = best_key
                acc["best_figures_wickets"] = c.wickets
                acc["best_figures_runs"] = c.runs_conceded
                acc["best_figures_balls"] = c.balls_bowled

    rows: list[dict] = []
    for player_id, acc in stats.items():
        full, rem = divmod(acc["balls_bowled"], 6)
        rows.append(
            {
                "player_id": player_id,
                "player_code": acc["player_code"],
                "first_name": acc["first_name"],
                "last_name": acc["last_name"],
                "full_name": _player_name(acc["first_name"], acc["last_name"], player_id),
                "team_id": acc["team_id"],
                "team_name": None,
                "team_short_name": None,
                "matches": len(acc["matches"]),
                "innings": acc["innings"],
                "balls_bowled": acc["balls_bowled"],
                "overs": round(full + rem / 6, 2),
                "overs_str": f"{full}.{rem}",
                "maidens": acc["maidens"],
                "runs_conceded": acc["runs_conceded"],
                "wickets": acc["wickets"],
                "economy": round(acc["runs_conceded"] / (acc["balls_bowled"] / 6), 2)
                if acc["balls_bowled"]
                else 0.0,
                "wides": acc["wides"],
                "no_balls": acc["no_balls"],
                "dot_balls": acc["dot_balls"],
                "best_figures_wickets": acc["best_figures_wickets"],
                "best_figures_runs": acc["best_figures_runs"],
                "best_figures_balls": acc["best_figures_balls"],
                "best_figures_display": (
                    f"{acc['best_figures_wickets']}/{acc['best_figures_runs']}"
                ),
            }
        )

    team_ids = {row["team_id"] for row in rows if row["team_id"] is not None}
    if team_ids:
        result = await db.execute(
            select(Team.id, Team.name, Team.short_name).where(Team.id.in_(team_ids))
        )
        names = {tid: (name, short) for tid, name, short in result.all()}
        for row in rows:
            if row["team_id"] is not None and row["team_id"] in names:
                row["team_name"], row["team_short_name"] = names[row["team_id"]]

    min_balls = min_overs * 6
    return {
        "scope": "account",
        "matches_considered": len(match_ids),
        "min_overs": min_overs,
        "most_wickets": _slice(
            rows,
            top,
            lambda r: (-r["wickets"], r["runs_conceded"], r["balls_bowled"], r["player_id"]),
            lambda r: r["wickets"],
        ),
        "best_economy": _slice(
            rows,
            top,
            lambda r: (r["economy"], -r["wickets"], r["balls_bowled"], r["player_id"]),
            lambda r: r["economy"],
            qualified=lambda r: r["balls_bowled"] >= min_balls,
        ),
        "best_bowling_figures": _slice(
            rows,
            top,
            lambda r: (
                -r["best_figures_wickets"],
                r["best_figures_runs"],
                r["best_figures_balls"],
                r["player_id"],
            ),
            lambda r: r["best_figures_wickets"],
        ),
        "most_dot_balls": _slice(
            rows,
            top,
            lambda r: (-r["dot_balls"], -r["wickets"], r["runs_conceded"], r["player_id"]),
            lambda r: r["dot_balls"],
        ),
        "most_maidens": _slice(
            rows,
            top,
            lambda r: (-r["maidens"], -r["wickets"], r["runs_conceded"], r["player_id"]),
            lambda r: r["maidens"],
        ),
    }


def _new_fielding_accumulator(first, last, code):
    return {
        "first_name": first,
        "last_name": last,
        "player_code": code,
        "matches": set(),
        "innings": 0,
        "catches": 0,
        "run_outs": 0,
        "stumpings": 0,
        "last_match_id": -1,
        "team_id": None,
    }


async def build_fielding_leaderboards(db: AsyncSession, user_id: int, *, top: int = 10) -> dict:
    """Build the three fielding boards across an account's completed matches.

    A dismissal credits a fielder only when the delivery carries one: the column
    is optional and older rows have none. The winning ball of a chase is skipped
    exactly as the engine skips its wicket, so these counts can never exceed the
    scorecards a user has already seen. is_super_over innings are excluded the
    same way the batting and bowling boards exclude them.
    """
    match_ids = await _completed_match_ids(db, user_id)

    stats: dict[int, dict] = {}
    # Preload all deliveries for completed matches to avoid N+1 queries
    if match_ids:
        # Get all innings IDs for completed matches
        innings_query = await db.execute(
            select(Innings.id, Innings.match_id, Innings.is_super_over, Innings.target, Innings.bowling_team_id)
            .where(Innings.match_id.in_(match_ids))
            .order_by(Innings.id)
        )
        all_innings = innings_query.all()
        all_innings_ids = [i[0] for i in all_innings]
        # Batch load all deliveries
        if all_innings_ids:
            deliv_query = await db.execute(
                select(Delivery.id, Delivery.innings_id, Delivery.wicket_type,
                       Delivery.fielder_id, Delivery.runs_batsman, Delivery.runs_extras)
                .where(Delivery.innings_id.in_(all_innings_ids))
                .order_by(Delivery.innings_id, Delivery.id)
            )
            all_deliveries = deliv_query.all()
            # Group deliveries by innings
            deliveries_by_innings = {}
            for d in all_deliveries:
                deliveries_by_innings.setdefault(d[1], []).append(d)
        else:
            deliveries_by_innings = {}
    else:
        all_innings = []
        deliveries_by_innings = {}

    for innings_data in all_innings:
        innings_id, match_id, is_super_over, target, bowling_team_id = innings_data
        if is_super_over:
            continue
        deliveries = deliveries_by_innings.get(innings_id, [])

        # The delivery that first reaches the chase target ends the innings;
        # a wicket on it is never registered, so it must not credit a fielder.
        winning_id = None
        if target is not None:
            running = 0
            for d in deliveries:
                running += d[4] + d[5]  # runs_batsman + runs_extras
                if running >= target:
                    winning_id = d[0]  # delivery id
                    break

        for d in deliveries:
            d_id, _inn_id, wicket_type, fielder_id, _rb, _re = d
            if fielder_id is None or wicket_type not in _FIELDING_WICKETS:
                continue
            if d_id == winning_id:
                continue
            bucket = _FIELDING_WICKETS[wicket_type]
            acc = stats.get(fielder_id)
            if acc is None:
                acc = _new_fielding_accumulator(None, None, None)
                stats[fielder_id] = acc
            acc[bucket] += 1
            acc["matches"].add(match_id)
            acc["innings"] += 1
            if match_id > acc["last_match_id"]:
                acc["last_match_id"] = match_id
                acc["team_id"] = bowling_team_id

    fielder_ids = list(stats)
    if fielder_ids:
        identities = await db.execute(
            select(Player.id, Player.first_name, Player.last_name, Player.player_code).where(
                Player.id.in_(fielder_ids)
            )
        )
        for pid, first, last, code in identities.all():
            acc = stats.get(pid)
            if acc is not None:
                acc["first_name"], acc["last_name"], acc["player_code"] = first, last, code

    rows: list[dict] = []
    for player_id, acc in stats.items():
        rows.append(
            {
                "player_id": player_id,
                "player_code": acc["player_code"],
                "first_name": acc["first_name"],
                "last_name": acc["last_name"],
                "full_name": _player_name(acc["first_name"], acc["last_name"], player_id),
                "team_id": acc["team_id"],
                "team_name": None,
                "team_short_name": None,
                "matches": len(acc["matches"]),
                "innings": acc["innings"],
                "catches": acc["catches"],
                "run_outs": acc["run_outs"],
                "stumpings": acc["stumpings"],
                "total_dismissals": acc["catches"] + acc["run_outs"] + acc["stumpings"],
            }
        )

    team_ids = {row["team_id"] for row in rows if row["team_id"] is not None}
    if team_ids:
        result = await db.execute(
            select(Team.id, Team.name, Team.short_name).where(Team.id.in_(team_ids))
        )
        names = {tid: (name, short) for tid, name, short in result.all()}
        for row in rows:
            if row["team_id"] is not None and row["team_id"] in names:
                row["team_name"], row["team_short_name"] = names[row["team_id"]]

    return {
        "scope": "account",
        "matches_considered": len(match_ids),
        "most_catches": _slice(
            rows,
            top,
            lambda r: (-r["catches"], -r["total_dismissals"], r["player_id"]),
            lambda r: r["catches"],
            qualified=lambda r: r["catches"] > 0,
        ),
        "most_run_outs": _slice(
            rows,
            top,
            lambda r: (-r["run_outs"], -r["total_dismissals"], r["player_id"]),
            lambda r: r["run_outs"],
            qualified=lambda r: r["run_outs"] > 0,
        ),
        "most_stumpings": _slice(
            rows,
            top,
            lambda r: (-r["stumpings"], -r["total_dismissals"], r["player_id"]),
            lambda r: r["stumpings"],
            qualified=lambda r: r["stumpings"] > 0,
        ),
    }


__all__ = [
    "build_batting_leaderboards",
    "build_bowling_leaderboards",
    "build_fielding_leaderboards",
]
