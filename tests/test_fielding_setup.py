"""Match-level wicketkeeper and fielding positions, end to end.

Drives the real FastAPI routes against the real database, so the wiring
between the Pydantic schemas, the CRUD validation and the HTTP responses is
covered exactly as the frontend consumes it. The nine behaviours under test
map to the feature's requirements:

1. a valid wicketkeeper from the fielding XI can be named;
2. re-naming the keeper updates the one row instead of duplicating it;
3. a player on the *batting* side is refused;
4. a player outside the playing XI is refused;
5. positions can be assigned, moved and swapped;
6. switching to innings 2 loads that innings' (empty) setup, not innings 1's;
7. a saved setup survives a reload from the database;
8. the existing scoring and dismissal workflow still works after a save;
9. the Field Setup panel is driven by the same payload on every layout
   (the diagram's coordinates come from the server, so a narrow screen draws
   the identical anchors - the assertion here is that the payload carries
   everything the panel needs, since this repo has no browser test runner).

The world seeds its own admin, teams and 11-player squads so the tests are
order-independent, and starts a real innings through ``start-innings`` so the
innings-scoped rules are exercised against a real innings row.
"""

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.fielding.models import FieldingAssignment, FieldingSetup
from app.levels.models import TeamLevel
from app.main import app
from app.matches.models import Match
from app.models import City, Country, State
from app.players.models import Player
from app.scoring.models import Delivery, Innings
from app.teams.models import PlayerTeamAssignment, Team

