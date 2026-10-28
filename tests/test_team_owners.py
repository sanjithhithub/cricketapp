"""The team owners: the register, the stakes, and the photo.

Ownership was a free-text name in a column on the team. This file tests
everything that replaced it:

1. an owner is registered with every detail the register shows;
2. the register can be searched, filtered, sorted and paged;
3. the rules that keep the data honest - one live stake per owner and team,
   the live percentages of a team never over a hundred, the dates in order,
   the organisation details demanded of a company but not of a person;
4. ending a stake keeps the history rather than deleting it, and an owner
   who still holds a team cannot be deleted out from under it;
5. the photo is stored, replaced and removed without leaving files behind;
6. the permissions - an administrator of the account writes, another
   account's owner is missing rather than forbidden, and the internal notes
   stay internal;
7. the Google sign-in is matched on the Google account's own identifier, so
   an account cannot be taken over by changing an address;
8. the teams, the players and the matches still work afterwards.
"""

import asyncio
import io
import itertools
import struct
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.crud import login_or_create_google_user
from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_EMAIL = "owner_admin@test.com"
OTHER_ADMIN_EMAIL = "owner_admin_b@test.com"


class FakeS3:
    """In-memory stand-in for boto3 so the suite needs no AWS credentials."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        from botocore.exceptions import ClientError

        objects = self.objects
        if kwargs["Key"] not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Resp:
            def read(self):
                return objects[kwargs["Key"]]

        return {"Body": _Resp()}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs["Key"])
        self.objects.pop(kwargs["Key"], None)


@pytest.fixture(autouse=True)
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr("app.storage.get_client", lambda: fake)
    return fake


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def refs(client):
    """Seed ids needed to satisfy the required address fields."""

    async def fetch():
        async with async_session() as db:
            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()
            return level.id, country.id, state.id, city.id

    return asyncio.run(fetch())


def _make_admin(email, name, role="admin"):
    async def setup():
        async with async_session() as db:
            user = User(
                email=email,
                hashed_password=hash_password("Test@1234"),
                full_name=name,
                role=role,
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return asyncio.run(setup())


@pytest.fixture(scope="module")
def admin():
    return _make_admin(ADMIN_EMAIL, "Owner Admin")


@pytest.fixture(scope="module")
def other_admin():
    return _make_admin(OTHER_ADMIN_EMAIL, "Other Owner Admin")


@pytest.fixture(scope="module")
def viewer():
    """An account that is not an administrator of the register."""
    return _make_admin("owner_viewer@test.com", "Owner Viewer", role="viewer")


@pytest.fixture
def as_user(admin):
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


def png_bytes():
    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def jpeg_bytes():
    """A file the sniffer recognises as a JPEG by its own signature."""
    return b"\xff\xd8\xff\xe0" + b"0" * 64


# Every owner the tests register needs an address of its own: the email is
# unique within an account, and the tests all register under one account.
_EMAIL_SEQUENCE = itertools.count(1)
_TEAM_SEQUENCE = itertools.count(1)


def _owner_payload(refs, **overrides):
    """A complete, valid owner create payload."""
    _, country_id, _, _ = refs
    payload = {
        "first_name": "Anil",
        "last_name": "Sharma",
        "date_of_birth": "1975-06-02",
        "gender": "male",
        "country_id": country_id,
        "profile_image": None,
        "email": "anil.sharma@test.com",
        "country_code": "+91",
        "mobile_number": "9812345670",
        "ownership_type": "individual_owner",
        "ownership_status": "active",
    }
    payload.update(overrides)
    return payload


def _create_owner(client, refs, **overrides):
    """Register an owner, with an email address of its own."""
    payload = _owner_payload(refs, **overrides)
    if "email" not in overrides:
        payload["email"] = f"owner{next(_EMAIL_SEQUENCE)}@test.com"
    resp = client.post("/v1/team-owners", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _team(client, refs, name=None):
    level_id, country_id, state_id, city_id = refs
    name = name or f"Owners FC {next(_TEAM_SEQUENCE)}"
    resp = client.post(
        "/v1/teams",
        json={
            "name": name,
            "short_name": name[:10],
            "level_id": level_id,
            "homeground": "Home",
            "founder": "Founder",
            "founded_year": 2001,
            "owner": "Owner",
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- 1. registering an owner ---------------------------------------------------


def test_creating_an_owner_records_every_detail_the_register_shows(client, refs, admin, as_user):
    as_user(admin)
    body = _create_owner(client, refs, email="anil.sharma@test.com")

    assert body["first_name"] == "Anil"
    assert body["last_name"] == "Sharma"
    assert body["full_name"] == "Anil Sharma"
    assert body["date_of_birth"] == "1975-06-02"
    assert body["gender"] == "male"
    assert body["nationality"]
    assert body["email"] == "anil.sharma@test.com"
    assert body["mobile_number"] == "9812345670"
    assert body["country_code"] == "+91"
    assert body["ownership_type"] == "individual_owner"
    assert body["ownership_status"] == "active"
    assert body["created_at"]
    assert body["updated_at"]


def test_an_organisation_ownership_records_the_organisation(client, refs, admin, as_user):
    as_user(admin)
    body = _create_owner(
        client,
        refs,
        ownership_type="company_organization",
        organization_name="Sharma Cricket Pvt Ltd",
        registration_number="REG-2026-114",
    )
    assert body["organization_name"] == "Sharma Cricket Pvt Ltd"
    assert body["registration_number"] == "REG-2026-114"


def test_a_company_ownership_without_the_organisation_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post(
        "/v1/team-owners",
        json=_owner_payload(refs, ownership_type="company_organization"),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert any("organization_name" in str(error.get("loc")) for error in body["detail"])


def test_a_franchise_ownership_without_a_registration_number_is_refused(
    client, refs, admin, as_user
):
    as_user(admin)
    resp = client.post(
        "/v1/team-owners",
        json=_owner_payload(
            refs, ownership_type="franchise_owner", organization_name="South Franchise"
        ),
    )
    assert resp.status_code == 422


def test_a_person_owning_in_their_own_name_needs_no_organisation(client, refs, admin, as_user):
    as_user(admin)
    body = _create_owner(client, refs, ownership_type="individual_owner")
    assert body["organization_name"] is None
    assert body["registration_number"] is None


def test_the_ownership_type_spellings_a_client_actually_sends_are_accepted(
    client, refs, admin, as_user
):
    """A club's paperwork says "Company", not "company_organization"."""
    as_user(admin)
    body = _create_owner(client, refs, ownership_type="Co-Owner")
    assert body["ownership_type"] == "co_owner"

    body = _create_owner(
        client,
        refs,
        ownership_type="Franchise Owner",
        organization_name="South Franchise",
        registration_number="FR-2026-07",
    )
    assert body["ownership_type"] == "franchise_owner"


