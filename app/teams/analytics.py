"""Team analytics: a team's record across its completed matches.

Reads every finished match the account owns that this team played in, and turns
the innings scorecards into the numbers a team page shows: matches played, wins,
losses, win percentage, runs scored and conceded, the highest and lowest innings,
the average scoring rate, recent form, and the leading run scorer and wicket
taker.

Every figure comes from the score engine's scorecard, and the winner of each
match comes from the same structural derivation the match summary uses
(``derive_result``), so a team's record can never disagree with the scorecards a
user has already seen. Only *completed* matches are counted: a live match has no
result and its totals would move under the reader.

Super Over innings are excluded from the batting figures (they are one-over
sides that would wreck a "lowest score"), but they still decide the winner of a
tied match through ``derive_result``.

Fielding attribution is not stored anywhere in the delivery log, so no fielding
figures are reported.
"""

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.enums import MatchStatus
from app.scoring.models import Innings
from app.scoring.summary import derive_result
from app.teams.models import Team

_RESULT_CODES = {"win": "W", "loss": "L", "draw": "D"}


async def _completed_matches(db: AsyncSession, team_id: int, user_id: int) -> list[Match]:
    """This team's finished matches, most recent first."""
    result = await db.execute(
        select(Match)
        .where(
            Match.user_id == user_id,
            Match.status == MatchStatus.COMPLETED.value,
            or_(Match.team_a_id == team_id, Match.team_b_id == team_id),
        )
        .order_by(Match.match_date.desc(), Match.id.desc())
    )
    return list(result.scalars().all())


async def _match_innings(db: AsyncSession, match_id: int) -> list[Innings]:
    result = await db.execute(
        select(Innings)
        .where(Innings.match_id == match_id)
        .order_by(Innings.innings_number, Innings.id)
    )
    return list(result.scalars().all())


async def _scorecards(engine: ScoreEngine, innings_list: list[Innings]) -> dict[int, object]:
    cards: dict[int, object] = {}
    for innings in innings_list:
        try:
            cards[innings.id] = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            cards[innings.id] = None
    return cards


async def _team_names(db: AsyncSession, team_ids) -> dict[int, dict]:
    ids = {tid for tid in team_ids if tid is not None}
    if not ids:
        return {}
    result = await db.execute(select(Team.id, Team.name, Team.short_name).where(Team.id.in_(ids)))
    return {row[0]: {"id": row[0], "name": row[1], "short_name": row[2]} for row in result.all()}


def _player_name(first: str | None, last: str | None, player_id: int) -> str:
    return full_name(first, last) or f"Player {player_id}"


def _top_run_scorer(totals: dict[int, dict]) -> dict | None:
    if not totals:
        return None
    top = max(totals.values(), key=lambda a: (a["runs"], -a["balls_faced"]))
    return {
        "player_id": top["player_id"],
        "first_name": top["first_name"],
        "last_name": top["last_name"],
        "full_name": _player_name(top["first_name"], top["last_name"], top["player_id"]),
        "player_code": top["player_code"],
        "innings": top["innings"],
        "runs": top["runs"],
        "balls_faced": top["balls_faced"],
        "fours": top["fours"],
        "sixes": top["sixes"],
        "highest_score": top["highest_score"],
        "strike_rate": round(top["runs"] * 100 / top["balls_faced"], 2)
        if top["balls_faced"]
        else 0.0,
    }


def _top_wicket_taker(totals: dict[int, dict]) -> dict | None:
    if not totals:
        return None
    top = max(
        totals.values(),
        key=lambda a: (a["wickets"], -a["runs_conceded"], -a["balls_bowled"]),
    )
    full, rem = divmod(top["balls_bowled"], 6)
    best_display = (
        f"{top['best_wickets']}/{top['best_runs_conceded']}"
        if top["best_wickets"] is not None
        else None
    )
    return {
        "player_id": top["player_id"],
        "first_name": top["first_name"],
        "last_name": top["last_name"],
        "full_name": _player_name(top["first_name"], top["last_name"], top["player_id"]),
        "player_code": top["player_code"],
        "innings": top["innings"],
        "wickets": top["wickets"],
        "balls_bowled": top["balls_bowled"],
        "overs": round(full + rem / 6, 2),
        "overs_str": f"{full}.{rem}",
        "runs_conceded": top["runs_conceded"],
        "economy": round(top["runs_conceded"] / (top["balls_bowled"] / 6), 2)
        if top["balls_bowled"]
        else 0.0,
        "best_wickets": top["best_wickets"],
        "best_runs_conceded": top["best_runs_conceded"],
        "best_display": best_display,
    }