ADMIN_EMAIL = "fielding_setup_admin@test.com"
TEAM_A_NAME = "Fielding Alpha"
TEAM_B_NAME = "Fielding Beta"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def as_admin():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(scope="module")
def world(client):
    """An admin, two 11-player teams, and a started match (innings 1 live)."""

    async def setup():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == ADMIN_EMAIL))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=ADMIN_EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Fielding Admin",
                    role="admin",
                    is_active=True,
                    is_verified=True,
                )
                db.add(admin)
                await db.flush()

            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()

            def new_team(name, short):
                team = Team(
                    name=name,
                    short_name=short,
                    homeground="Fielding Ground",
                    founder="Founder",
                    founded_year=2020,
                    owner="Owner",
                    country_id=country.id,
                    state_id=state.id,
                    city_id=city.id,
                    level_id=level.id,
                    user_id=admin.id,
                )
                db.add(team)
                return team

            team_a = new_team(TEAM_A_NAME, "FLA")
            team_b = new_team(TEAM_B_NAME, "FLB")
            await db.flush()

            def make_squad(prefix):
                ids = []
                for i in range(11):
                    player = Player(
                        first_name=f"FL{prefix}",
                        last_name=f"Player{i}",
                        date_of_birth=date(2000, 1, 1),
                        gender="male",
                        profile_image=None,
                        batting_hand="right",
                        batting_position="middle",
                        bowling_hand="right",
                        bowling_type="medium",
                        country_id=country.id,
                        state_id=state.id,
                        city_id=city.id,
                        height=175.0,
                        weight=70.0,
                        country_code="+91",
                        mobile_number=9700000000 + int(f"{prefix}{i}", 36),
                        email=f"fielding_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            squad_a = make_squad("A")
            squad_b = make_squad("B")
            await db.flush()
            for squad, team in ((squad_a, team_a), (squad_b, team_b)):
                for player in squad:
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
            await db.flush()

            match = Match(
                match_date=date(2026, 6, 1),
                match_time="19:00",
                venue="Fielding Stadium",
                match_type="T20",
                result=None,
                team_a_id=team_a.id,
                team_b_id=team_b.id,
                toss_winner_id=team_a.id,
                toss_decision="bat",  # A bats first, so B fields in innings 1
                user_id=admin.id,
            )
            db.add(match)
            await db.commit()

            return {
                "admin": admin,
                "team_a_id": team_a.id,
                "team_b_id": team_b.id,
                # innings 1: A bats, B fields.
                "fielding_team_id": team_b.id,
                "batting_team_id": team_a.id,
                "fielding_squad": [p.id for p in squad_b],
                "batting_squad": [p.id for p in squad_a],
                "match_id": match.id,
            }

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def _start_innings(client, world, batting_order):
    return client.post(
        f"/v1/matches/{world['match_id']}/start-innings",
        json={"batting_order": batting_order},
    )


def _setup_url(world, innings_number=None):
    url = f"/v1/matches/{world['match_id']}/fielding-setup"
    if innings_number is not None:
        url += f"?innings_number={innings_number}"
    return url


@pytest.fixture(scope="module")
def started(client, world, as_admin):
    """Innings 1 opened with a real batting order, so saving is legal."""
    _use_admin(as_admin, world)
    order = world["batting_squad"][:2]
    resp = _start_innings(client, world, order)
    assert resp.status_code == 200, resp.text
    return world


# --- 1. a valid wicketkeeper -----------------------------------------------


def test_assign_valid_wicketkeeper(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    keeper = w["fielding_squad"][0]

    resp = client.put(
        _setup_url(w),
        json={"innings_number": 1, "wicketkeeper_player_id": keeper, "assignments": []},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fielding_team_id"] == w["fielding_team_id"]
    assert body["wicketkeeper_player_id"] == keeper
    assert body["innings_number"] == 1
    # The panel's inputs ride along on the same response.
    assert len(body["eligible_players"]) == 11
    # The full grouped catalog: every close, off-side, leg-side, boundary and
    # bowling/special position, not the short list the panel used to show.
    assert len(body["position_catalog"]) == 48
    assert all("anchor_x" in p and "anchor_y" in p for p in body["position_catalog"])
    assert all("group" in p for p in body["position_catalog"])
    # Every position the picker promises, under exactly one group - and the
    # five group headings the panel renders them under.
    by_key = {p["key"]: p for p in body["position_catalog"]}
    assert len(by_key) == len(body["position_catalog"]), "a position is offered twice"
    assert set(by_key) >= {
        # close fielders
        "slip",
        "first_slip",
        "second_slip",
        "third_slip",
        "fourth_slip",
        "leg_slip",
        "gully",
        "leg_gully",
        "short_leg",
        "forward_short_leg",
        "backward_short_leg",
        "silly_point",
        "silly_mid_off",
        "silly_mid_on",
        # off side
        "point",
        "backward_point",
        "deep_point",
        "square_point",
        "cover",
        "extra_cover",
        "deep_cover",
        "deep_extra_cover",
        "sweeper_cover",
        "mid_off",
        "wide_mid_off",
        "long_off",
        # leg side
        "mid_on",
        "wide_mid_on",
        "long_on",
        "midwicket",
        "deep_midwicket",
        "square_leg",
        "forward_square_leg",
        "backward_square_leg",
        "deep_square_leg",
        "deep_backward_square_leg",
        "fine_leg",
        "deep_fine_leg",
        "short_fine_leg",
        "long_leg",
        # boundary
        "third_man",
        "deep_third_man",
        # other
        "bowler",
        "wicket_keeper",
        "unassigned",
        "custom_position",
    }
    assert {p["group"] for p in body["position_catalog"]} == {
        "close",
        "off",
        "leg",
        "boundary",
        "special",
    }


def test_player_photos_ride_along_in_both_buckets(client, started, as_admin):
    """The XI and reserve lists carry each player's photo when one exists.

    The panel draws an avatar from ``profile_image`` and falls back to the
    player's initials when it is null, so the key has to be present on *both*
    collections: a missing field would render every name as a default avatar,
    and a reserve's photo would vanish if only the XI were annotated.
    """
    w = started
    _use_admin(as_admin, w)
    player_id = w["fielding_squad"][1]
    photo = f"/uploads/players/{player_id}.png"

    async def _set_photo(value):
        async with async_session() as db:
            player = (
                (await db.execute(select(Player).where(Player.id == player_id))).scalars().first()
            )
            assert player is not None
            player.profile_image = value
            await db.commit()

    asyncio.run(_set_photo(photo))
    try:
        body = client.get(_setup_url(w)).json()
        entry = next(p for p in body["eligible_players"] if p["player_id"] == player_id)
        assert entry["profile_image"] == photo
        # Every entry exposes the key whatever its value - the panel reads it
        # unconditionally, and a squad with no uploads at all still works.
        assert all("profile_image" in p for p in body["eligible_players"])
        assert all("profile_image" in p for p in body["eligible_substitutes"])
    finally:
        asyncio.run(_set_photo(None))


# --- 2. changing the keeper never duplicates --------------------------------


def test_change_keeper_updates_not_duplicates(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    first, second = w["fielding_squad"][0], w["fielding_squad"][1]

    r1 = client.put(
        _setup_url(w),
        json={"innings_number": 1, "wicketkeeper_player_id": first, "assignments": []},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.put(
        _setup_url(w),
        json={"innings_number": 1, "wicketkeeper_player_id": second, "assignments": []},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["wicketkeeper_player_id"] == second

    async def count():
        async with async_session() as db:
            setups = (
                (
                    await db.execute(
                        select(FieldingSetup).where(
                            FieldingSetup.match_id == w["match_id"],
                            FieldingSetup.innings_number == 1,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(setups)

    # Exactly one setup row for innings 1, holding the *new* keeper.
    assert asyncio.run(count()) == 1

    # And the stored row agrees with the response.
    reloaded = client.get(_setup_url(w, innings_number=1)).json()
    assert reloaded["wicketkeeper_player_id"] == second


# --- 3. batting-side players are refused ------------------------------------


def test_reject_batting_side_keeper(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    opposition = w["batting_squad"][0]  # A player, but A is batting

    resp = client.put(
        _setup_url(w),
        json={"innings_number": 1, "wicketkeeper_player_id": opposition, "assignments": []},
    )
    assert resp.status_code == 400
    assert "batting side" in resp.json()["detail"]

    # And the fielder route refuses them too.
    resp2 = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": None,
            "assignments": [{"player_id": opposition, "position_key": "slip"}],
        },
    )
    assert resp2.status_code == 400
    assert "batting side" in resp2.json()["detail"]


# --- 4. non-XI players are refused ------------------------------------------


def test_reject_non_xi_keeper(client, world, as_admin, started):
    w = world
    _use_admin(as_admin, w)

    # A real player id that exists but belongs to no team's XI here.
    async def stranger_id():
        async with async_session() as db:
            in_xi = select(PlayerTeamAssignment.player_id)
            p = (
                await db.execute(select(Player.id).where(Player.id.notin_(in_xi)).limit(1))
            ).scalar()
            if p is not None:
                return p
            # Every seeded player is in an XI; make a fresh non-XI player.
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()
            player = Player(
                first_name="Bench",
                last_name="Player",
                date_of_birth=date(1999, 1, 1),
                gender="male",
                profile_image=None,
                batting_hand="right",
                batting_position="middle",
                bowling_hand="right",
                bowling_type="medium",
                country_id=country.id,
                state_id=state.id,
                city_id=city.id,
                height=175.0,
                weight=70.0,
                country_code="+91",
                mobile_number=9500000001,
                email="fielding_bench_nonxi@test.com",
            )
            db.add(player)
            await db.commit()
            return player.id

    pid = asyncio.run(stranger_id())
    resp = client.put(
        _setup_url(w),
        json={"innings_number": 1, "wicketkeeper_player_id": pid, "assignments": []},
    )
    assert resp.status_code == 400
    assert "not in the fielding side's playing XI" in resp.json()["detail"]


# --- 5. positions can be assigned, moved and swapped ------------------------


def test_assign_and_change_positions(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    p1, p2, p3 = w["fielding_squad"][:3]
    keeper = p1

    # Assign three positions at once.
    resp = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [
                {"player_id": keeper, "position_key": "wicket_keeper"},
                {"player_id": p2, "position_key": "slip"},
                {"player_id": p3, "position_key": "cover"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {a["position_key"]: a["player_id"] for a in body["assignments"]} == {
        "wicket_keeper": keeper,
        "slip": p2,
        "cover": p3,
    }

    # Move p2 from slip to point, and swap p3 onto slip.
    resp2 = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [
                {"player_id": keeper, "position_key": "wicket_keeper"},
                {"player_id": p2, "position_key": "point"},
                {"player_id": p3, "position_key": "slip"},
            ],
        },
    )
    assert resp2.status_code == 200, resp2.text
    moved = {a["position_key"]: a["player_id"] for a in resp2.json()["assignments"]}
    assert moved["point"] == p2
    assert moved["slip"] == p3
    assert "cover" not in moved  # released when p3 moved away

    async def rows():
        async with async_session() as db:
            setup = (
                await db.execute(
                    select(FieldingSetup).where(
                        FieldingSetup.match_id == w["match_id"],
                        FieldingSetup.innings_number == 1,
                    )
                )
            ).scalar_one()
            return (
                (
                    await db.execute(
                        select(FieldingAssignment).where(FieldingAssignment.setup_id == setup.id)
                    )
                )
                .scalars()
                .all()
            )

    stored = asyncio.run(rows())
    # Three assignments, three distinct players, three distinct positions -
    # the swap created no duplicates.
    assert len(stored) == 3
    assert len({a.player_id for a in stored}) == 3
    assert len({a.position_key for a in stored}) == 3


def test_two_fielders_may_share_a_position_name(client, started, as_admin):
    """Two fielders can hold the same *named* area when their coords differ.

    A real cordon has a second and a third slip; a captain may want two men
    at point. Uniqueness by position name alone was the wrong rule, so the
    422 the schema used to return is now a 200 - with the two markers drawn
    at their own coordinates. What is still refused: the same player in two
    places (schema), and the wicket_keeper anchor belonging to somebody who
    is not the designated keeper (crud).
    """
    w = started
    _use_admin(as_admin, w)
    p1, p2 = w["fielding_squad"][:2]

    # Two fielders, one position name, two different drop points.
    resp = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [
                {"player_id": p1, "position_key": "wicket_keeper"},
                {"player_id": p2, "position_key": "slip", "anchor_x": 60.0, "anchor_y": 86.0},
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    # A second fielder on the same named position, dragged a few units away.
    third = w["fielding_squad"][2]
    resp2 = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [
                {"player_id": p1, "position_key": "wicket_keeper"},
                {"player_id": p2, "position_key": "slip", "anchor_x": 60.0, "anchor_y": 86.0},
                {"player_id": third, "position_key": "slip", "anchor_x": 66.0, "anchor_y": 84.0},
            ],
        },
    )
    assert resp2.status_code == 200, resp2.text
    slips = [a for a in resp2.json()["assignments"] if a["position_key"] == "slip"]
    assert {a["player_id"] for a in slips} == {p2, third}
    assert len({(a["anchor_x"], a["anchor_y"]) for a in slips}) == 2

    # The same player twice is still refused, before any write.
    dup = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [
                {"player_id": p2, "position_key": "slip"},
                {"player_id": p2, "position_key": "point"},
            ],
        },
    )
    assert dup.status_code == 422

    # More than eleven active players is refused too.
    too_many = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [
                {"player_id": pid, "position_key": "slip"} for pid in w["fielding_squad"]
            ]
            + [{"player_id": w["batting_squad"][0], "position_key": "point"}],
        },
    )
    assert too_many.status_code in (400, 422)

    # The wicket_keeper anchor may only belong to the named keeper.
    resp2 = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [{"player_id": p2, "position_key": "wicket_keeper"}],
        },
    )
    assert resp2.status_code == 400
    assert "wicketkeeper" in resp2.json()["detail"]

    # An unknown position key is refused.
    resp3 = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": p1,
            "assignments": [{"player_id": p2, "position_key": "cow_corner"}],
        },
    )
    assert resp3.status_code == 422


# --- 6. innings switching loads the right (empty) setup ---------------------


def test_innings_switch_loads_correct_setup(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    keeper = w["fielding_squad"][0]

    # Set up innings 1 (team B fields).
    client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [{"player_id": keeper, "position_key": "wicket_keeper"}],
        },
    )

    # Innings 2 has not been opened yet. The fielding side is still knowable
    # (whichever side batted first now fields), so this is a valid, empty
    # setup flagged as not-yet-started - not an error. Saving, though, is
    # refused until the innings exists (asserted below via the save route).
    resp_early = client.get(_setup_url(w, innings_number=2))
    assert resp_early.status_code == 200, resp_early.text
    early = resp_early.json()
    assert early["innings_started"] is False
    assert early["fielding_team_id"] == w["batting_team_id"]  # A now fields
    assert early["wicketkeeper_player_id"] is None
    assert early["assignments"] == []

    # Saving to a not-yet-started innings is refused.
    save_early = client.put(
        _setup_url(w),
        json={
            "innings_number": 2,
            "wicketkeeper_player_id": w["fielding_squad"][0],
            "assignments": [],
        },
    )
    assert save_early.status_code == 400
    assert "has not started" in save_early.json()["detail"]

    # Open innings 2 by finishing innings 1's only over? Instead, drive the
    # engine: score enough to end innings 1, which opens innings 2. Two legal
    # dots complete a 1-over innings; here we use the API's own second-innings
    # opener by recording a delivery that exhausts the over.
    #
    # Simpler and just as real: call open_second_innings the way the engine
    # does when innings 1 completes, via the scoring route for the chase.
    from app.scoring.crud import open_second_innings

    async def open_two():
        async with async_session() as db:
            match = (await db.execute(select(Match).where(Match.id == w["match_id"]))).scalar_one()
            return await open_second_innings(db, match, None)

    innings2 = asyncio.run(open_two())
    assert innings2.innings_number == 2
    # The sides swapped: A now fields.
    assert innings2.bowling_team_id == w["batting_team_id"]

    resp = client.get(_setup_url(w, innings_number=2))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fielding_team_id"] == w["batting_team_id"]
    assert body["wicketkeeper_player_id"] is None
    assert body["assignments"] == []  # innings 1's setup did NOT leak
    assert len(body["eligible_players"]) == 11

    # Innings 1's setup is still intact and still team B's.
    resp1 = client.get(_setup_url(w, innings_number=1))
    assert resp1.status_code == 200
    assert resp1.json()["wicketkeeper_player_id"] == keeper
    assert resp1.json()["fielding_team_id"] == w["fielding_team_id"]

    # Restore the shared match: opening innings 2 advanced
    # current_innings_number, and later tests read the *default* innings. Leave
    # the world as we found it so the suite stays order-independent.
    async def restore():
        async with async_session() as db:
            match = (await db.execute(select(Match).where(Match.id == w["match_id"]))).scalar_one()
            match.current_innings_number = 1
            await db.commit()

    asyncio.run(restore())


