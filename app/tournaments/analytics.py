"""Derived views over a tournament: its fixtures, points table and leaderboards.

Everything here is computed from the scored matches already on file, never
stored. A points table that was persisted would be one more thing to keep in
step with the scorecards; a table recomputed per request cannot drift from the
results that produced it. Each winner is derived with :func:`derive_result`, the
same function the match summary uses, so a competition cannot disagree with the
match it is built from.

The two rules that need stating because a naive version gets them wrong:

* a fixture only counts once its linked match is *completed*. A scheduled or
  even a live fixture is a plan, not a result;
* net run rate credits a side bowled out with its full quota of overs, not the
  overs it actually faced. Without that rule a team skittled for 50 in ten overs
  would look worse than one that made the same score across twenty.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import MatchStatus
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScorecardDTO, ScoreEngine, ScoreEngineError
from app.scoring.models import Innings
from app.scoring.summary import derive_result
from app.teams.models import Team
from app.tournaments.models import Tournament, TournamentFixture, TournamentTeam

# A side bowled out is charged its full overs for net run rate. 10 is the only
# value the normal innings allows; Super Overs (2 wickets) are never counted here.
_ALL_OUT_WICKETS = 10


async def load_tournament_teams(db: AsyncSession, tournament_id: int) -> list[Team]:
    """The teams entered in a tournament, in entry order."""
    result = await db.execute(
        select(Team)
        .join(TournamentTeam, TournamentTeam.team_id == Team.id)
        .where(TournamentTeam.tournament_id == tournament_id)
        .order_by(TournamentTeam.id)
    )
    return list(result.scalars().all())


async def load_fixtures(db: AsyncSession, tournament_id: int) -> list[TournamentFixture]:
    """Every fixture of a tournament, earliest scheduled date first.

    Undated fixtures sort last so a generated-but-unscheduled round-robin still
    has a stable banded order (by round, then id) and a scheduled match is always
    ahead of one that has no date yet.
    """
    result = await db.execute(
        select(TournamentFixture)
        .options(
            selectinload(TournamentFixture.team_a),
            selectinload(TournamentFixture.team_b),
            selectinload(TournamentFixture.match),
        )
        .where(TournamentFixture.tournament_id == tournament_id)
        .order_by(
            TournamentFixture.scheduled_date.is_(None),
            TournamentFixture.scheduled_date,
            TournamentFixture.round_number.is_(None),
            TournamentFixture.round_number,
            TournamentFixture.id,
        )
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
    db: AsyncSession, innings_list: list[Innings]
) -> dict[int, ScorecardDTO | None]:
    """Each innings' scorecard, keyed by innings id (``None`` on error).

    A scorecard error yields ``None`` rather than raising, so one broken innings
    cannot take a whole points table or leaderboard down with it.
    """
    engine = ScoreEngine(ScoringRepository(db))
    cards: dict[int, ScorecardDTO | None] = {}
    for innings in innings_list:
        try:
            cards[innings.id] = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            cards[innings.id] = None
    return cards


def _team_card(
    innings_list: list[Innings], cards: dict[int, ScorecardDTO | None], team_id: int
) -> ScorecardDTO | None:
    for innings in innings_list:
        if innings.is_super_over or innings.batting_team_id != team_id:
            continue
        card = cards.get(innings.id)
        if card is not None:
            return card
    return None


def _fixture_outcome(
    innings: list[Innings], cards: dict[int, ScorecardDTO | None]
) -> tuple[str, int | None]:
    """``("result", winner_id_or_None)`` or ``("no_result", None)``.

    ``no_result`` is the honest answer whenever the second innings never
    completed - the match is on file as completed but the chase did not finish,
    which is a washout or an abandonment rather than a tie.
    """
    normal = [i for i in innings if not i.is_super_over]
    if len(normal) < 2:
        return "no_result", None
    first, second = normal[0], normal[1]
    if cards.get(first.id) is None or cards.get(second.id) is None:
        return "no_result", None
    if not cards[second.id].completed:
        return "no_result", None
    winner_id, _margin = derive_result(innings, cards)
    return "result", winner_id


def _overs_credited(card: ScorecardDTO) -> float:
    if card.legal_balls <= 0:
        return 0.0
    if card.wickets >= _ALL_OUT_WICKETS and card.max_overs:
        return float(card.max_overs)
    return card.legal_balls / 6


def _name(first: str | None, last: str | None, player_id: int) -> str:
    return full_name(first, last) or f"Player {player_id}"


async def build_points_table(db: AsyncSession, tournament: Tournament) -> list[dict]:
    """The league standings, most points first, with net run rate as tie-break."""
    teams = await load_tournament_teams(db, tournament.id)
    fixtures = await load_fixtures(db, tournament.id)

    stats: dict[int, dict] = {
        team.id: {
            "team_id": team.id,
            "team_name": team.name,
            "team_short_name": team.short_name,
            "logo": team.logo,
            "played": 0,
            "won": 0,
            "lost": 0,
            "tied": 0,
            "no_result": 0,
            "points": 0,
            "runs_for": 0,
            "overs_for": 0.0,
            "runs_against": 0,
            "overs_against": 0.0,
            "form": [],
        }
        for team in teams
    }

    for fixture in fixtures:
        match = fixture.match
        if match is None or match.status != MatchStatus.COMPLETED.value:
            continue
        if fixture.team_a_id not in stats or fixture.team_b_id not in stats:
            continue

        innings = await _match_innings(db, match.id)
        cards = await _scorecards(db, innings)
        kind, winner_id = _fixture_outcome(innings, cards)

        row_a = stats[fixture.team_a_id]
        row_b = stats[fixture.team_b_id]
        row_a["played"] += 1
        row_b["played"] += 1

        if kind == "no_result":
            row_a["no_result"] += 1
            row_b["no_result"] += 1
            row_a["points"] += tournament.points_no_result
            row_b["points"] += tournament.points_no_result
            row_a["form"].append("N")
            row_b["form"].append("N")
            continue

        card_a = _team_card(innings, cards, fixture.team_a_id)
        card_b = _team_card(innings, cards, fixture.team_b_id)
        if card_a is not None and card_b is not None:
            row_a["runs_for"] += card_a.total
            row_a["runs_against"] += card_b.total
            row_a["overs_for"] += _overs_credited(card_a)
            row_a["overs_against"] += _overs_credited(card_b)
            row_b["runs_for"] += card_b.total
            row_b["runs_against"] += card_a.total
            row_b["overs_for"] += _overs_credited(card_b)
            row_b["overs_against"] += _overs_credited(card_a)

        if winner_id is None:
            row_a["tied"] += 1
            row_b["tied"] += 1
            row_a["points"] += tournament.points_tie
            row_b["points"] += tournament.points_tie
            row_a["form"].append("T")
            row_b["form"].append("T")
        elif winner_id == fixture.team_a_id:
            row_a["won"] += 1
            row_a["points"] += tournament.points_win
            row_b["lost"] += 1
            row_b["points"] += tournament.points_loss
            row_a["form"].append("W")
            row_b["form"].append("L")
        else:
            row_b["won"] += 1
            row_b["points"] += tournament.points_win
            row_a["lost"] += 1
            row_a["points"] += tournament.points_loss
            row_b["form"].append("W")
            row_a["form"].append("L")

    rows: list[dict] = []
    for row in stats.values():
        overs_for = row["overs_for"]
        overs_against = row["overs_against"]
        against_rate = row["runs_against"] / overs_against if overs_against else 0.0
        for_rate = row["runs_for"] / overs_for if overs_for else 0.0
        row["overs_for"] = round(overs_for, 2)
        row["overs_against"] = round(overs_against, 2)
        row["net_run_rate"] = round(for_rate - against_rate, 3)
        row["form"] = "-".join(row["form"][-5:])
        rows.append(row)

    rows.sort(
        key=lambda r: (
            -r["points"],
            -r["net_run_rate"],
            -r["won"],
            r["team_name"].lower(),
        )
    )
    for position, row in enumerate(rows, start=1):
        row["position"] = position
    return rows


async def ranked_team_ids(db: AsyncSession, tournament: Tournament) -> list[int]:
    """Team ids in points-table order, for seeding a knockout draw."""
    return [row["team_id"] for row in await build_points_table(db, tournament)]


async def _completed_fixture_innings(
    db: AsyncSession, fixtures: list[TournamentFixture]
) -> list[tuple[TournamentFixture, list[Innings], dict[int, ScorecardDTO | None]]]:
    loaded = []
    for fixture in fixtures:
        match = fixture.match
        if match is None or match.status != MatchStatus.COMPLETED.value:
            continue
        innings = await _match_innings(db, match.id)
        loaded.append((fixture, innings, await _scorecards(db, innings)))
    return loaded


async def build_leaderboards(db: AsyncSession, tournament: Tournament, top: int = 10) -> dict:
    """The tournament's leading run scorers and wicket takers.

    Performances are summed across every completed fixture, so an entry is a
    tournament aggregate rather than a single innings. Only normal (non Super
    Over) innings count, matching how the rest of the app reports batting and
    bowling.
    """
    teams = {team.id: team for team in await load_tournament_teams(db, tournament.id)}
    fixtures = await load_fixtures(db, tournament.id)
    loaded = await _completed_fixture_innings(db, fixtures)

    batters: dict[int, dict] = {}
    bowlers: dict[int, dict] = {}

    for fixture, innings, cards in loaded:
        for entry in innings:
            card = cards.get(entry.id)
            if card is None or entry.is_super_over:
                continue
            team = teams.get(entry.batting_team_id)

            for b in card.batsmen:
                if b.did_not_bat:
                    continue
                acc = batters.setdefault(
                    b.player_id,
                    {
                        "player_id": b.player_id,
                        "full_name": _name(b.first_name, b.last_name, b.player_id),
                        "team_id": entry.batting_team_id,
                        "team_name": team.name if team else None,
                        "matches": set(),
                        "innings": 0,
                        "runs": 0,
                        "balls_faced": 0,
                        "fours": 0,
                        "sixes": 0,
                        "highest_score": 0,
                    },
                )
                acc["matches"].add(fixture.match_id)
                acc["innings"] += 1
                acc["runs"] += b.runs
                acc["balls_faced"] += b.balls_faced
                acc["fours"] += b.fours
                acc["sixes"] += b.sixes
                acc["highest_score"] = max(acc["highest_score"], b.runs)

            bowling_team = teams.get(entry.bowling_team_id)
            for b in card.bowlers:
                acc = bowlers.setdefault(
                    b.player_id,
                    {
                        "player_id": b.player_id,
                        "full_name": _name(b.first_name, b.last_name, b.player_id),
                        "team_id": entry.bowling_team_id,
                        "team_name": bowling_team.name if bowling_team else None,
                        "matches": set(),
                        "innings": 0,
                        "wickets": 0,
                        "balls_bowled": 0,
                        "runs_conceded": 0,
                        "best_wickets": None,
                        "best_runs_conceded": None,
                    },
                )
                acc["matches"].add(fixture.match_id)
                acc["innings"] += 1
                acc["wickets"] += b.wickets
                acc["balls_bowled"] += b.balls_bowled
                acc["runs_conceded"] += b.runs_conceded
                best_key = (
                    (acc["best_wickets"], -acc["best_runs_conceded"])
                    if acc["best_wickets"] is not None
                    else None
                )
                if best_key is None or (b.wickets, -b.runs_conceded) > best_key:
                    acc["best_wickets"] = b.wickets
                    acc["best_runs_conceded"] = b.runs_conceded

    batter_rows = []
    for acc in batters.values():
        batter_rows.append(
            {
                **acc,
                "matches": len(acc["matches"]),
                "strike_rate": round(acc["runs"] * 100 / acc["balls_faced"], 2)
                if acc["balls_faced"]
                else 0.0,
            }
        )
    batter_rows.sort(
        key=lambda r: (-r["runs"], -r["highest_score"], r["balls_faced"], r["player_id"])
    )

    bowler_rows = []
    for acc in bowlers.values():
        bowler_rows.append(
            {
                **acc,
                "matches": len(acc["matches"]),
                "overs": round(acc["balls_bowled"] / 6, 2),
                "economy": round(acc["runs_conceded"] / (acc["balls_bowled"] / 6), 2)
                if acc["balls_bowled"]
                else 0.0,
            }
        )
    bowler_rows.sort(
        key=lambda r: (
            -r["wickets"],
            r["economy"],
            r["player_id"],
        )
    )

    return {
        "top_run_scorers": batter_rows[:top],
        "top_wicket_takers": bowler_rows[:top],
    }


async def fixture_public_rows(db: AsyncSession, tournament_id: int) -> list[dict]:
    """Every fixture of a tournament as a response dict.

    The match status and result are folded in so a schedule renders a completed
    fixture's score and winner without following the ``match_id`` itself. A
    fixture with no match is ``"scheduled"``.
    """
    fixtures = await load_fixtures(db, tournament_id)
    rows: list[dict] = []
    for fixture in fixtures:
        match = fixture.match

        winner_team_id = None
        if match is not None and match.status == MatchStatus.COMPLETED.value:
            innings = await _match_innings(db, match.id)
            cards = await _scorecards(db, innings)
            kind, derived_winner = _fixture_outcome(innings, cards)
            if kind == "result":
                winner_team_id = derived_winner

        rows.append(
            {
                "id": fixture.id,
                "tournament_id": fixture.tournament_id,
                "team_a_id": fixture.team_a_id,
                "team_a_name": fixture.team_a.name if fixture.team_a else None,
                "team_a_short_name": fixture.team_a.short_name if fixture.team_a else None,
                "team_b_id": fixture.team_b_id,
                "team_b_name": fixture.team_b.name if fixture.team_b else None,
                "team_b_short_name": fixture.team_b.short_name if fixture.team_b else None,
                "match_id": fixture.match_id,
                "scheduled_date": fixture.scheduled_date,
                "scheduled_time": fixture.scheduled_time,
                "venue": fixture.venue,
                "stage": fixture.stage,
                "round_number": fixture.round_number,
                "status": match.status if match is not None else "scheduled",
                "result": match.result if match is not None else None,
                "winner_team_id": winner_team_id,
            }
        )
    return rows


__all__ = [
    "build_leaderboards",
    "build_points_table",
    "fixture_public_rows",
    "load_fixtures",
    "load_tournament_teams",
    "ranked_team_ids",
]