async def get_team_analytics(db: AsyncSession, team: Team, user_id: int, recent: int = 5) -> dict:
    """Build the analytics payload for ``team`` over its completed matches."""
    matches = await _completed_matches(db, team.id, user_id)
    engine = ScoreEngine(ScoringRepository(db))

    matches_played = 0
    wins = losses = draws = 0
    runs_scored = runs_conceded = 0
    legal_balls_faced = 0
    scores: list[int] = []

    bat_totals: dict[int, dict] = {}
    bowl_totals: dict[int, dict] = {}
    form_rows: list[dict] = []
    opponent_ids: set[int] = set()

    for match in matches:
        innings_list = await _match_innings(db, match.id)
        cards = await _scorecards(engine, innings_list)
        normal = [i for i in innings_list if not i.is_super_over]

        # The team's own batting innings decides whether the match counts at all:
        # a completed match with no scoreable innings for this side (abandoned, or
        # a walkover) contributes no cricket and is left out entirely.
        team_innings = next(
            (i for i in normal if i.batting_team_id == team.id and cards.get(i.id) is not None),
            None,
        )
        if team_innings is None:
            continue
        team_card = cards[team_innings.id]

        opponent_innings = next((i for i in normal if i.id != team_innings.id), None)
        opponent_card = cards.get(opponent_innings.id) if opponent_innings is not None else None

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

        matches_played += 1
        runs_scored += team_card.total
        legal_balls_faced += team_card.legal_balls
        scores.append(team_card.total)
        if opponent_card is not None:
            runs_conceded += opponent_card.total

        opponent_id = match.team_b_id if match.team_a_id == team.id else match.team_a_id
        opponent_ids.add(opponent_id)

        # Leading run scorer: only this side's batters, from its batting innings.
        for b in team_card.batsmen:
            if b.did_not_bat:
                continue
            acc = bat_totals.setdefault(
                b.player_id,
                {
                    "player_id": b.player_id,
                    "first_name": b.first_name,
                    "last_name": b.last_name,
                    "player_code": b.player_code,
                    "innings": 0,
                    "runs": 0,
                    "balls_faced": 0,
                    "fours": 0,
                    "sixes": 0,
                    "highest_score": 0,
                },
            )
            acc["innings"] += 1
            acc["runs"] += b.runs
            acc["balls_faced"] += b.balls_faced
            acc["fours"] += b.fours
            acc["sixes"] += b.sixes
            if b.runs > acc["highest_score"]:
                acc["highest_score"] = b.runs

        # Leading wicket taker: this side's bowlers, from the innings it fields.
        if opponent_card is not None:
            for b in opponent_card.bowlers:
                acc = bowl_totals.setdefault(
                    b.player_id,
                    {
                        "player_id": b.player_id,
                        "first_name": b.first_name,
                        "last_name": b.last_name,
                        "player_code": b.player_code,
                        "innings": 0,
                        "wickets": 0,
                        "balls_bowled": 0,
                        "runs_conceded": 0,
                        "best_wickets": None,
                        "best_runs_conceded": None,
                    },
                )
                acc["innings"] += 1
                acc["wickets"] += b.wickets
                acc["balls_bowled"] += b.balls_bowled
                acc["runs_conceded"] += b.runs_conceded
                # Best figures: most wickets, then fewest runs conceded.
                key = (b.wickets, -b.runs_conceded)
                best_key = (
                    (acc["best_wickets"], -acc["best_runs_conceded"])
                    if acc["best_wickets"] is not None
                    else None
                )
                if best_key is None or key > best_key:
                    acc["best_wickets"] = b.wickets
                    acc["best_runs_conceded"] = b.runs_conceded

        form_rows.append(
            {
                "match_id": match.id,
                "match_date": match.match_date,
                "match_type": match.match_type,
                "opponent_id": opponent_id,
                "result": result,
                "team_score": team_card.total,
                "opponent_score": opponent_card.total if opponent_card is not None else None,
                "margin": margin,
            }
        )

    names = await _team_names(db, opponent_ids)
    recent_form = [
        {
            **row,
            "opponent_name": names.get(row["opponent_id"], {}).get("name"),
            "result_code": _RESULT_CODES[row["result"]],
        }
        for row in form_rows[:recent]
    ]

    return {
        "team_id": team.id,
        "team_name": team.name,
        "short_name": team.short_name,
        "matches_played": matches_played,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_percentage": round(wins * 100 / matches_played, 2) if matches_played else 0.0,
        "runs_scored": runs_scored,
        "runs_conceded": runs_conceded,
        "highest_score": max(scores) if scores else None,
        "lowest_score": min(scores) if scores else None,
        "average_score": round(runs_scored / matches_played, 2) if matches_played else None,
        "average_run_rate": round(runs_scored / (legal_balls_faced / 6), 2)
        if legal_balls_faced
        else 0.0,
        "recent_form": recent_form,
        "recent_form_string": "-".join(row["result_code"] for row in recent_form),
        "top_run_scorer": _top_run_scorer(bat_totals),
        "top_wicket_taker": _top_wicket_taker(bowl_totals),
    }


