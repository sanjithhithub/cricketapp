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

A pure knockout competition has no league table at all. Its fixtures still
produce a summary and leaderboards, so those views are computed from the same
completed-match set rather than from the (deliberately empty) points table.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

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
from app.tournaments.enums import FixtureStage
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


async def match_winner_id(db: AsyncSession, match) -> int | None:
    """The winner of a completed match, or ``None`` when it has none.

    A tie, an abandoned chase and a match that is not completed all return
    ``None``. Callers use that to refuse to advance a knockout team rather than
    guessing which side should go through.
    """
    if match is None or match.status != MatchStatus.COMPLETED.value:
        return None
    innings = await _match_innings(db, match.id)
    cards = await _scorecards(db, innings)
    kind, winner_id = _fixture_outcome(innings, cards)
    return winner_id if kind == "result" else None


def _overs_credited(card: ScorecardDTO) -> float:
    if card.legal_balls <= 0:
        return 0.0
    if card.wickets >= _ALL_OUT_WICKETS and card.max_overs:
        return float(card.max_overs)
    return card.legal_balls / 6


def _name(first: str | None, last: str | None, player_id: int) -> str:
    return full_name(first, last) or f"Player {player_id}"


def _same_pair(fixture: TournamentFixture, match) -> bool:
    return {fixture.team_a_id, fixture.team_b_id} == {match.team_a_id, match.team_b_id}


async def build_points_table(db: AsyncSession, tournament: Tournament) -> list[dict]:
    """The league standings, most points first, with net run rate as tie-break.

    A pure knockout tournament returns an empty list: a league table is not a
    meaningful view of a bracket. A league-plus-knockout tournament counts only
    its ``league`` stage fixtures, so a semi-final cannot change the positions
    that decided who reached the semi-finals.

    Each linked match is counted at most once. The same ``match_id`` on two
    fixtures is a data error, not a second result, and a duplicate would
    otherwise double a team's played/points totals.
    """
    if tournament.format == "knockout":
        return []

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

    counted_matches: set[int] = set()
    for fixture in fixtures:
        match = fixture.match
        if match is None or match.status != MatchStatus.COMPLETED.value:
            continue
        if fixture.stage != FixtureStage.LEAGUE.value:
            continue
        if fixture.team_a_id not in stats or fixture.team_b_id not in stats:
            continue
        if not _same_pair(fixture, match):
            continue
        if match.id in counted_matches:
            continue
        counted_matches.add(match.id)

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
    """Team ids in seeding order, for a knockout draw.

    A league competition seeds from its points table. A pure knockout has no
    table to seed from - its teams are listed in entry order, which is the only
    order the organiser has actually expressed. Falling back to an empty table
    here is what used to make a knockout draw impossible to regenerate.
    """
    teams = await load_tournament_teams(db, tournament.id)
    entry_order = [team.id for team in teams]
    if tournament.format == "knockout":
        return entry_order
    seeded = [row["team_id"] for row in await build_points_table(db, tournament)]
    extra = [team_id for team_id in entry_order if team_id not in seeded]
    return seeded + extra