# --- 7. a saved setup survives a reload -------------------------------------


def test_saved_setup_persists_on_reload(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    keeper = w["fielding_squad"][4]
    fielder = w["fielding_squad"][5]

    client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [
                {"player_id": keeper, "position_key": "wicket_keeper"},
                {"player_id": fielder, "position_key": "mid_off"},
            ],
        },
    )

    # A brand-new read (as if the page were reloaded) returns the same data.
    fresh = client.get(_setup_url(w)).json()
    assert fresh["wicketkeeper_player_id"] == keeper
    by_pos = {a["position_key"]: a["player_id"] for a in fresh["assignments"]}
    assert by_pos == {"wicket_keeper": keeper, "mid_off": fielder}


# --- 8. scoring and dismissals still work after a save ----------------------


def test_scoring_still_works_after_fielding_save(client, started, as_admin):
    w = started
    _use_admin(as_admin, w)
    keeper = w["fielding_squad"][0]
    catcher = w["fielding_squad"][1]

    save = client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [
                {"player_id": keeper, "position_key": "wicket_keeper"},
                {"player_id": catcher, "position_key": "slip"},
            ],
        },
    )
    assert save.status_code == 200, save.text

    # Now record a delivery with a catch dismissal, credited to our slip.
    striker, non_striker = w["batting_squad"][0], w["batting_squad"][1]
    bowler = w["fielding_squad"][2]
    delivery = client.post(
        f"/v1/matches/{w['match_id']}/deliveries",
        json={
            "striker_id": striker,
            "non_striker_id": non_striker,
            "bowler_id": bowler,
            "runs_batsman": 0,
            "runs_extras": 0,
            "extra_type": "none",
            "wicket_type": "caught",
            "dismissed_player_id": striker,
            "fielder_id": catcher,
        },
    )
    assert delivery.status_code == 201, delivery.text
    card = delivery.json()
    assert card["wickets"] >= 1
    assert card["total"] == 0

    # The dismissal is attributed to our slip, and the fielding setup is
    # untouched by scoring.
    async def check():
        async with async_session() as db:
            d = (
                (await db.execute(select(Delivery).order_by(Delivery.id.desc()).limit(1)))
                .scalars()
                .first()
            )
            setup = (
                await db.execute(
                    select(FieldingSetup).where(
                        FieldingSetup.match_id == w["match_id"],
                        FieldingSetup.innings_number == 1,
                    )
                )
            ).scalar_one()
            return d.fielder_id, setup.wicketkeeper_player_id

    fielder_id, stored_keeper = asyncio.run(check())
    assert fielder_id == catcher
    assert stored_keeper == keeper  # scoring never touched the setup