# --- Head-to-head -----------------------------------------------------------


async def _head_to_head_matches(
    db: AsyncSession, team_id: int, opponent_id: int, user_id: int
) -> list[Match]:
    """Finished matches between exactly these two sides, most recent first.

    The pair is matched in either order, so a home/away swap is still the same
    fixture. Matches involving any other side are not here at all.
    """
    result = await db.execute(
        select(Match)
        .where(
            Match.user_id == user_id,
            Match.status == MatchStatus.COMPLETED.value,
            or_(
                and_(Match.team_a_id == team_id, Match.team_b_id == opponent_id),
                and_(Match.team_a_id == opponent_id, Match.team_b_id == team_id),
            ),
        )
        .order_by(Match.match_date.desc(), Match.id.desc())
    )
    return list(result.scalars().all())


def _batting_performance(b, match: Match, side_id: int) -> dict:
    return {
        "player_id": b.player_id,
        "first_name": b.first_name,
        "last_name": b.last_name,
        "full_name": _player_name(b.first_name, b.last_name, b.player_id),
        "player_code": b.player_code,
        "team_id": side_id,
        "match_id": match.id,
        "match_date": match.match_date,
        "runs": b.runs,
        "balls_faced": b.balls_faced,
        "fours": b.fours,
        "sixes": b.sixes,
        "out": b.out,
        "strike_rate": round(b.runs * 100 / b.balls_faced, 2) if b.balls_faced else 0.0,
    }


def _bowling_performance(b, match: Match, side_id: int) -> dict:
    full, rem = divmod(b.balls_bowled, 6)
    return {
        "player_id": b.player_id,
        "first_name": b.first_name,
        "last_name": b.last_name,
        "full_name": _player_name(b.first_name, b.last_name, b.player_id),
        "player_code": b.player_code,
        "team_id": side_id,
        "match_id": match.id,
        "match_date": match.match_date,
        "wickets": b.wickets,
        "runs_conceded": b.runs_conceded,
        "balls_bowled": b.balls_bowled,
        "overs": round(full + rem / 6, 2),
        "overs_str": f"{full}.{rem}",
        "maidens": b.maidens,
        "economy": round(b.runs_conceded / (b.balls_bowled / 6), 2) if b.balls_bowled else 0.0,
    }