def test_an_unknown_ownership_type_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, ownership_type="landlord"))
    assert resp.status_code == 422


def test_an_unknown_gender_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, gender="unknown"))
    assert resp.status_code == 422


def test_prefer_not_to_say_is_a_gender_the_record_can_hold(client, refs, admin, as_user):
    """The players' gender column is ten characters; this one is longer."""
    as_user(admin)
    body = _create_owner(client, refs, gender="prefer not to say")
    assert body["gender"] == "prefer_not_to_say"


def test_a_future_date_of_birth_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, date_of_birth="2099-01-01"))
    assert resp.status_code == 422


def test_the_required_fields_are_required(client, refs, admin, as_user):
    as_user(admin)
    payload = _owner_payload(refs)
    for field in ("first_name", "last_name", "date_of_birth", "gender", "country_id", "email"):
        body = dict(payload)
        body.pop(field)
        resp = client.post("/v1/team-owners", json=body)
        assert resp.status_code == 422, field
        assert any(field in str(error.get("loc")) for error in resp.json()["detail"]), field


def test_an_invalid_email_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, email="not-an-address"))
    assert resp.status_code == 422


def test_the_email_is_normalised_before_it_is_stored(client, refs, admin, as_user):
    as_user(admin)
    body = _create_owner(client, refs, email="Mixed.Case@Test.COM")
    assert body["email"] == "mixed.case@test.com"


def test_a_duplicate_email_is_refused_however_it_is_spelled(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, email="duplicate@test.com")
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, email="Duplicate@Test.com"))
    assert resp.status_code == 409


def test_a_phone_number_with_letters_in_it_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs, mobile_number="98123abc4567"))
    assert resp.status_code == 422