# --- 9. the panel payload is complete for every layout ----------------------


def test_panel_payload_carries_everything_the_ui_needs(client, started, as_admin):
    """The diagram and controls are drawn from this one payload.

    Because the anchors and the eligible XI come from the server, a narrow
    (mobile) layout and a wide (desktop) one render the identical data - the
    only difference is CSS. This asserts the payload is complete: catalog with
    coordinates, eligible XI with captaincy marks, and current assignments
    with their stored anchors.
    """
    w = started
    _use_admin(as_admin, w)
    keeper = w["fielding_squad"][3]

    client.put(
        _setup_url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": keeper,
            "assignments": [{"player_id": keeper, "position_key": "wicket_keeper"}],
        },
    )
    body = client.get(_setup_url(w)).json()

    # 1. every catalog position has a label and in-range coordinates.
    for p in body["position_catalog"]:
        assert p["label"]
        assert 0 <= p["anchor_x"] <= 100
        assert 0 <= p["anchor_y"] <= 100
    assert {p["key"] for p in body["position_catalog"]} >= {
        "slip",
        "gully",
        "point",
        "backward_point",
        "cover",
        "extra_cover",
        "mid_off",
        "mid_on",
        "midwicket",
        "square_leg",
        "fine_leg",
        "third_man",
        "long_off",
        "long_on",
        "deep_cover",
        "deep_square_leg",
        "wicket_keeper",
    }

    # 2. the eligible XI is exactly the fielding side's playing XI.
    assert {p["player_id"] for p in body["eligible_players"]} == set(w["fielding_squad"])

    # 3. stored assignments carry the anchor the diagram draws them at.
    assert body["assignments"][0]["anchor_x"] == 50.0
    assert body["assignments"][0]["anchor_y"] == 88.0

    # 4. the fielding side and its batting counterpart are both named.
    assert body["fielding_team_id"] == w["fielding_team_id"]
    assert body["batting_team_id"] == w["batting_team_id"]


