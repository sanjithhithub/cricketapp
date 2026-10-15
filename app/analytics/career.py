"""Player career statistics, split every way a selectors' page needs.

One pass over every innings a player has appeared in produces the career total
and the splits: by season (the calendar year of the match), by competition (the
level the player's side plays at), by tournament (the competition a match was
part of, when it was played under one) and by team. A recent-innings log is built
from the same pass, newest first.

Every figure comes from the shared :class:`PlayerStatsAccumulator`, so a career
figure here, on the profile and on the performance endpoint is always the same
number. Only finished matches count by default - a live match's figures would
move under the reader - but ``completed_only=False`` reports everything the
player has played.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.levels.models import TeamLevel
from app.matches.models import Match
from app.players.analytics import (
    PlayerStatsAccumulator,
    fielding_block,
    fielding_counts,
    load_player_innings,
)
from app.players.identity import full_name
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.models import Innings
from app.teams.models import Team
from app.tournaments.models import Tournament, TournamentFixture

# Used only when an innings' side cannot be resolved to a level (a team row with
# no matching level). The innings still counts rather than silently vanishing.
UNKNOWN_LEVEL_ID = 0
UNKNOWN_LEVEL_NAME = "Unknown competition"

NO_TOURNAMENT_NAME = "No tournament"


async def _team_meta(db: AsyncSession, team_ids) -> dict[int, tuple[int, str, str | None, str | None]]:
    """Map team id -> (level_id, level_name, team_name, team_short_name)."""
    ids = {t for t in team_ids if t is not None}
    if not ids:
        return {}
    result = await db.execute(
        select(Team.id, Team.level_id, TeamLevel.name, Team.name, Team.short_name)
        .outerjoin(TeamLevel, TeamLevel.id == Team.level_id)
        .where(Team.id.in_(ids))
    )
    return {
        row[0]: (
            row[1] if row[1] is not None else UNKNOWN_LEVEL_ID,
            row[2] or UNKNOWN_LEVEL_NAME,
            row[3],
            row[4],
        )
        for row in result.all()
    }


def _player_team_id(innings: Innings, card, player_id: int) -> int:
    """Which side the player turned out for in this innings.

    A player named in the batting order plays for the batting side, whether or
    not they actually came in; a substitute who only bowled plays for the bowling
    side.
    """
    if card is not None and any(b.player_id == player_id for b in card.batsmen):
        return innings.batting_team_id
    return innings.bowling_team_id


def _recent_entry(innings: Innings, card, deliveries, player_id, match_date, match_type) -> dict:
    bat = next((b for b in card.batsmen if b.player_id == player_id), None)
    bowl = next((b for b in card.bowlers if b.player_id == player_id), None)
    did_bat = bat is not None and not bat.did_not_bat
    return {
        "match_id": innings.match_id,
        "match_date": match_date,
        "match_type": match_type or "",
        "innings_number": innings.innings_number,
        "is_super_over": innings.is_super_over,
        "batting": {
            "did_bat": did_bat,
            "runs": bat.runs if did_bat else 0,
            "balls_faced": bat.balls_faced if did_bat else 0,
            "fours": bat.fours if did_bat else 0,
            "sixes": bat.sixes if did_bat else 0,
            "strike_rate": bat.strike_rate if did_bat else 0.0,
            "out": bat.out if did_bat else False,
            "dismissal": bat.dismissal if did_bat else None,
        },
        "bowling": {
            "bowled": bowl is not None,
            "balls_bowled": bowl.balls_bowled if bowl is not None else 0,
            "overs_str": bowl.overs_str if bowl is not None else "0.0",
            "runs_conceded": bowl.runs_conceded if bowl is not None else 0,
            "wickets": bowl.wickets if bowl is not None else 0,
            "economy": bowl.economy if bowl is not None else 0.0,
        },
        "fielding": fielding_block(fielding_counts(innings, deliveries, player_id)),
    }


async def get_player_career_stats(
    db: AsyncSession,
    player,
    user_id: int,
    *,
    completed_only: bool = True,
    recent: int = 10,
) -> dict:
    """Build the career-statistics payload for ``player``."""
    innings_list = await load_player_innings(
        db, player.id, user_id, completed_only=completed_only
    )
    repo = ScoringRepository(db)
    engine = ScoreEngine(repo)

    match_ids = {innings.match_id for innings in innings_list}
    matches: dict[int, tuple] = {}
    fixture_tournaments: dict[int, tuple[int, str]] = {}
    if match_ids:
        rows = await db.execute(
            select(Match.id, Match.match_date, Match.match_type).where(Match.id.in_(match_ids))
        )
        matches = {row[0]: (row[1], row[2]) for row in rows.all()}
        fixture_rows = await db.execute(
            select(TournamentFixture.match_id, Tournament.id, Tournament.name)
            .join(Tournament, Tournament.id == TournamentFixture.tournament_id)
            .where(TournamentFixture.match_id.in_(match_ids))
            .order_by(TournamentFixture.id)
        )
        for match_id, tournament_id, name in fixture_rows.all():
            fixture_tournaments.setdefault(match_id, (tournament_id, name))

    loaded: list[tuple[Innings, object, list]] = []
    team_ids: set[int] = set()
    for innings in innings_list:
        try:
            card = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            continue
        deliveries = await repo.list_deliveries(innings.id)
        loaded.append((innings, card, deliveries))
        team_ids.update((innings.batting_team_id, innings.bowling_team_id))

    meta = await _team_meta(db, team_ids)

    career = PlayerStatsAccumulator()
    by_season: dict[int, PlayerStatsAccumulator] = {}
    by_competition: dict[int, PlayerStatsAccumulator] = {}
    by_tournament: dict[int | None, PlayerStatsAccumulator] = {}
    by_team: dict[int, PlayerStatsAccumulator] = {}
    competition_names: dict[int, str] = {}
    tournament_names: dict[int | None, str] = {}
    team_names: dict[int, tuple] = {}

    for innings, card, deliveries in loaded:
        career.add_innings(innings, card, player.id, deliveries)

        match_date = matches.get(innings.match_id, (None, None))[0]
        season = match_date.year if match_date is not None else 0
        by_season.setdefault(season, PlayerStatsAccumulator()).add_innings(
            innings, card, player.id, deliveries
        )

        team_id = _player_team_id(innings, card, player.id)
        level_id, level_name, team_name, team_short = meta.get(
            team_id, (UNKNOWN_LEVEL_ID, UNKNOWN_LEVEL_NAME, None, None)
        )
        competition_names[level_id] = level_name
        by_competition.setdefault(level_id, PlayerStatsAccumulator()).add_innings(
            innings, card, player.id, deliveries
        )

        team_names[team_id] = (team_name, team_short)
        by_team.setdefault(team_id, PlayerStatsAccumulator()).add_innings(
            innings, card, player.id, deliveries
        )

        tournament = fixture_tournaments.get(innings.match_id)
        key = tournament[0] if tournament is not None else None
        tournament_names[key] = tournament[1] if tournament is not None else NO_TOURNAMENT_NAME
        by_tournament.setdefault(key, PlayerStatsAccumulator()).add_innings(
            innings, card, player.id, deliveries
        )

    recent_form = []
    for innings, card, deliveries in reversed(loaded):
        match_date, match_type = matches.get(innings.match_id, (None, None))
        recent_form.append(
            _recent_entry(innings, card, deliveries, player.id, match_date, match_type)
        )
        if len(recent_form) >= recent:
            break

    return {
        "player_id": player.id,
        "player_code": player.player_code,
        "first_name": player.first_name,
        "last_name": player.last_name,
        "full_name": full_name(player.first_name, player.last_name),
        "scope": "completed" if completed_only else "all",
        "career": career.stats_payload(),
        "by_season": [
            {"season": season, **stats.stats_payload()}
            for season, stats in sorted(by_season.items(), key=lambda item: item[0], reverse=True)
        ],
        "by_competition": [
            {"level_id": level_id, "level_name": competition_names[level_id], **stats.stats_payload()}
            for level_id, stats in sorted(
                by_competition.items(), key=lambda item: competition_names[item[0]].lower()
            )
        ],
        "by_tournament": [
            {"tournament_id": key, "tournament_name": tournament_names[key], **stats.stats_payload()}
            for key, stats in sorted(
                by_tournament.items(), key=lambda item: tournament_names[item[0]].lower()
            )
        ],
        "by_team": [
            {
                "team_id": team_id,
                "team_name": team_names.get(team_id, (None, None))[0],
                "team_short_name": team_names.get(team_id, (None, None))[1],
                **stats.stats_payload(),
            }
            for team_id, stats in sorted(
                by_team.items(), key=lambda item: (team_names.get(item[0], (None, None))[0] or "").lower()
            )
        ],
        "recent_form": recent_form,
    }


__all__ = ["get_player_career_stats"]