async def get_head_to_head_analytics(
    db: AsyncSession,
    team: Team,
    opponent: Team,
    user_id: int,
    recent: int = 5,
    top: int = 5,
) -> dict:
    """Build the head-to-head payload for ``team`` against ``opponent``.

    Covers the total matches between the two sides, wins for each, the recent
    results, each side's highest and lowest innings, and the best individual
    batting and bowling performances from those matches.
    """
    matches = await _head_to_head_matches(db, team.id, opponent.id, user_id)
    engine = ScoreEngine(ScoringRepository(db))
    repository = ScoringRepository(db)

    matches_played = team_wins = opponent_wins = draws = 0
    team_scores: list[int] = []
    opponent_scores: list[int] = []
    result_rows: list[dict] = []
    bat_performances: list[dict] = []
    bowl_performances: list[dict] = []
    pom_ids: set[int] = set()

    for match in matches:
        innings_list = await _match_innings(db, match.id)
        cards = await _scorecards(engine, innings_list)
        normal = [i for i in innings_list if not i.is_super_over]

        # Both sides must have a scoreable innings for the match to count: with
        # only one, there is no contest to record (abandoned, or a walkover).
        team_innings = next(
            (i for i in normal if i.batting_team_id == team.id and cards.get(i.id) is not None),
            None,
        )
        opponent_innings = next(
            (i for i in normal if i.batting_team_id == opponent.id and cards.get(i.id) is not None),
            None,
        )
        if team_innings is None or opponent_innings is None:
            continue
        team_card = cards[team_innings.id]
        opponent_card = cards[opponent_innings.id]

        winner_id, margin = derive_result(innings_list, cards)
        if winner_id == team.id:
            result, code = "win", "W"
            team_wins += 1
        elif winner_id == opponent.id:
            result, code = "loss", "L"
            opponent_wins += 1
        else:
            result, code = "draw", "D"
            draws += 1

        matches_played += 1
        team_scores.append(team_card.total)
        opponent_scores.append(opponent_card.total)
        if match.player_of_match_id is not None:
            pom_ids.add(match.player_of_match_id)

        result_rows.append(
            {
                "match_id": match.id,
                "match_date": match.match_date,
                "match_type": match.match_type,
                "venue": match.venue,
                "team_score": team_card.total,
                "opponent_score": opponent_card.total,
                "result": result,
                "result_code": code,
                "winner_team_id": winner_id,
                "margin": margin,
                "player_of_match_id": match.player_of_match_id,
                "player_of_match_name": None,
            }
        )

        # Best individual performances: each innings' batsmen (who batted) and
        # bowlers (the fielding side) are kept as single-innings efforts, not
        # summed, because a "top score" is one knock.
        for innings in (team_innings, opponent_innings):
            card = cards.get(innings.id)
            if card is None:
                continue
            for b in card.batsmen:
                if b.did_not_bat:
                    continue
                bat_performances.append(_batting_performance(b, match, innings.batting_team_id))
            for b in card.bowlers:
                bowl_performances.append(_bowling_performance(b, match, innings.bowling_team_id))

    if pom_ids:
        identities = await repository.player_identities(pom_ids)
        for row in result_rows:
            pid = row["player_of_match_id"]
            if pid is None:
                continue
            first, last, _code = identities.get(pid, (None, None, None))
            row["player_of_match_name"] = _player_name(first, last, pid)

    bat_performances.sort(
        key=lambda p: (-p["runs"], p["balls_faced"], p["match_id"], p["player_id"])
    )
    bowl_performances.sort(
        key=lambda p: (
            -p["wickets"],
            p["runs_conceded"],
            p["balls_bowled"],
            p["match_id"],
            p["player_id"],
        )
    )

    recent_results = result_rows[:recent]

    return {
        "team_id": team.id,
        "team_name": team.name,
        "team_short_name": team.short_name,
        "opponent_id": opponent.id,
        "opponent_name": opponent.name,
        "opponent_short_name": opponent.short_name,
        "matches_played": matches_played,
        "team_wins": team_wins,
        "opponent_wins": opponent_wins,
        "draws": draws,
        "team_win_percentage": round(team_wins * 100 / matches_played, 2)
        if matches_played
        else 0.0,
        "team_highest_score": max(team_scores) if team_scores else None,
        "team_lowest_score": min(team_scores) if team_scores else None,
        "opponent_highest_score": max(opponent_scores) if opponent_scores else None,
        "opponent_lowest_score": min(opponent_scores) if opponent_scores else None,
        "recent_results": recent_results,
        "recent_form_string": "-".join(row["result_code"] for row in recent_results),
        "top_batting": bat_performances[:top],
        "top_bowling": bowl_performances[:top],
    }


__all__ = ["get_team_analytics", "get_head_to_head_analytics"]
