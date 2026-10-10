from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    paginated_list_response,
    set_pagination_headers,
)
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.tournaments.analytics import (
    build_leaderboards,
    build_points_table,
    fixture_public_rows,
)
from app.tournaments.crud import (
    add_team,
    create_fixture,
    create_tournament,
    delete_fixture,
    delete_tournament,
    generate_fixtures,
    generate_knockout,
    get_tournament,
    get_tournaments,
    list_tournament_teams,
    remove_team,
    set_fixture_result,
    tournament_response,
    update_fixture,
    update_tournament,
)
from app.tournaments.schemas import (
    FixtureCreate,
    FixtureGenerate,
    FixtureResponse,
    FixtureResult,
    FixtureUpdate,
    KnockoutGenerate,
    PointsTableResponse,
    TournamentCreate,
    TournamentLeaderboardResponse,
    TournamentResponse,
    TournamentTeamAdd,
    TournamentTeamList,
    TournamentTeamResponse,
    TournamentUpdate,
)

router = APIRouter(tags=["tournaments"])


async def _require_tournament(db: AsyncSession, tournament_id: int, user_id: int):
    tournament = await get_tournament(db, tournament_id, user_id)
    if tournament is None:
        raise HTTPException(404, "Tournament not found")
    return tournament


async def _fixture_rows(db: AsyncSession, tournament_id: int, ids: list[int]) -> list[dict]:
    rows = await fixture_public_rows(db, tournament_id)
    if ids is None:
        return rows
    wanted = set(ids)
    return [row for row in rows if row["id"] in wanted]


