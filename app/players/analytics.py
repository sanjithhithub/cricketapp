"""Career performance analytics for a single player.

Aggregates a player's batting, bowling and fielding output across the innings
they have appeared in, reading the figures straight from the score engine so the
numbers always agree with the scorecards a user has already seen.

Participation is scoped to the requesting account: only innings belonging to
matches owned by ``user_id`` are counted, exactly like the rest of the player
API.

Fielding figures (catches, run-outs and stumpings) are counted from the
``fielder_id`` a dismissal may carry. Attribution is optional - deliveries
scored before the column existed, and dismissals that belong to nobody (a
bowled, an unattributed run out), carry none - so a fielder is credited only
where the log names them. The delivery that first reaches a chase target never
registers its wicket, so it cannot credit a fielder either, mirroring the engine.

The per-innings accumulation lives in :class:`PlayerStatsAccumulator` so the
career-performance endpoint and the profile endpoint derive identical figures
from the same code; only the set of innings fed in differs.
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.enums import ExtraType, MatchStatus, WicketType
from app.scoring.models import Delivery, Innings, InningsBatsman

_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE.value, ExtraType.NO_BALL.value)

# Only these dismissals can be credited to a fielder; bowled, lbw and hit wicket
# are the bowler's alone.
_FIELDING_WICKETS = {
    WicketType.CAUGHT.value: "catches",
    WicketType.RUN_OUT.value: "run_outs",
    WicketType.STUMPED.value: "stumpings",
}

FIELDING_NOTE = (
    "Fielding is counted only where the delivery log attributes it: a catch, "
    "run out or stumping credited to this player. Deliveries scored before "
    "fielder attribution existed contribute nothing."
)


def _extra(delivery) -> str:
    value = delivery.extra_type
    return value.value if isinstance(value, ExtraType) else (value or ExtraType.NONE.value)


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 2) if denominator else None


def _empty_batting() -> dict:
    return {
        "innings": 0,
        "not_outs": 0,
        "dismissals": 0,
        "runs": 0,
        "balls_faced": 0,
        "fours": 0,
        "sixes": 0,
        "dot_balls": 0,
        "highest_score": 0,
        "highest_score_not_out": False,
        "fifties": 0,
        "hundreds": 0,
    }


def _empty_bowling() -> dict:
    return {
        "innings": 0,
        "balls_bowled": 0,
        "runs_conceded": 0,
        "wickets": 0,
        "maidens": 0,
        "dot_balls": 0,
        "wides": 0,
        "no_balls": 0,
        "best_wickets": None,
        "best_runs_conceded": None,
        "best_balls": None,
    }


def _empty_fielding() -> dict:
    return {"catches": 0, "run_outs": 0, "stumpings": 0}


def _winning_delivery_id(innings: Innings, deliveries) -> int | None:
    """The delivery that first reaches the chase target, if any.

    Mirrors the engine: the run chase ends the instant the target is reached and
    a wicket on that same delivery is never registered, so it must not credit a
    fielder either.
    """
    if innings.target is None:
        return None
    running = 0
    for d in deliveries:
        running += d.runs_batsman + d.runs_extras
        if running >= innings.target:
            return d.id
    return None


def fielding_counts(innings: Innings, deliveries, player_id: int) -> dict:
    """Catches, run-outs and stumpings this player is credited with in one innings.

    A delivery credits a fielder only when it names one and carries a
    fielding dismissal; the winning ball of a chase is skipped, exactly as the
    engine skips its wicket. Exposed so a caller reporting one innings at a time
    can reuse the same rule as the accumulator.
    """
    winning_id = _winning_delivery_id(innings, deliveries)
    counts = _empty_fielding()
    for d in deliveries:
        if d.id == winning_id or d.fielder_id != player_id or not d.wicket_type:
            continue
        bucket = _FIELDING_WICKETS.get(d.wicket_type)
        if bucket is not None:
            counts[bucket] += 1
    return counts


def fielding_block(counts: dict) -> dict:
    """The response body for a fielding tally."""
    return {
        "available": True,
        "catches": counts["catches"],
        "run_outs": counts["run_outs"],
        "stumpings": counts["stumpings"],
        "total_dismissals": counts["catches"] + counts["run_outs"] + counts["stumpings"],
        "note": FIELDING_NOTE,
    }


class PlayerStatsAccumulator:
    """Running totals for one player, fed one innings at a time.

    The caller decides *which* innings to add - all of them for a career, only
    those from completed matches for a profile, and once per competition there -
    so the same rules always produce the same figures however they are grouped.
    """

    def __init__(self) -> None:
        self.matches: set[int] = set()
        self.batting = _empty_batting()
        self.bowling = _empty_bowling()
        self.fielding = _empty_fielding()

    def add_innings(self, innings: Innings, card, player_id: int, deliveries) -> None:
        self.matches.add(innings.match_id)

        bat = next((b for b in card.batsmen if b.player_id == player_id), None)
        if bat is not None and not bat.did_not_bat:
            batting = self.batting
            batting["innings"] += 1
            batting["runs"] += bat.runs
            batting["balls_faced"] += bat.balls_faced
            batting["fours"] += bat.fours
            batting["sixes"] += bat.sixes
            if bat.out:
                batting["dismissals"] += 1
            else:
                batting["not_outs"] += 1
            if bat.runs >= 100:
                batting["hundreds"] += 1
            elif bat.runs >= 50:
                batting["fifties"] += 1
            if bat.runs > batting["highest_score"]:
                batting["highest_score"] = bat.runs
                batting["highest_score_not_out"] = not bat.out

        bowl = next((b for b in card.bowlers if b.player_id == player_id), None)
        if bowl is not None:
            bowling = self.bowling
            bowling["innings"] += 1
            bowling["balls_bowled"] += bowl.balls_bowled
            bowling["runs_conceded"] += bowl.runs_conceded
            bowling["wickets"] += bowl.wickets
            bowling["maidens"] += bowl.maidens
            bowling["wides"] += bowl.wides
            bowling["no_balls"] += bowl.no_balls
            key = (bowl.wickets, -bowl.runs_conceded, -bowl.balls_bowled)
            best_key = (
                (
                    bowling["best_wickets"],
                    -bowling["best_runs_conceded"],
                    -bowling["best_balls"],
                )
                if bowling["best_wickets"] is not None
                else None
            )
            if best_key is None or key > best_key:
                bowling["best_wickets"] = bowl.wickets
                bowling["best_runs_conceded"] = bowl.runs_conceded
                bowling["best_balls"] = bowl.balls_bowled

        # Dot balls need the raw deliveries: a dot is a legal ball with no runs
        # scored off it at all (byes and leg byes count as runs, so they are not
        # dots).
        for d in deliveries:
            if _extra(d) in _ILLEGAL_BALL_EXTRAS:
                continue
            if d.runs_batsman + d.runs_extras != 0:
                continue
            if d.striker_id == player_id:
                self.batting["dot_balls"] += 1
            if d.bowler_id == player_id:
                self.bowling["dot_balls"] += 1

        counts = fielding_counts(innings, deliveries, player_id)
        for bucket in self.fielding:
            self.fielding[bucket] += counts[bucket]

    def batting_payload(self) -> dict:
        batting = self.batting
        return {
            "innings": batting["innings"],
            "not_outs": batting["not_outs"],
            "dismissals": batting["dismissals"],
            "runs": batting["runs"],
            "balls_faced": batting["balls_faced"],
            "average": _ratio(batting["runs"], batting["dismissals"]),
            "strike_rate": round(_ratio(batting["runs"] * 100, batting["balls_faced"]) or 0.0, 2),
            "highest_score": batting["highest_score"],
            "highest_score_not_out": batting["highest_score_not_out"],
            "highest_score_display": (
                f"{batting['highest_score']}{'*' if batting['highest_score_not_out'] else ''}"
            ),
            "fours": batting["fours"],
            "sixes": batting["sixes"],
            "dot_balls": batting["dot_balls"],
            "fifties": batting["fifties"],
            "hundreds": batting["hundreds"],
        }

    def bowling_payload(self) -> dict:
        bowling = self.bowling
        best_display = (
            f"{bowling['best_wickets']}/{bowling['best_runs_conceded']}"
            if bowling["best_wickets"] is not None
            else None
        )
        return {
            "innings": bowling["innings"],
            "balls_bowled": bowling["balls_bowled"],
            "overs": round(bowling["balls_bowled"] / 6, 2),
            "overs_str": f"{bowling['balls_bowled'] // 6}.{bowling['balls_bowled'] % 6}",
            "runs_conceded": bowling["runs_conceded"],
            "wickets": bowling["wickets"],
            "economy": round(
                _ratio(bowling["runs_conceded"], bowling["balls_bowled"] / 6) or 0.0, 2
            ),
            "average": _ratio(bowling["runs_conceded"], bowling["wickets"]),
            "strike_rate": _ratio(bowling["balls_bowled"], bowling["wickets"]),
            "maidens": bowling["maidens"],
            "dot_balls": bowling["dot_balls"],
            "wides": bowling["wides"],
            "no_balls": bowling["no_balls"],
            "best_wickets": bowling["best_wickets"],
            "best_runs_conceded": bowling["best_runs_conceded"],
            "best_display": best_display,
        }

    def fielding_payload(self) -> dict:
        return fielding_block(self.fielding)

    def stats_payload(self) -> dict:
        return {
            "matches_played": len(self.matches),
            "innings_played": self.batting["innings"],
            "batting": self.batting_payload(),
            "bowling": self.bowling_payload(),
            "fielding": self.fielding_payload(),
        }


def _participation_innings(player_id: int):
    """Innings the player appeared in: in the XI, or involved in a delivery.

    Someone named in the batting order has played even if they never batted
    (``did_not_bat``); a substitute who only bowled shows up through the
    delivery log. The union of both is the honest set of appearances.
    """
    in_order = select(InningsBatsman.innings_id).where(InningsBatsman.player_id == player_id)
    in_delivery = select(Delivery.innings_id).where(
        or_(
            Delivery.striker_id == player_id,
            Delivery.non_striker_id == player_id,
            Delivery.bowler_id == player_id,
        )
    )
    return in_order.union(in_delivery)


async def load_player_innings(
    db: AsyncSession,
    player_id: int,
    user_id: int,
    *,
    completed_only: bool = False,
) -> list[Innings]:
    """Every innings the player appeared in, oldest match first.

    ``completed_only`` restricts the result to matches whose status is
    ``completed`` - the profile endpoint counts only finished cricket, while the
    career-performance endpoint reports everything the player has played.
    """
    stmt = (
        select(Innings)
        .join(Match, Match.id == Innings.match_id)
        .where(
            Match.user_id == user_id,
            Innings.id.in_(_participation_innings(player_id)),
        )
    )
    if completed_only:
        stmt = stmt.where(Match.status == MatchStatus.COMPLETED.value)
    stmt = stmt.order_by(Match.id, Innings.innings_number, Innings.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_player_performance(db: AsyncSession, player, user_id: int) -> dict:
    """Build the career performance payload for ``player``."""
    innings_list = await load_player_innings(db, player.id, user_id)

    repo = ScoringRepository(db)
    engine = ScoreEngine(repo)
    stats = PlayerStatsAccumulator()

    for innings in innings_list:
        try:
            card = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            continue
        stats.add_innings(innings, card, player.id, await repo.list_deliveries(innings.id))

    return {
        "player_id": player.id,
        "player_code": player.player_code,
        "first_name": player.first_name,
        "last_name": player.last_name,
        "full_name": full_name(player.first_name, player.last_name),
        **stats.stats_payload(),
    }


__all__ = [
    "FIELDING_NOTE",
    "PlayerStatsAccumulator",
    "fielding_block",
    "fielding_counts",
    "get_player_performance",
    "load_player_innings",
]