async def build_summary(db: AsyncSession, tournament: Tournament) -> dict:
    """Fixtures and results at tournament scope, counted once per match.

    The counts are *team results*: a decided match contributes one win and one
    loss, a tie contributes two ties, and an abandoned match contributes two
    no-results. ``wins + losses + ties + no_results`` therefore equals
    ``2 * matches_played``, which keeps the four cards mutually consistent.
    Only completed, linked, de-duplicated matches are eligible; an upcoming
    fixture is a plan, not a result.
    """
    teams = await load_tournament_teams(db, tournament.id)
    fixtures = await load_fixtures(db, tournament.id)
    team_ids = {team.id for team in teams}

    matches_played = 0
    wins = 0
    losses = 0
    ties = 0
    no_results = 0
    champion_team_id: int | None = None
    counted_matches: set[int] = set()

    for fixture in fixtures:
        match = fixture.match
        if match is None or match.status != MatchStatus.COMPLETED.value:
            continue
        if match.id in counted_matches:
            continue
        if fixture.team_a_id not in team_ids or fixture.team_b_id not in team_ids:
            continue
        if not _same_pair(fixture, match):
            continue
        counted_matches.add(match.id)

        innings = await _match_innings(db, match.id)
        cards = await _scorecards(db, innings)
        kind, winner_id = _fixture_outcome(innings, cards)
        matches_played += 1
        if kind == "no_result":
            no_results += 2
        elif winner_id is None:
            ties += 2
        else:
            wins += 1
            losses += 1
            if fixture.stage == FixtureStage.FINAL.value:
                champion_team_id = winner_id

    champion_team_name = next((team.name for team in teams if team.id == champion_team_id), None)

    applicable = tournament.format != "knockout"
    if not applicable:
        message = "Not applicable for knockout format."
    elif matches_played == 0:
        message = "No completed matches yet. Standings update when results are linked."
    else:
        message = None

    return {
        "tournament_id": tournament.id,
        "tournament_name": tournament.name,
        "format": tournament.format,
        "status": tournament.status,
        "team_count": len(teams),
        "fixture_count": len(fixtures),
        "matches_played": matches_played,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "no_results": no_results,
        "champion_team_id": champion_team_id,
        "champion_team_name": champion_team_name,
        "points_table_empty": matches_played == 0,
        "points_table_applicable": applicable,
        "message": message,
    }


async def _completed_fixture_innings(
    db: AsyncSession, fixtures: list[TournamentFixture]
) -> list[tuple[TournamentFixture, list[Innings], dict[int, ScorecardDTO | None]]]:
    loaded = []
    counted_matches: set[int] = set()
    for fixture in fixtures:
        match = fixture.match
        if match is None or match.status != MatchStatus.COMPLETED.value:
            continue
        if match.id in counted_matches:
            continue
        counted_matches.add(match.id)
        innings = await _match_innings(db, match.id)
        loaded.append((fixture, innings, await _scorecards(db, innings)))
    return loaded


