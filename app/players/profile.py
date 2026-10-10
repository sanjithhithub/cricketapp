"""Player profile: identity plus career and per-competition statistics.

A player's "competition" is the level (league) their team plays at - the same
``TeamLevel`` vocabulary the app seeds (IPL, World Cup, Ranji Trophy, U19, ...).
A match does not name a competition itself, so it is resolved from the side the
player turned out for in each innings: a player named in the batting order is
playing for the batting side, a player who only bowled is playing for the
bowling side.

Only completed matches count, as a profile should not shift under the reader
while a match is still live. Every figure comes from the score engine and the
shared :class:`PlayerStatsAccumulator`, so a profile row always agrees with the
career-performance endpoint and the scorecard it was read from.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.levels.models import TeamLevel
from app.players.analytics import PlayerStatsAccumulator, load_player_innings
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.models import Innings
from app.teams.models import Team

# Used only when an innings' side cannot be resolved to a level (for example a
# team row that no longer has a matching level). The innings still counts toward
# the career total rather than silently vanishing.
UNKNOWN_LEVEL_ID = 0
UNKNOWN_LEVEL_NAME = "Unknown competition"


async def _team_levels(db: AsyncSession, team_ids) -> dict[int, tuple[int, str]]:
    ids = {t for t in team_ids if t is not None}
    if not ids:
        return {}
    result = await db.execute(
        select(Team.id, Team.level_id, TeamLevel.name)
        .join(TeamLevel, TeamLevel.id == Team.level_id)
        .where(Team.id.in_(ids))
    )
    return {row[0]: (row[1], row[2]) for row in result.all()}


def _player_team_id(innings: Innings, card, player_id: int) -> int:
    """Which side the player turned out for in this innings."""
    if any(b.player_id == player_id for b in card.batsmen):
        # Named in the batting order: they play for the batting side, whether or
        # not they actually came in (a did_not_bat row still means selected).
        return innings.batting_team_id
    return innings.bowling_team_id


async def get_player_profile(db: AsyncSession, player, user_id: int) -> dict:
    """Build the career + per-competition payload for ``player``."""
    innings_list = await load_player_innings(db, player.id, user_id, completed_only=True)

    repo = ScoringRepository(db)
    engine = ScoreEngine(repo)

    # Read each scorecard and its deliveries once, then attribute the innings to
    # both the career total and its competition.
    loaded: list[tuple[Innings, object, list]] = []
    career = PlayerStatsAccumulator()
    for innings in innings_list:
        try:
            card = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            continue
        deliveries = await repo.list_deliveries(innings.id)
        loaded.append((innings, card, deliveries))
        career.add_innings(innings, card, player.id, deliveries)

    levels = await _team_levels(
        db,
        [innings.batting_team_id for innings, _, _ in loaded]
        + [innings.bowling_team_id for innings, _, _ in loaded],
    )

    by_competition: dict[int, PlayerStatsAccumulator] = {}
    level_names: dict[int, str] = {}
    for innings, card, deliveries in loaded:
        team_id = _player_team_id(innings, card, player.id)
        level_id, level_name = levels.get(team_id, (UNKNOWN_LEVEL_ID, UNKNOWN_LEVEL_NAME))
        level_names[level_id] = level_name
        by_competition.setdefault(level_id, PlayerStatsAccumulator()).add_innings(
            innings, card, player.id, deliveries
        )

    competitions = [
        {"level_id": level_id, "level_name": level_names[level_id], **stats.stats_payload()}
        for level_id, stats in sorted(
            by_competition.items(), key=lambda item: level_names[item[0]].lower()
        )
    ]
    return {"career": career.stats_payload(), "competitions": competitions}


__all__ = ["get_player_profile"]