def test_a_number_too_long_for_its_country_code_is_refused(client, refs, admin, as_user):
    """E.164 allows fifteen digits in total, country code included."""
    as_user(admin)
    resp = client.post(
        "/v1/team-owners",
        json=_owner_payload(refs, country_code="+91", mobile_number="9" * 14),
    )
    assert resp.status_code == 422


def test_a_number_that_dials_from_another_country_is_accepted(client, refs, admin, as_user):
    """An owner may be from one country and reachable on another's code."""
    as_user(admin)
    body = _create_owner(client, refs, country_code="+44", mobile_number="7700900123")
    assert body["mobile_number"] == "7700900123"
    assert body["country_code"] == "+44"


def test_a_percentage_outside_the_range_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    resp = client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 120},
    )
    assert resp.status_code == 422


def test_an_end_date_before_the_start_date_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    resp = client.post(
        f"/v1/teams/{team}/owners",
        json={
            "owner_id": owner["id"],
            "ownership_start_date": "2026-01-01",
            "ownership_end_date": "2025-01-01",
        },
    )
    assert resp.status_code == 422


# --- 2. the register -----------------------------------------------------------


def test_the_register_can_be_searched(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, first_name="Rohan", last_name="Kapoor")
    resp = client.get("/v1/team-owners", params={"q": "Kapoor"})
    assert resp.status_code == 200
    names = [row["full_name"] for row in resp.json()]
    assert "Rohan Kapoor" in names


def test_the_register_can_be_searched_by_email(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, email="findme@test.com")
    resp = client.get("/v1/team-owners", params={"q": "findme@test.com"})
    assert resp.status_code == 200
    assert any(row["email"] == "findme@test.com" for row in resp.json())


def test_the_register_is_filtered_by_ownership_type(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, ownership_type="individual_owner")
    _create_owner(client, refs, ownership_type="co_owner")
    resp = client.get("/v1/team-owners", params={"ownership_type": "co_owner"})
    assert resp.status_code == 200
    assert resp.json()
    assert all(row["ownership_type"] == "co_owner" for row in resp.json())


def test_the_register_is_filtered_by_ownership_status(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, ownership_status="active")
    _create_owner(client, refs, ownership_status="inactive")
    resp = client.get("/v1/team-owners", params={"ownership_status": "inactive"})
    assert resp.status_code == 200
    assert resp.json()
    assert all(row["ownership_status"] == "inactive" for row in resp.json())


def test_the_register_is_filtered_by_gender(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, gender="female")
    resp = client.get("/v1/team-owners", params={"gender": "female"})
    assert resp.status_code == 200
    assert resp.json()
    assert all(row["gender"] == "female" for row in resp.json())


def test_the_register_is_filtered_by_nationality(client, refs, admin, as_user):
    as_user(admin)
    _, country_id, _, _ = refs
    _create_owner(client, refs, country_id=country_id)
    resp = client.get("/v1/team-owners", params={"nationality": str(country_id)})
    assert resp.status_code == 200
    assert all(row["country_id"] == country_id for row in resp.json())


def test_the_register_is_filtered_by_team(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    _create_owner(client, refs)
    client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 100},
    )
    resp = client.get("/v1/team-owners", params={"team_id": team})
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert owner["id"] in ids
    assert len(ids) == 1


def test_the_register_is_sorted(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, first_name="Zara", last_name="Young")
    _create_owner(client, refs, first_name="Adam", last_name="Old")
    resp = client.get("/v1/team-owners", params={"sort_by": "first_name", "sort_order": "asc"})
    assert resp.status_code == 200
    names = [row["first_name"] for row in resp.json()]
    assert names == sorted(names)


