"""Visualization analytics: chart-ready slices of matches and teams.

One builder per chart type, so a route is a thin wrapper and every chart follows
the same rules the scorecard already uses:

- innings totals, over-by-over runs and run-rate progression come from the same
  ball-by-ball module the analytics endpoint uses (``build_innings_analytics``),
  so the charts always agree with the delivered log;
- player batting and bowling figures come from the score engine's scorecard, so
  they agree with the scorecard a user has already seen;
- partnerships replay the delivery log between wickets and skip the winning
  ball of a chase exactly as the engine skips its wicket;
- team results come from ``derive_result``, the same structural derivation the
  match summary uses.

Super Over innings are included in the match charts (they are real cricket and
numbered separately so they never corrupt the normal innings); team-form charts
use the completed-match win/loss rules everywhere else uses them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.identity import full_name
from app.scoring.analytics import _max_overs_for, build_innings_analytics
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.enums import MatchStatus
from app.scoring.models import Innings
from app.scoring.partnerships import compute_partnerships
from app.scoring.summary import derive_result
from app.teams.models import Team

_RESULT_CODES = {"win": "W", "loss": "L", "draw": "D"}


def _player_name(first: str | None, last: str | None, player_id: int) -> str:
    return full_name(first, last) or f"Player {player_id}"


def _run_rate(runs: int, legal_balls: int) -> float:
    return round(runs / (legal_balls / 6), 2) if legal_balls else 0.0


@dataclass
class _InningsData:
    innings: Innings
    deliveries: list
    card: object | None
    identities: dict[int, tuple]


async def _list_innings(
    db: AsyncSession, match_id: int, innings_number: int | None = None
) -> list[Innings]:
    stmt = select(Innings).where(Innings.match_id == match_id)
    if innings_number is not None:
        stmt = stmt.where(Innings.innings_number == innings_number)
    result = await db.execute(stmt.order_by(Innings.innings_number, Innings.id))
    return list(result.scalars().all())


async def _load_innings(
    db: AsyncSession,
    match: Match,
    innings_number: int | None = None,
    *,
    with_cards: bool = True,
) -> list[_InningsData]:
    engine = ScoreEngine(ScoringRepository(db))
    repo = ScoringRepository(db)
    innings_list = await _list_innings(db, match.id, innings_number)
    # Batch load deliveries for all innings
    if innings_list:
        from app.scoring.models import Delivery

        deliv_query = await db.execute(
            select(Delivery)
            .where(Delivery.innings_id.in_([i.id for i in innings_list]))
            .order_by(Delivery.innings_id, Delivery.id)
        )
        all_deliveries = deliv_query.scalars().all()
        deliveries_by_innings = {}
        for d in all_deliveries:
            deliveries_by_innings.setdefault(d.innings_id, []).append(d)
    else:
        deliveries_by_innings = {}
    entries: list[_InningsData] = []
    # Collect all players for batch identity lookup
    all_players: set[int] = set()
    for innings in innings_list:
        deliveries = deliveries_by_innings.get(innings.id, [])
        for d in deliveries:
            all_players.update(
                (d.striker_id, d.non_striker_id, d.bowler_id, d.dismissed_player_id, d.fielder_id)
            )
    identities_all = await repo.player_identities(all_players)
    # Build entries
    for innings in innings_list:
        deliveries = deliveries_by_innings.get(innings.id, [])
        identities = identities_all  # Use cached - could filter, but fine for small sets
        card = None
        if with_cards:
            try:
                card = await engine.get_scorecard(innings.id)
            except ScoreEngineError:
                card = None
        entries.append(_InningsData(innings, deliveries, card, identities))
    return entries


def _innings_frame(entry: _InningsData, match: Match) -> dict:
    return {
        "innings_id": entry.innings.id,
        "innings_number": entry.innings.innings_number,
        "batting_team_id": entry.innings.batting_team_id,
        "bowling_team_id": entry.innings.bowling_team_id,
        "is_super_over": entry.innings.is_super_over,
        "target": entry.innings.target,
        "total": None,
        "wickets": None,
        "legal_balls": None,
        "overs_bowled_str": None,
    }


def _aggregate_batting(entries: list[_InningsData]) -> list[dict]:
    totals: dict[int, dict] = {}
    for entry in entries:
        card = entry.card
        if card is None:
            continue
        for b in card.batsmen:
            if b.did_not_bat:
                continue
            acc = totals.setdefault(
                b.player_id,
                {
                    "player_id": b.player_id,
                    "first_name": b.first_name,
                    "last_name": b.last_name,
                    "player_code": b.player_code,
                    "team_id": entry.innings.batting_team_id,
                    "innings": 0,
                    "runs": 0,
                    "balls_faced": 0,
                    "fours": 0,
                    "sixes": 0,
                    "highest_score": 0,
                    "highest_score_not_out": False,
                },
            )
            acc["innings"] += 1
            acc["runs"] += b.runs
            acc["balls_faced"] += b.balls_faced
            acc["fours"] += b.fours
            acc["sixes"] += b.sixes
            if b.runs > acc["highest_score"] or (
                b.runs == acc["highest_score"]
                and not b.out
                and acc["highest_score_not_out"] is False
            ):
                acc["highest_score"] = b.runs
                acc["highest_score_not_out"] = not b.out
    rows = []
    for player_id, acc in totals.items():
        rows.append(
            {
                **acc,
                "full_name": _player_name(acc["first_name"], acc["last_name"], player_id),
                "highest_score_display": (
                    f"{acc['highest_score']}{'*' if acc['highest_score_not_out'] else ''}"
                ),
                "strike_rate": round(acc["runs"] * 100 / acc["balls_faced"], 2)
                if acc["balls_faced"]
                else 0.0,
            }
        )
    return rows


def _aggregate_bowling(entries: list[_InningsData]) -> list[dict]:
    totals: dict[int, dict] = {}
    for entry in entries:
        card = entry.card
        if card is None:
            continue
        for b in card.bowlers:
            acc = totals.setdefault(
                b.player_id,
                {
                    "player_id": b.player_id,
                    "first_name": b.first_name,
                    "last_name": b.last_name,
                    "player_code": b.player_code,
                    "team_id": entry.innings.bowling_team_id,
                    "innings": 0,
                    "balls_bowled": 0,
                    "runs_conceded": 0,
                    "wickets": 0,
                    "maidens": 0,
                },
            )
            acc["innings"] += 1
            acc["balls_bowled"] += b.balls_bowled
            acc["runs_conceded"] += b.runs_conceded
            acc["wickets"] += b.wickets
            acc["maidens"] += b.maidens
    rows = []
    for player_id, acc in totals.items():
        full, rem = divmod(acc["balls_bowled"], 6)
        rows.append(
            {
                **acc,
                "full_name": _player_name(acc["first_name"], acc["last_name"], player_id),
                "overs": round(full + rem / 6, 2),
                "overs_str": f"{full}.{rem}",
                "economy": round(acc["runs_conceded"] / (acc["balls_bowled"] / 6), 2)
                if acc["balls_bowled"]
                else 0.0,
            }
        )
    return rows


# --- Match-wide charts -----------------------------------------------------


async def get_runs_by_over(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Runs scored in each over of every innings (bar-chart slice)."""
    innings = []
    for entry in await _load_innings(db, match, innings_number, with_cards=False):
        built = build_innings_analytics(
            entry.innings, entry.deliveries, entry.identities, _max_overs_for(match, entry.innings)
        )
        frame = _innings_frame(entry, match)
        frame.update(
            {
                "total": built["total"],
                "wickets": built["wickets"],
                "legal_balls": built["legal_balls"],
                "overs_bowled_str": built["overs_bowled_str"],
            }
        )
        frame["overs"] = built["over_by_over"]
        innings.append(frame)
    return {"match_id": match.id, "innings": innings}