async def build_leaderboards(db: AsyncSession, tournament: Tournament, top: int = 10) -> dict:
    """The tournament's leading run scorers and wicket takers.

    Performances are summed across every completed fixture, so an entry is a
    tournament aggregate rather than a single innings. Only normal (non Super
    Over) innings count, matching how the rest of the app reports batting and
    bowling. Each linked match is read once even if a data error points two
    fixtures at it, so a player cannot appear twice for one innings.
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
        "matches_considered": len(loaded),
    }


async def sync_bracket_progression(
    db: AsyncSession, tournament_id: int, *, commit: bool = False
) -> bool:
    """Fill each knockout slot from the winner of the fixture that feeds it.

    The edge is ``(next_fixture_id, next_slot)`` on the earlier fixture. A
    completed match with a decisive winner fills the slot; a tie, an abandoned
    chase, an unfinished match or a missing match leaves/clears it, because no
    team may be advanced without actually winning.

    A downstream fixture that already has a linked match is never rewritten: its
    participants belong to a result that has been recorded. Callers that change
    an upstream result are expected to refuse such a change (see
    :func:`app.tournaments.crud.set_fixture_result`), and this function leaves
    the recorded match untouched if one slips through.

    Returns whether anything changed. The caller commits; a read view that calls
    this with ``commit=True`` does so to keep the bracket honest after a
    scorecard edit.
    """
    fixtures = await load_fixtures(db, tournament_id)
    by_id = {fixture.id: fixture for fixture in fixtures}
    changed = False

    for feeder in fixtures:
        target_id = feeder.next_fixture_id
        if target_id is None or feeder.next_slot not in {"a", "b"}:
            continue
        target = by_id.get(target_id)
        if target is None:
            continue

        winner_id = await match_winner_id(db, feeder.match)
        slot = f"team_{feeder.next_slot}_id"
        current = getattr(target, slot)
        if current == winner_id:
            continue
        if target.match_id is not None:
            # A recorded result is not rewritten by a later edit upstream.
            continue
        other_slot = "team_b_id" if feeder.next_slot == "a" else "team_a_id"
        if winner_id is not None and getattr(target, other_slot) == winner_id:
            # The same team in both slots at once is never a valid bracket.
            winner_id = None
        setattr(target, slot, winner_id)
        changed = True

    if changed and commit:
        await db.commit()
    return changed


async def sync_match_fixture_progression(db: AsyncSession, match) -> bool:
    """Advance the bracket of a fixture-started match after it is completed.

    A match is linked to its fixture when it is started, so completion can find
    the tournament without guessing from team names or dates. A legacy match
    that was never linked is deliberately ignored: linking one is an explicit
    manual recovery action, not an automatic guess.

    The write is idempotent. :func:`sync_bracket_progression` only writes a slot
    whose value differs, and it never rewrites a downstream fixture that has
    its own linked result.
    """
    row = (
        await db.execute(
            select(TournamentFixture.tournament_id).where(TournamentFixture.match_id == match.id)
        )
    ).first()
    if row is None:
        return False
    return await sync_bracket_progression(db, row.tournament_id, commit=False)


def _fixture_warnings(fixtures: list[TournamentFixture]) -> dict[int, list[str]]:
    """Scheduling conflicts, keyed by fixture id.

    Only a date *and* a time can clash; an undated fixture is a plan awaiting a
    slot and is not reported. The check is deliberately a warning rather than a
    rejection: a ground may host two games at once only if the organiser says so.
    """
    warnings: dict[int, list[str]] = defaultdict(list)
    slots: dict[tuple[date, str], list[TournamentFixture]] = defaultdict(list)
    for fixture in fixtures:
        if fixture.scheduled_date is None or not fixture.scheduled_time:
            continue
        slots[(fixture.scheduled_date, fixture.scheduled_time)].append(fixture)

    for group in slots.values():
        if len(group) < 2:
            continue
        for fixture in group:
            for other in group:
                if other.id == fixture.id:
                    continue
                shares_team = bool(
                    {fixture.team_a_id, fixture.team_b_id} & {other.team_a_id, other.team_b_id}
                )
                same_venue = (
                    bool(fixture.venue)
                    and bool(other.venue)
                    and fixture.venue.strip().lower() == other.venue.strip().lower()
                )
                if shares_team:
                    warnings[fixture.id].append(
                        f"Team clash: a participating team also has fixture #{other.id} "
                        "at this date and time."
                    )
                if same_venue:
                    warnings[fixture.id].append(
                        f"Venue clash: fixture #{other.id} uses the same venue at this date "
                        "and time."
                    )
    return warnings


async def fixture_public_rows(db: AsyncSession, tournament_id: int) -> list[dict]:
    """Every fixture of a tournament as a response dict.

    The match status and result are folded in so a schedule renders a completed
    fixture's score and winner without following the ``match_id`` itself. A
    fixture with no match is ``"scheduled"``. A fixture whose participants are
    still to be decided reports ``is_tbd`` and null team fields rather than a
    guessed team. The bracket is reconciled first so a win recorded since the
    last request is reflected in the next round.
    """
    await sync_bracket_progression(db, tournament_id, commit=True)
    fixtures = await load_fixtures(db, tournament_id)
    warnings = _fixture_warnings(fixtures)
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
                "result": (
                    match.result
                    if match is not None and match.status == MatchStatus.COMPLETED.value
                    else None
                ),
                "winner_team_id": winner_team_id,
                "next_fixture_id": fixture.next_fixture_id,
                "next_slot": fixture.next_slot,
                "is_tbd": fixture.team_a_id is None or fixture.team_b_id is None,
                "warnings": warnings.get(fixture.id, []),
            }
        )
    return rows


__all__ = [
    "build_leaderboards",
    "build_points_table",
    "build_summary",
    "fixture_public_rows",
    "load_fixtures",
    "load_tournament_teams",
    "match_winner_id",
    "ranked_team_ids",
    "sync_bracket_progression",
    "sync_match_fixture_progression",
]