def test_the_register_pages_and_says_how_many_rows_there_are(client, refs, admin, as_user):
    as_user(admin)
    for _ in range(3):
        _create_owner(client, refs)
    resp = client.get("/v1/team-owners", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert int(resp.headers["X-Total-Count"]) >= 3
    assert resp.headers["X-Page"] == "1"
    assert resp.headers["X-Page-Size"] == "2"
    assert int(resp.headers["X-Total-Pages"]) >= 2


def test_the_register_counts_the_owners_and_the_active_ones(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs, ownership_status="active")
    _create_owner(client, refs, ownership_status="inactive")
    resp = client.get("/v1/team-owners/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    assert body["active"] >= 1
    assert body["inactive"] >= 1


def test_the_owner_picker_returns_only_what_a_picker_needs(client, refs, admin, as_user):
    as_user(admin)
    _create_owner(client, refs)
    resp = client.get("/v1/team-owners/options")
    assert resp.status_code == 200
    assert resp.json()
    row = resp.json()[0]
    assert "full_name" in row
    assert "email" not in row
    assert "mobile_number" not in row


def test_another_accounts_owner_is_missing_rather_than_forbidden(
    client, refs, admin, other_admin, as_user
):
    """A 403 would confirm that the id exists."""
    as_user(admin)
    owner = _create_owner(client, refs)

    as_user(other_admin)
    resp = client.get(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 404


def test_only_an_administrator_may_register_an_owner(client, refs, viewer, as_user):
    as_user(viewer)
    resp = client.post("/v1/team-owners", json=_owner_payload(refs))
    assert resp.status_code == 403


def test_the_internal_notes_stay_internal(client, refs, admin, viewer, as_user):
    as_user(admin)
    owner = _create_owner(client, refs, notes="Pays late, every time.")

    # The owner that registered them may read the note.
    as_user(admin)
    resp = client.get(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 200
    assert resp.json()["notes"] == "Pays late, every time."

    # A list hides it, even from the administrator who wrote it.
    resp = client.get("/v1/team-owners")
    listed = [row for row in resp.json() if row["id"] == owner["id"]]
    assert listed
    assert listed[0]["notes"] is None


# --- 3. the stake book ---------------------------------------------------------


def test_an_owner_takes_on_a_team(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    resp = client.post(
        f"/v1/teams/{team}/owners",
        json={
            "owner_id": owner["id"],
            "ownership_type": "co_owner",
            "ownership_percentage": 40,
            "ownership_start_date": "2024-01-01",
        },
    )
    assert resp.status_code == 201, resp.text
    stake = resp.json()
    assert stake["owner_id"] == owner["id"]
    assert stake["team_id"] == team
    assert stake["ownership_type"] == "co_owner"
    assert stake["ownership_percentage"] == 40
    assert stake["ownership_status"] == "active"


def test_a_stake_takes_the_owners_own_type_when_it_is_not_stated(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(
        client,
        refs,
        ownership_type="franchise_owner",
        organization_name="South Franchise",
        registration_number="FR-2026-07",
    )
    resp = client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["ownership_type"] == "franchise_owner"


def test_the_owners_stakes_are_kept_with_the_owner(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 60},
    )
    resp = client.get(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["ownerships"]) == 1
    assert body["ownerships"][0]["team_id"] == team
    assert body["ownerships"][0]["ownership_percentage"] == 60
    assert [t["id"] for t in body["teams"]] == [team]


def test_one_owner_cannot_hold_the_same_team_twice_at_once(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    first = client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    assert first.status_code == 201
    second = client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    assert second.status_code == 409


def test_the_live_percentages_of_a_team_never_add_up_to_more_than_a_hundred(
    client, refs, admin, as_user
):
    as_user(admin)
    team = _team(client, refs)
    first = _create_owner(client, refs)
    second = _create_owner(client, refs)
    assert (
        client.post(
            f"/v1/teams/{team}/owners",
            json={"owner_id": first["id"], "ownership_percentage": 60},
        ).status_code
        == 201
    )
    resp = client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": second["id"], "ownership_percentage": 50},
    )
    assert resp.status_code == 409
    assert "100" in resp.json()["detail"]


def test_the_percentages_are_not_required_to_add_up_to_a_hundred(client, refs, admin, as_user):
    """A club that does not split its equity does not have to pretend to."""
    as_user(admin)
    team = _team(client, refs)
    first = _create_owner(client, refs)
    second = _create_owner(client, refs)
    assert (
        client.post(
            f"/v1/teams/{team}/owners",
            json={"owner_id": first["id"], "ownership_percentage": 30},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/v1/teams/{team}/owners",
            json={"owner_id": second["id"], "ownership_percentage": 25},
        ).status_code
        == 201
    )


def test_ending_a_stake_keeps_the_history(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 100},
    )

    resp = client.delete(f"/v1/teams/{team}/owners/{owner['id']}")
    assert resp.status_code == 204, resp.text

    # The row is still there, marked as ended and dated.
    resp = client.get(f"/v1/teams/{team}/owners")
    assert resp.status_code == 200
    stakes = resp.json()
    assert len(stakes) == 1
    assert stakes[0]["ownership_status"] == "inactive"
    assert stakes[0]["ownership_end_date"]


def test_an_owner_may_take_a_team_again_after_ending_the_stake(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    client.delete(f"/v1/teams/{team}/owners/{owner['id']}")
    again = client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    assert again.status_code == 201, again.text


def test_the_terms_of_a_stake_can_be_changed(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 40},
    )
    resp = client.patch(
        f"/v1/teams/{team}/owners/{owner['id']}",
        json={"ownership_percentage": 55, "ownership_type": "co_owner"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ownership_percentage"] == 55
    assert resp.json()["ownership_type"] == "co_owner"


def test_a_stake_on_a_team_that_does_not_exist_is_refused(client, refs, admin, as_user):
    as_user(admin)
    owner = _create_owner(client, refs)
    resp = client.post("/v1/teams/99999999/owners", json={"owner_id": owner["id"]})
    assert resp.status_code == 404


def test_a_stake_by_an_owner_that_does_not_exist_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    resp = client.post(f"/v1/teams/{team}/owners", json={"owner_id": 99999999})
    assert resp.status_code == 404


def test_another_accounts_team_is_not_reachable(client, refs, admin, other_admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)

    as_user(other_admin)
    resp = client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    assert resp.status_code == 404


def test_the_team_panel_shows_its_owners(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(
        f"/v1/teams/{team}/owners",
        json={"owner_id": owner["id"], "ownership_percentage": 100},
    )
    resp = client.get(f"/v1/teams/{team}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["owners"]) == 1
    assert body["owners"][0]["full_name"] == owner["full_name"]
    assert body["owners"][0]["ownership_percentage"] == 100
    assert body["owners"][0]["ownership_status"] == "active"


def test_the_team_panel_does_not_carry_the_owners_contact_details(client, refs, admin, as_user):
    """The owner record lives once, in the register."""
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    resp = client.get(f"/v1/teams/{team}")
    assert resp.status_code == 200
    panel = resp.json()["owners"][0]
    assert "email" not in panel
    assert "mobile_number" not in panel
    assert "notes" not in panel


# --- 4. deleting ---------------------------------------------------------------


def test_an_owner_who_holds_nothing_can_be_deleted(client, refs, admin, as_user):
    as_user(admin)
    owner = _create_owner(client, refs)
    resp = client.delete(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 204, resp.text
    assert client.get(f"/v1/team-owners/{owner['id']}").status_code == 404


def test_an_owner_who_holds_a_team_cannot_be_deleted(client, refs, admin, as_user):
    """Deleting the row would take the club's history with it."""
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})

    resp = client.delete(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 409


def test_the_delete_check_says_what_would_happen(client, refs, admin, as_user):
    as_user(admin)
    owner = _create_owner(client, refs)
    resp = client.get(f"/v1/team-owners/{owner['id']}/delete-check")
    assert resp.status_code == 200
    assert resp.json()["can_delete"] is True

    team = _team(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    resp = client.get(f"/v1/team-owners/{owner['id']}/delete-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_delete"] is False
    assert body["active_ownerships"] == 1
    assert body["reason"]


def test_deleting_an_owner_leaves_the_team_the_players_and_the_matches_alone(
    client, refs, admin, as_user
):
    """The delete must not cascade into the club."""
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})
    client.delete(f"/v1/teams/{team}/owners/{owner['id']}")

    # The stake is ended, the owner is still on the register, and the team is
    # exactly where it was.
    resp = client.get(f"/v1/team-owners/{owner['id']}")
    assert resp.status_code == 200
    assert client.get(f"/v1/teams/{team}").status_code == 200


def test_deleting_a_team_leaves_the_owner_alone(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    owner = _create_owner(client, refs)
    client.post(f"/v1/teams/{team}/owners", json={"owner_id": owner["id"]})

    resp = client.delete(f"/v1/teams/{team}")
    assert resp.status_code == 204, resp.text
    assert client.get(f"/v1/team-owners/{owner['id']}").status_code == 200


# --- 5. the photo --------------------------------------------------------------


def test_a_photo_is_stored_against_the_owner(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    owner = _create_owner(client, refs)
    resp = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("photo.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()["profile_image"]
    assert path.startswith("/uploads/team_owners/")
    assert any(key.startswith("team_owners/") for key in fake_s3.objects)

    # The record points at it.
    resp = client.get(f"/v1/team-owners/{owner['id']}")
    assert resp.json()["profile_image"] == path


def test_a_replaced_photo_does_not_linger_in_the_bucket(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    owner = _create_owner(client, refs)
    first = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("photo.png", io.BytesIO(png_bytes()), "image/png")},
    )
    first_path = first.json()["profile_image"]

    # A different file type gets a different key, so the old file has to be
    # deleted explicitly rather than being overwritten in place.
    second = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("photo2.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
    )
    assert second.status_code == 200
    second_path = second.json()["profile_image"]
    assert second_path != first_path
    # The file that was replaced is gone.
    assert first_path[len("/uploads/") :] in fake_s3.deleted

    # The same type is written over the same key, so the bucket holds one file
    # either way - never two.
    third = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("photo3.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
    )
    assert third.status_code == 200
    assert third.json()["profile_image"] == second_path
    stored = [key for key in fake_s3.objects if key.startswith("team_owners/")]
    assert len(stored) == 1


def test_a_photo_can_be_removed(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    owner = _create_owner(client, refs)
    client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("photo.png", io.BytesIO(png_bytes()), "image/png")},
    )
    resp = client.delete(f"/v1/team-owners/{owner['id']}/profile-photo")
    assert resp.status_code == 204, resp.text
    assert fake_s3.deleted
    assert client.get(f"/v1/team-owners/{owner['id']}").json()["profile_image"] is None


def test_a_file_that_is_not_an_image_is_refused(client, refs, admin, as_user):
    as_user(admin)
    owner = _create_owner(client, refs)
    resp = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("notes.txt", io.BytesIO(b"just some text"), "text/plain")},
    )
    assert resp.status_code == 400


def test_an_oversized_photo_is_refused(client, refs, admin, as_user):
    as_user(admin)
    owner = _create_owner(client, refs)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
    resp = client.post(
        f"/v1/team-owners/{owner['id']}/profile-photo",
        files={"file": ("big.png", io.BytesIO(big), "image/png")},
    )
    assert resp.status_code == 400


def test_only_an_administrator_may_upload_a_photo(client, refs, viewer, as_user):
    as_user(viewer)
    resp = client.post(
        "/v1/team-owners/1/profile-photo",
        files={"file": ("photo.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert resp.status_code in (403, 404)


# --- 6. the profile completion -------------------------------------------------


def test_an_owner_may_complete_their_own_profile_and_nobody_elses(client, refs, admin, as_user):
    """The profile-completion form after a Google sign-in."""
    as_user(admin)
    owner = _create_owner(client, refs)

    # Link the record to the account that signed in.
    async def link():
        async with async_session() as db:
            row = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
            from app.team_owners.models import TeamOwner

            record = (
                await db.execute(select(TeamOwner).where(TeamOwner.id == owner["id"]))
            ).scalar_one()
            record.linked_user_id = row.id
            await db.commit()

    asyncio.run(link())

    resp = client.get("/v1/team-owners/me")
    assert resp.status_code == 200
    assert resp.json()["id"] == owner["id"]

    resp = client.patch(
        f"/v1/team-owners/{owner['id']}",
        json={"organization_name": "Sharma Cricket Pvt Ltd"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["organization_name"] == "Sharma Cricket Pvt Ltd"


def test_an_account_with_no_owner_profile_is_told_so(client, refs, other_admin, as_user):
    as_user(other_admin)
    resp = client.get("/v1/team-owners/me")
    assert resp.status_code == 404


@pytest.fixture(scope="module")
def self_owner():
    """An account with no owner profile, used only by the completion tests."""
    return _make_admin("owner_self@test.com", "Self Owner", role="viewer")


@pytest.fixture(scope="module")
def self_team_owner():
    """An account that completes a profile that takes on one of its teams."""
    return _make_admin("owner_self_team@test.com", "Self Team Owner")


@pytest.fixture(scope="module")
def self_other():
    """A second account, to prove the link is not visible outside its own."""
    return _make_admin("owner_self_other@test.com", "Self Other")


def test_an_account_can_create_its_own_owner_profile(client, refs, self_owner, as_user):
    as_user(self_owner)
    payload = _owner_payload(refs, email="self.profile@test.com", mobile_number="9811100011")
    resp = client.post("/v1/team-owners/me", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "self.profile@test.com"
    assert body["message"]

    # The link is read back through /me, not taken from the request body.
    me = client.get("/v1/team-owners/me")
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]

    # A second profile is refused rather than duplicated.
    again = client.post(
        "/v1/team-owners/me",
        json=_owner_payload(refs, email="self.profile2@test.com"),
    )
    assert again.status_code == 409


def test_another_account_cannot_see_a_self_service_profile(
    client, refs, self_owner, self_other, as_user
):
    as_user(self_owner)
    mine = client.get("/v1/team-owners/me").json()

    as_user(self_other)
    resp = client.get(f"/v1/team-owners/{mine['id']}")
    assert resp.status_code == 404


def test_a_self_service_profile_can_take_on_the_accounts_teams(
    client, refs, self_team_owner, as_user
):
    as_user(self_team_owner)
    team_id = _team(client, refs, name="Self Service FC")
    resp = client.post(
        "/v1/team-owners/me",
        json=_owner_payload(
            refs,
            email="self.team@test.com",
            mobile_number="9811100022",
            ownerships=[{"team_id": team_id, "ownership_percentage": 60}],
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert [stake["team_id"] for stake in body["ownerships"]] == [team_id]

    # The team's own panel now names the owner, and the team is not duplicated.
    owners = client.get(f"/v1/teams/{team_id}/owners").json()
    assert [stake["owner_id"] for stake in owners] == [body["id"]]


# --- 7. Google sign-in ---------------------------------------------------------


def _google_user(email, subject, full_name="Google User"):
    async def run():
        async with async_session() as db:
            return await login_or_create_google_user(
                db,
                {
                    "email": email,
                    "sub": subject,
                    "full_name": full_name,
                    "profile_picture": None,
                    "email_verified": True,
                },
            )

    return asyncio.run(run())


def test_a_google_sign_in_stores_the_google_accounts_own_identifier():
    """`sub` is stable for the life of the Google account; the email is not."""
    user = _google_user("google_owner_one@test.com", "google-sub-001")
    assert user.id
    assert user.google_sub == "google-sub-001"
    assert user.auth_provider == "google"


def test_a_second_sign_in_with_the_same_google_identity_finds_the_same_account():
    first = _google_user("google_owner_two@test.com", "google-sub-002")
    second = _google_user("google_owner_two@test.com", "google-sub-002")
    assert first.id == second.id


def test_a_google_identity_is_matched_on_its_identifier_not_on_the_address():
    """A new address for the same Google account is the same person."""
    first = _google_user("google_owner_three@test.com", "google-sub-003")
    second = _google_user("moved_owner@test.com", "google-sub-003")
    assert first.id == second.id
    assert second.email == "moved_owner@test.com"


def test_an_address_already_linked_to_another_google_identity_is_refused():
    """A second Google account claiming the same address is a takeover."""
    _google_user("taken_owner@test.com", "google-sub-004")
    try:
        _google_user("taken_owner@test.com", "google-sub-005")
    except Exception as exc:  # HTTPException
        assert getattr(exc, "status_code", None) == 403
    else:  # pragma: no cover - the refusal is the point of the test
        raise AssertionError("the second Google identity was allowed to take the account")


# --- 8. nothing else broke -----------------------------------------------------


def test_the_teams_still_work(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    resp = client.get("/v1/teams")
    assert resp.status_code == 200
    assert any(row["id"] == team for row in resp.json())


def test_the_team_detail_still_reports_its_squad(client, refs, admin, as_user):
    as_user(admin)
    team = _team(client, refs)
    resp = client.get(f"/v1/teams/{team}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"]
    assert body["total"] == 0
    # The new panel is present and empty, rather than missing.
    assert body["owners"] == []


def test_the_players_still_work(client, refs, admin, as_user):
    as_user(admin)
    resp = client.get("/v1/players")
    assert resp.status_code == 200


def test_the_matches_still_work(client, refs, admin, as_user):
    as_user(admin)
    resp = client.get("/v1/matches")
    assert resp.status_code == 200