@router.get(
    "/tournaments",
    response_model=list[TournamentResponse],
    responses=paginated_list_response("One page of this account's tournaments."),
)
async def list_tournaments(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    rows, total = await get_tournaments(db, current_user.id, skip=skip, limit=limit)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return rows


@router.post(
    "/tournaments", response_model=TournamentResponse, status_code=201, responses={"409": CONFLICT}
)
async def create_tournament_endpoint(
    data: TournamentCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        tournament = await create_tournament(db, data, current_user.id)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A tournament with this name already exists in your account.")
    return await tournament_response(db, tournament)


@router.get("/tournaments/{tournament_id}", response_model=TournamentResponse)
async def get_tournament_endpoint(
    tournament_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    return await tournament_response(db, tournament)


@router.patch(
    "/tournaments/{tournament_id}",
    response_model=TournamentResponse,
    responses={"400": BAD_REQUEST, "409": CONFLICT},
)
async def update_tournament_endpoint(
    tournament_id: int,
    data: TournamentUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    try:
        tournament = await update_tournament(db, tournament, data)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A tournament with this name already exists in your account.")
    return await tournament_response(db, tournament)


@router.delete("/tournaments/{tournament_id}", status_code=204)
async def delete_tournament_endpoint(
    tournament_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    await delete_tournament(db, tournament)


@router.get(
    "/tournaments/{tournament_id}/teams",
    response_model=TournamentTeamList,
    responses={"404": NOT_FOUND},
)
async def list_tournament_teams_endpoint(
    tournament_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    teams = await list_tournament_teams(db, tournament.id)
    return TournamentTeamList(tournament_id=tournament.id, teams=teams)


@router.post(
    "/tournaments/{tournament_id}/teams",
    response_model=TournamentTeamResponse,
    status_code=201,
    responses={"400": BAD_REQUEST},
)
async def add_tournament_team_endpoint(
    tournament_id: int,
    data: TournamentTeamAdd,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    row, error = await add_team(db, tournament, data.team_id, current_user.id)
    if error:
        raise HTTPException(400, error)
    return row


@router.delete(
    "/tournaments/{tournament_id}/teams/{team_id}", status_code=204, responses={"400": BAD_REQUEST}
)
async def remove_tournament_team_endpoint(
    tournament_id: int,
    team_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    removed, error = await remove_team(db, tournament, team_id)
    if error:
        raise HTTPException(400, error)
    if not removed:
        raise HTTPException(404, "Team is not in this tournament")


@router.get(
    "/tournaments/{tournament_id}/fixtures",
    response_model=list[FixtureResponse],
    responses=paginated_list_response("One page of this tournament's fixtures."),
)
async def list_fixtures_endpoint(
    tournament_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    rows = await fixture_public_rows(db, tournament.id)
    total = len(rows)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return rows[skip : skip + limit]


@router.post(
    "/tournaments/{tournament_id}/fixtures",
    response_model=FixtureResponse,
    status_code=201,
    responses={"400": BAD_REQUEST},
)
async def create_fixture_endpoint(
    tournament_id: int,
    data: FixtureCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    fixture, error = await create_fixture(db, tournament, data)
    if error:
        raise HTTPException(400, error)
    rows = await _fixture_rows(db, tournament.id, [fixture.id])
    return rows[0]


@router.post(
    "/tournaments/{tournament_id}/fixtures/generate",
    response_model=list[FixtureResponse],
    responses={"400": BAD_REQUEST},
)
async def generate_fixtures_endpoint(
    tournament_id: int,
    data: FixtureGenerate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    _count, error = await generate_fixtures(db, tournament, data.legs, data.replace)
    if error:
        raise HTTPException(400, error)
    return await fixture_public_rows(db, tournament.id)


@router.post(
    "/tournaments/{tournament_id}/fixtures/knockout",
    response_model=list[FixtureResponse],
    responses={"400": BAD_REQUEST},
)
async def generate_knockout_endpoint(
    tournament_id: int,
    data: KnockoutGenerate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    created, error = await generate_knockout(db, tournament, data.teams)
    if error:
        raise HTTPException(400, error)
    return await _fixture_rows(db, tournament.id, [fixture.id for fixture in created])


@router.patch(
    "/tournaments/{tournament_id}/fixtures/{fixture_id}",
    response_model=FixtureResponse,
    responses={"400": BAD_REQUEST},
)
async def update_fixture_endpoint(
    tournament_id: int,
    fixture_id: int,
    data: FixtureUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    fixture, error = await update_fixture(db, tournament, fixture_id, data)
    if error:
        if error == "Fixture not found":
            raise HTTPException(404, error)
        raise HTTPException(400, error)
    rows = await _fixture_rows(db, tournament.id, [fixture.id])
    return rows[0]


@router.delete("/tournaments/{tournament_id}/fixtures/{fixture_id}", status_code=204)
async def delete_fixture_endpoint(
    tournament_id: int,
    fixture_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    deleted = await delete_fixture(db, tournament, fixture_id)
    if not deleted:
        raise HTTPException(404, "Fixture not found")


@router.post(
    "/tournaments/{tournament_id}/fixtures/{fixture_id}/result",
    response_model=FixtureResponse,
    responses={"400": BAD_REQUEST},
)
async def set_fixture_result_endpoint(
    tournament_id: int,
    fixture_id: int,
    data: FixtureResult,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    fixture, error = await set_fixture_result(
        db, tournament, fixture_id, data.match_id, current_user.id
    )
    if error:
        if error == "Fixture not found":
            raise HTTPException(404, error)
        raise HTTPException(400, error)
    rows = await _fixture_rows(db, tournament.id, [fixture.id])
    return rows[0]


@router.get(
    "/tournaments/{tournament_id}/points-table",
    response_model=PointsTableResponse,
    responses={"404": NOT_FOUND},
)
async def points_table_endpoint(
    tournament_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The league standings.

    Points follow the tournament's own win/tie/loss/no-result values. Net run
    rate is the first tie-break, with wins, then name, after it. Only fixtures
    whose linked match is completed count.
    """
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    table = await build_points_table(db, tournament)
    return PointsTableResponse(
        tournament_id=tournament.id,
        tournament_name=tournament.name,
        format=tournament.format,
        status=tournament.status,
        table=table,
    )


@router.get(
    "/tournaments/{tournament_id}/leaderboards",
    response_model=TournamentLeaderboardResponse,
    responses={"404": NOT_FOUND},
)
async def leaderboards_endpoint(
    tournament_id: int,
    top: int = Query(10, ge=1, le=50, description="How many players per leaderboard"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The tournament's leading run scorers and wicket takers.

    Aggregated across every completed fixture, so an entry is a tournament
    total. Super Over innings are excluded; only normal innings count.
    """
    tournament = await _require_tournament(db, tournament_id, current_user.id)
    data = await build_leaderboards(db, tournament, top=top)
    return TournamentLeaderboardResponse(
        tournament_id=tournament.id,
        tournament_name=tournament.name,
        top_run_scorers=data["top_run_scorers"],
        top_wicket_takers=data["top_wicket_takers"],
    )