# --- permissions and lifecycle ----------------------------------------------


def test_write_requires_admin(client, started):
    """An owner who is not an admin can read the setup but cannot write it.

    The write route is admin-gated on top of ownership scoping. A non-admin
    who does not even own the match gets 404 (scoping, so ids cannot be
    probed); here the user owns the match, so the 403 admin gate is what
    answers - and the read, which is not admin-gated, still succeeds.
    """
    w = started

    async def make_owner():
        async with async_session() as db:
            owner = User(
                email="fielding_scorer@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Fielding Scorer",
                role="user",
                is_active=True,
                is_verified=True,
            )
            db.add(owner)
            await db.flush()

            # A match this non-admin owns, with innings 1 seeded directly (the
            # start-innings route is admin-gated, so we cannot use it here).
            match = Match(
                match_date=date(2026, 6, 2),
                match_time="19:00",
                venue="Fielding Ground",
                match_type="T20",
                result=None,
                team_a_id=w["team_a_id"],
                team_b_id=w["team_b_id"],
                toss_winner_id=w["team_a_id"],
                toss_decision="bat",
                user_id=owner.id,
            )
            db.add(match)
            await db.flush()
            db.add(
                Innings(
                    match_id=match.id,
                    batting_team_id=w["team_a_id"],
                    bowling_team_id=w["team_b_id"],
                    innings_number=1,
                )
            )
            await db.commit()
            return owner, match.id

    scorer, owner_match_id = asyncio.run(make_owner())
    url = f"/v1/matches/{owner_match_id}/fielding-setup"
    app.dependency_overrides[get_current_user] = lambda: scorer
    try:
        # Read is allowed for the owner regardless of role.
        read = client.get(url)
        assert read.status_code == 200, read.text
        # Write is refused: not an admin.
        write = client.put(
            url,
            json={"innings_number": 1, "wicketkeeper_player_id": None, "assignments": []},
        )
        assert write.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_defaults_to_current_innings(client, started, as_admin):
    """Omitting innings_number reads the match's current innings."""
    w = started
    _use_admin(as_admin, w)
    body = client.get(_setup_url(w)).json()
    assert body["innings_number"] == 1
    assert body["fielding_team_id"] == w["fielding_team_id"]


def test_match_of_another_owner_is_404(client, world):
    """Scoping: another account cannot read or write this match's setup."""

    async def make_owner():
        async with async_session() as db:
            owner = User(
                email="fielding_other_owner@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Other Owner",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add(owner)
            await db.commit()
            return owner

    other = asyncio.run(make_owner())
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        assert client.get(_setup_url(world)).status_code == 404
        assert (
            client.put(
                _setup_url(world),
                json={"innings_number": 1, "wicketkeeper_player_id": None, "assignments": []},
            ).status_code
            == 404
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