async def get_innings_progression(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Cumulative run progression over each innings (line-chart slice)."""
    innings = []
    for entry in await _load_innings(db, match, innings_number, with_cards=False):
        built = build_innings_analytics(
            entry.innings, entry.deliveries, entry.identities, _max_overs_for(match, entry.innings)
        )
        frame = _innings_frame(entry, match)
        frame.update(
            {
                "total": built["total"],
                "wickets": built["wickets"],
                "legal_balls": built["legal_balls"],
                "overs_bowled_str": built["overs_bowled_str"],
            }
        )
        frame["progression"] = built["run_rate_progression"]
        innings.append(frame)
    return {"match_id": match.id, "innings": innings}


async def get_player_runs(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Aggregated batting figures for the match, ranked by runs."""
    rows = _aggregate_batting(await _load_innings(db, match, innings_number))
    rows.sort(key=lambda r: (-r["runs"], -r["strike_rate"], r["balls_faced"], r["player_id"]))
    return {"match_id": match.id, "players": rows}


async def get_player_strike_rates(
    db: AsyncSession,
    match: Match,
    innings_number: int | None = None,
    min_balls_faced: int = 1,
) -> dict:
    """Player strike rates for the match, ranked by strike rate."""
    rows = _aggregate_batting(await _load_innings(db, match, innings_number))
    rows = [row for row in rows if row["balls_faced"] >= min_balls_faced]
    rows.sort(key=lambda r: (-r["strike_rate"], -r["runs"], r["balls_faced"], r["player_id"]))
    return {"match_id": match.id, "players": rows}


async def get_bowler_economies(
    db: AsyncSession,
    match: Match,
    innings_number: int | None = None,
    min_overs: float = 0.0,
) -> dict:
    """Bowling figures for the match, ranked by economy (best first)."""
    rows = _aggregate_bowling(await _load_innings(db, match, innings_number))
    rows = [row for row in rows if row["overs"] >= min_overs]
    rows.sort(key=lambda r: (r["economy"], -r["wickets"], r["runs_conceded"], r["player_id"]))
    return {"match_id": match.id, "bowlers": rows}


async def get_wickets_by_bowler(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Wickets by bowler for the match, ranked by wickets."""
    rows = _aggregate_bowling(await _load_innings(db, match, innings_number))
    rows.sort(key=lambda r: (-r["wickets"], r["economy"], r["runs_conceded"], r["player_id"]))
    return {"match_id": match.id, "bowlers": rows}


def _distribution(built: dict) -> dict:
    pattern = built["scoring_pattern"]
    legal = pattern["legal_balls"]
    other = legal - (
        pattern["dot_balls"]
        + pattern["singles"]
        + pattern["doubles"]
        + pattern["triples"]
        + pattern["fours"]
        + pattern["sixes"]
    )
    return {
        "dot_balls": pattern["dot_balls"],
        "ones": pattern["singles"],
        "twos": pattern["doubles"],
        "threes": pattern["triples"],
        "fours": pattern["fours"],
        "sixes": pattern["sixes"],
        "other": max(other, 0),
        "legal_balls": pattern["legal_balls"],
        "deliveries": pattern["deliveries"],
        "runs_off_bat": pattern["runs_off_bat"],
        "extras": pattern["extras"],
    }


async def get_run_distribution(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """How the runs were made: the per-ball run histogram of every innings."""
    innings = []
    totals = {
        "dot_balls": 0,
        "ones": 0,
        "twos": 0,
        "threes": 0,
        "fours": 0,
        "sixes": 0,
        "other": 0,
        "legal_balls": 0,
        "deliveries": 0,
        "runs_off_bat": 0,
        "extras": 0,
    }
    for entry in await _load_innings(db, match, innings_number, with_cards=False):
        built = build_innings_analytics(
            entry.innings, entry.deliveries, entry.identities, _max_overs_for(match, entry.innings)
        )
        dist = _distribution(built)
        frame = _innings_frame(entry, match)
        frame["total"] = built["total"]
        frame["wickets"] = built["wickets"]
        frame["legal_balls"] = built["legal_balls"]
        frame["overs_bowled_str"] = built["overs_bowled_str"]
        frame["distribution"] = dist
        frame["extras_breakdown"] = built["extras"]
        innings.append(frame)
        for key in totals:
            totals[key] += dist[key]
    return {"match_id": match.id, "match_totals": totals, "innings": innings}


def _partnerships_for(innings: Innings, deliveries: list) -> list[dict]:
    # The cricket rules live in app.scoring.partnerships (the same module the
    # live scorecard reads); this only maps each stand onto the chart's keys.
    stands = compute_partnerships(deliveries, innings.target)
    return [
        {
            "pair": (stand.batter1.player_id, stand.batter2.player_id),
            "runs": stand.total_runs,
            "balls": stand.total_balls,
            "player_out_id": stand.player_out_id,
            "unbroken": stand.unbroken,
            "for_wicket": number,
        }
        for number, stand in enumerate(stands, 1)
    ]


async def get_partnerships(
    db: AsyncSession, match: Match, innings_number: int | None = None
) -> dict:
    """Partnership contributions: runs added by each pair before a wicket."""
    innings = []
    for entry in await _load_innings(db, match, innings_number, with_cards=False):
        stands = _partnerships_for(entry.innings, entry.deliveries)
        enriched = []
        for number, stand in enumerate(stands, start=1):
            striker, non_striker = stand["pair"]
            enriched.append(
                {
                    "partnership_number": number,
                    "batsman_1_id": striker,
                    "batsman_1_name": _player_name(
                        *entry.identities.get(striker, (None, None, None))[:2], striker
                    ),
                    "batsman_2_id": non_striker,
                    "batsman_2_name": _player_name(
                        *entry.identities.get(non_striker, (None, None, None))[:2], non_striker
                    ),
                    "runs": stand["runs"],
                    "balls": stand["balls"],
                    "for_wicket": stand["for_wicket"],
                    "unbroken": stand["unbroken"],
                    "player_out_id": stand["player_out_id"],
                    "player_out_name": (
                        _player_name(
                            *entry.identities.get(stand["player_out_id"], (None, None, None))[:2],
                            stand["player_out_id"],
                        )
                        if stand["player_out_id"] is not None
                        else None
                    ),
                }
            )
        frame = _innings_frame(entry, match)
        frame["partnerships"] = enriched
        innings.append(frame)
    return {"match_id": match.id, "innings": innings}


# --- Team-wide charts ------------------------------------------------------


async def _team_matches_chronological(db: AsyncSession, team_id: int, user_id: int) -> list[Match]:
    result = await db.execute(
        select(Match)
        .where(
            Match.user_id == user_id,
            Match.status == MatchStatus.COMPLETED.value,
            or_(Match.team_a_id == team_id, Match.team_b_id == team_id),
        )
        .order_by(Match.match_date.asc(), Match.id.asc())
    )
    return list(result.scalars().all())


async def _scorecards(db: AsyncSession, innings_list: list[Innings]) -> dict[int, object]:
    engine = ScoreEngine(ScoringRepository(db))
    cards: dict[int, object] = {}
    for innings in innings_list:
        try:
            cards[innings.id] = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            cards[innings.id] = None
    return cards


async def get_win_loss_trend(db: AsyncSession, team: Team, user_id: int) -> dict:
    """The team's record through its completed matches, oldest to newest.

    Each point carries the running win/loss/draw tallies, so the chart is a
    cumulative trend line.
    """
    matches = await _team_matches_chronological(db, team.id, user_id)
    points = []
    wins = losses = draws = 0
    opponent_ids: set[int] = set()

    for match in matches:
        innings_list = await _list_innings(db, match.id)
        cards = await _scorecards(db, innings_list)
        normal = [i for i in innings_list if not i.is_super_over]
        team_card = None
        opponent_card = None
        for innings in normal:
            card = cards.get(innings.id)
            if card is None:
                continue
            if innings.batting_team_id == team.id and team_card is None:
                team_card = card
            elif innings.batting_team_id != team.id and opponent_card is None:
                opponent_card = card
        if team_card is None:
            continue

        winner_id, margin = derive_result(innings_list, cards)
        if winner_id == team.id:
            result = "win"
            wins += 1
        elif winner_id is None:
            result = "draw"
            draws += 1
        else:
            result = "loss"
            losses += 1

        opponent_id = match.team_b_id if match.team_a_id == team.id else match.team_a_id
        opponent_ids.add(opponent_id)
        points.append(
            {
                "match_id": match.id,
                "match_date": match.match_date,
                "match_type": match.match_type,
                "opponent_id": opponent_id,
                "result": result,
                "result_code": _RESULT_CODES[result],
                "team_score": team_card.total,
                "opponent_score": opponent_card.total if opponent_card is not None else None,
                "margin": margin,
                "cumulative_wins": wins,
                "cumulative_losses": losses,
                "cumulative_draws": draws,
                "cumulative_matches": wins + losses + draws,
                "win_percentage": round(wins * 100 / (wins + losses + draws), 2),
            }
        )

    if opponent_ids:
        result = await db.execute(select(Team.id, Team.name).where(Team.id.in_(opponent_ids)))
        names = {row[0]: row[1] for row in result.all()}
        for point in points:
            point["opponent_name"] = names.get(point["opponent_id"])

    return {
        "team_id": team.id,
        "team_name": team.name,
        "short_name": team.short_name,
        "matches_played": len(points),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_percentage": round(wins * 100 / len(points), 2) if points else 0.0,
        "trend": points,
    }


async def get_recent_form(db: AsyncSession, team: Team, user_id: int, recent: int = 5) -> dict:
    """The team's most recent completed results, newest first."""
    matches = await _team_matches_chronological(db, team.id, user_id)
    matches.reverse()
    return await _form_points(db, team, matches, recent)


async def _form_points(
    db: AsyncSession, team: Team, matches: list[Match], limit: int | None
) -> dict:
    points = []
    opponent_ids: set[int] = set()
    for match in matches[:limit] if limit is not None else matches:
        innings_list = await _list_innings(db, match.id)
        cards = await _scorecards(db, innings_list)
        normal = [i for i in innings_list if not i.is_super_over]
        team_card = None
        opponent_card = None
        for innings in normal:
            card = cards.get(innings.id)
            if card is None:
                continue
            if innings.batting_team_id == team.id and team_card is None:
                team_card = card
            elif innings.batting_team_id != team.id and opponent_card is None:
                opponent_card = card
        if team_card is None:
            continue
        winner_id, margin = derive_result(innings_list, cards)
        if winner_id == team.id:
            result = "win"
        elif winner_id is None:
            result = "draw"
        else:
            result = "loss"
        opponent_id = match.team_b_id if match.team_a_id == team.id else match.team_a_id
        opponent_ids.add(opponent_id)
        points.append(
            {
                "match_id": match.id,
                "match_date": match.match_date,
                "match_type": match.match_type,
                "opponent_id": opponent_id,
                "result": result,
                "result_code": _RESULT_CODES[result],
                "team_score": team_card.total,
                "opponent_score": opponent_card.total if opponent_card is not None else None,
                "margin": margin,
            }
        )
    if opponent_ids:
        result = await db.execute(select(Team.id, Team.name).where(Team.id.in_(opponent_ids)))
        names = {row[0]: row[1] for row in result.all()}
        for point in points:
            point["opponent_name"] = names.get(point["opponent_id"])
    return {
        "team_id": team.id,
        "team_name": team.name,
        "short_name": team.short_name,
        "recent_form": points,
        "recent_form_string": "-".join(point["result_code"] for point in points),
    }


__all__ = [
    "get_runs_by_over",
    "get_innings_progression",
    "get_player_runs",
    "get_player_strike_rates",
    "get_bowler_economies",
    "get_wickets_by_bowler",
    "get_run_distribution",
    "get_partnerships",
    "get_win_loss_trend",
    "get_recent_form",
]
