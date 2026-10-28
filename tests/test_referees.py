"""The match officials: the register, the photo, and the appointment book.

The officials panel is the part of the scorecard that was previously three
free-text names and nothing else - no photo, no nationality, no way to reach
the official. This file tests the whole path that makes that panel real:

1. an official is registered with every detail the panel shows;
2. the register can be searched, filtered, sorted and paged;
3. the photo is stored, replaced and removed without leaving files behind;
4. a match links to registered officials, and the panel answers with them;
5. the rules that keep the data honest - one official cannot stand in two
   slots of the same match, an official must belong to the account that names
   them, a suspended official is not put on new matches, and an official who
   has officiated is not silently deleted;
6. the phone number is masked in a list and shown in full where it is needed.
"""

import asyncio
import io
import itertools
import struct
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_EMAIL = "referee_admin@test.com"
OTHER_ADMIN_EMAIL = "referee_admin_b@test.com"


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


def _make_admin(email, name):
    async def setup():
        async with async_session() as db:
            user = User(
                email=email,
                hashed_password=hash_password("Test@1234"),
                full_name=name,
                role="admin",
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
    return _make_admin(ADMIN_EMAIL, "Referee Admin")


@pytest.fixture(scope="module")
def other_admin():
    return _make_admin(OTHER_ADMIN_EMAIL, "Other Referee Admin")


@pytest.fixture(scope="module")
def clean_admin():
    """An account that has never registered an official.

    The tests that check the register is scoped per account need an account
    whose register is empty, and the accounts above are used by other tests
    that do register officials.
    """
    return _make_admin("referee_clean_admin@test.com", "Clean Referee Admin")


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


# Every official the tests register needs an address of its own: the email is
# unique within an account, and the tests all register under one account.
_EMAIL_SEQUENCE = itertools.count(1)


def _referee_payload(refs, **overrides):
    """A complete, valid referee create payload."""
    _, country_id, _, _ = refs
    payload = {
        "first_name": "John",
        "last_name": "Smith",
        "date_of_birth": "1985-04-11",
        "country_id": country_id,
        "profile_image": None,
        "referee_type": "first_umpire",
        "experience_years": 12,
        "country_code": "+91",
        "mobile_number": "9812345670",
        "email": "john.smith@test.com",
    }
    payload.update(overrides)
    return payload


def _create_referee(client, refs, **overrides):
    """Register an official, with an email address of its own."""
    payload = _referee_payload(refs, **overrides)
    if "email" not in overrides:
        payload["email"] = f"official{next(_EMAIL_SEQUENCE)}@test.com"
    resp = client.post("/v1/referees", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _team(client, refs, name):
    level_id, country_id, state_id, city_id = refs
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


def _create_match(client, refs, **overrides):
    """Create a match with two fresh teams and the given official slots."""
    suffix = overrides.pop("_suffix", "A")
    team_a = _team(client, refs, f"Officials FC {suffix}")
    team_b = _team(client, refs, f"Rivals FC {suffix}")
    payload = {
        "match_date": "2026-09-30",
        "match_time": "19:30",
        "venue": "Wankhede",
        "match_type": "T20",
        "team_a_id": team_a,
        "team_b_id": team_b,
        "toss_winner_id": team_a,
        "toss_decision": "bat",
    }
    payload.update(overrides)
    resp = client.post("/v1/matches", json=payload)
    return resp


# --- 1. registering an official ---------------------------------------------


def test_creating_a_referee_records_every_detail_the_panel_shows(client, refs, admin, as_user):
    as_user(admin)
    body = _create_referee(client, refs, email="john.smith@test.com")

    assert body["first_name"] == "John"
    assert body["last_name"] == "Smith"
    assert body["full_name"] == "John Smith"
    assert body["date_of_birth"] == "1985-04-11"
    assert body["nationality"]
    assert body["referee_type"] == "first_umpire"
    assert body["experience_years"] == 12
    assert body["mobile_number"] == "9812345670"
    assert body["email"] == "john.smith@test.com"
    # The permanent code is what tells two same-named officials apart.
    assert body["referee_code"].startswith("REF-")
    # The administrative fields the register carries.
    assert body["status"] == "active"
    assert body["availability_status"] == "available"
    # The band is derived from the years, never set on its own.
    assert body["experience_level"] == "experienced"


def test_the_administrative_details_of_an_official_are_recorded(client, refs, admin, as_user):
    as_user(admin)
    body = _create_referee(
        client,
        refs,
        certification_level="national",
        certifying_authority="BCCI",
        certification_number="NC-2026-001",
        certification_expiry_date="2027-04-11",
        specializations=["T20", "ODI"],
        languages=["English", "Hindi"],
        address="12 Willow Road",
        postal_code="400001",
        joining_date="2020-01-15",
        notes="Prefers evening fixtures.",
    )
    assert body["certification_level"] == "national"
    assert body["certifying_authority"] == "BCCI"
    assert body["certification_number"] == "NC-2026-001"
    assert body["certification_expiry_date"] == "2027-04-11"
    assert body["certification_expired"] is False
    assert body["specializations"] == ["T20", "ODI"]
    assert body["languages"] == ["English", "Hindi"]
    assert body["address"] == "12 Willow Road"
    assert body["postal_code"] == "400001"
    assert body["joining_date"] == "2020-01-15"
    assert body["notes"] == "Prefers evening fixtures."
    assert body["created_at"]
    assert body["updated_at"]


def test_a_referee_code_is_unique_and_survives_a_rename(client, refs, admin, as_user):
    as_user(admin)
    first = _create_referee(client, refs)
    second = _create_referee(client, refs, email="jane.doe@test.com", mobile_number="9812345671")
    assert first["referee_code"] != second["referee_code"]

    renamed = client.patch(f"/v1/referees/{first['id']}", json={"last_name": "Jones"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["referee_code"] == first["referee_code"]
    assert renamed.json()["full_name"] == "John Jones"


def test_the_referee_type_spellings_a_client_actually_sends_are_accepted(
    client, refs, admin, as_user
):
    """A scorecard says "Umpire 1", not "first_umpire"."""
    as_user(admin)
    body = _create_referee(client, refs, referee_type="Umpire 1")
    assert body["referee_type"] == "first_umpire"

    body = _create_referee(
        client,
        refs,
        referee_type="Match Referee",
        email="mr@test.com",
        mobile_number="9812345672",
    )
    assert body["referee_type"] == "match_referee"


def test_an_unknown_referee_type_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, referee_type="timekeeper"))
    assert resp.status_code == 422


def test_a_future_date_of_birth_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, date_of_birth="2099-01-01"))
    assert resp.status_code == 422


def test_only_an_admin_may_register_an_official(client, refs, admin, as_user):
    async def setup():
        async with async_session() as db:
            user = User(
                email="referee_viewer@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Referee Viewer",
                role="viewer",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    viewer = asyncio.run(setup())
    as_user(viewer)
    assert client.post("/v1/referees", json=_referee_payload(refs)).status_code == 403


def test_only_an_admin_may_change_or_remove_an_official(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs)

    async def setup():
        async with async_session() as db:
            user = User(
                email="referee_editor@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Referee Editor",
                role="viewer",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    viewer = asyncio.run(setup())
    as_user(viewer)
    assert client.patch(f"/v1/referees/{created['id']}", json={"last_name": "X"}).status_code == 403
    assert client.delete(f"/v1/referees/{created['id']}").status_code == 403


# --- 1b. the validation rules -----------------------------------------------


def test_an_email_already_on_the_register_is_refused(client, refs, admin, as_user):
    """The address is unique within an account, whatever its case."""
    as_user(admin)
    _create_referee(client, refs, email="duplicate@test.com")

    resp = client.post("/v1/referees", json=_referee_payload(refs, email="duplicate@test.com"))
    assert resp.status_code == 409
    assert "already on the register" in resp.json()["detail"]


def test_the_same_email_in_a_different_case_is_the_same_address(client, refs, admin, as_user):
    as_user(admin)
    _create_referee(client, refs, email="Mixed.Case@test.com")

    resp = client.post("/v1/referees", json=_referee_payload(refs, email="mixed.case@test.com"))
    assert resp.status_code == 409


def test_the_same_email_may_be_used_by_another_account(client, refs, admin, other_admin, as_user):
    """Uniqueness is scoped to the account that owns the register."""
    as_user(admin)
    _create_referee(client, refs, email="shared@test.com")

    as_user(other_admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, email="shared@test.com"))
    assert resp.status_code == 201, resp.text


def test_changing_to_an_address_already_on_file_is_refused(client, refs, admin, as_user):
    as_user(admin)
    first = _create_referee(client, refs, email="taken.one@test.com")
    second = _create_referee(client, refs, email="taken.two@test.com")

    resp = client.patch(f"/v1/referees/{second['id']}", json={"email": "taken.one@test.com"})
    assert resp.status_code == 409

    # The official keeps the address they had.
    assert client.get(f"/v1/referees/{second['id']}").json()["email"] == "taken.two@test.com"
    # And an official may keep their own address.
    resp = client.patch(f"/v1/referees/{first['id']}", json={"email": "TAKEN.ONE@test.com"})
    assert resp.status_code == 200, resp.text


def test_a_number_that_is_not_a_number_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, mobile_number="98123abc4567"))
    assert resp.status_code == 422


def test_a_number_that_is_too_short_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, mobile_number="123"))
    assert resp.status_code == 422


def test_a_number_longer_than_e164_allows_is_refused(client, refs, admin, as_user):
    """The dialling code and the number together have to be dialable."""
    as_user(admin)
    resp = client.post(
        "/v1/referees",
        json=_referee_payload(refs, country_code="+91", mobile_number="1234567890123456"),
    )
    assert resp.status_code == 422


def test_a_number_is_not_ten_digits_by_rule(client, refs, admin, as_user):
    """The length is the country's business, not the server's."""
    as_user(admin)
    body = _create_referee(client, refs, mobile_number="+44 20 7946 0958", country_code="+44")
    # Twelve digits, kept whole. The server does not trim a number to a
    # length of its own, and it does not refuse one that is not ten digits.
    assert body["mobile_number"] == "442079460958"
    assert len(body["mobile_number"]) == 12
    # The dialling code and the number are still both there to dial.
    assert body["country_code"] == "+44"


def test_negative_experience_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, experience_years=-1))
    assert resp.status_code == 422


def test_an_unknown_status_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, status="on fire"))
    assert resp.status_code == 422


def test_an_unknown_availability_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, availability_status="maybe"))
    assert resp.status_code == 422


def test_an_unknown_certification_level_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post(
        "/v1/referees", json=_referee_payload(refs, certification_level="world class")
    )
    assert resp.status_code == 422


def test_the_experience_band_may_not_contradict_the_years(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post(
        "/v1/referees",
        json=_referee_payload(
            refs,
            experience_years=12,
            experience_level="beginner",
            email="band.contradicts@test.com",
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/v1/referees",
        json=_referee_payload(
            refs,
            experience_years=12,
            experience_level="experienced",
            email="band.agrees@test.com",
        ),
    )
    assert resp.status_code == 201, resp.text


def test_the_experience_band_follows_the_years_when_they_change(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs, experience_years=12)
    assert created["experience_level"] == "experienced"

    patched = client.patch(f"/v1/referees/{created['id']}", json={"experience_years": 4})
    assert patched.status_code == 200, patched.text
    assert patched.json()["experience_level"] == "intermediate"


def test_an_empty_name_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, first_name="   "))
    assert resp.status_code == 422


def test_an_address_that_is_not_an_address_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = client.post("/v1/referees", json=_referee_payload(refs, email="not-an-address"))
    assert resp.status_code == 422


# --- 2. reading the register ------------------------------------------------


def test_the_list_masks_the_number_and_one_opens_in_full(client, refs, admin, as_user):
    """A list is read by people who have no reason to call each official."""
    as_user(admin)
    created = _create_referee(client, refs)

    # An admin reads the register in full.
    listed = client.get("/v1/referees")
    assert listed.status_code == 200
    row = next(r for r in listed.json() if r["id"] == created["id"])
    assert row["mobile_number"] == "9812345670"
    # The display form is masked whatever the caller's role is.
    assert "*" in row["phone_display"]

    # A non-admin owner reads the list masked, and one official in full.
    scorer = _make_scorer(created["id"])
    as_user(scorer)
    listed = client.get("/v1/referees")
    row = next(r for r in listed.json() if r["id"] == created["id"])
    assert row["mobile_number"] != "9812345670"
    # The masked form is carried by phone_display, exactly as the player list
    # carries it: mobile_number keeps the app's list convention.
    assert "*" in row["phone_display"]
    assert row["phone_display"] != "9812345670"

    detail = client.get(f"/v1/referees/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["mobile_number"] == "9812345670"


def _make_scorer(referee_id):
    """Create a non-admin account and hand one official over to it.

    The register is account-scoped, so the only way to see the masked list is to
    be the owning account and not an admin - which is the situation the masking
    exists for.
    """
    from app.referees.models import Referee

    async def setup():
        async with async_session() as db:
            user = User(
                email="referee_scorer@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Referee Scorer",
                role="scorer",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            referee = (
                await db.execute(select(Referee).where(Referee.id == referee_id))
            ).scalar_one()
            referee.user_id = user.id
            await db.commit()
            return user

    return asyncio.run(setup())


def test_the_picker_carries_no_contact_details(client, refs, admin, as_user):
    """The dropdown needs to tell two officials apart, and nothing more."""
    as_user(admin)
    created = _create_referee(client, refs)

    options = client.get("/v1/referees/options")
    assert options.status_code == 200
    row = next(r for r in options.json() if r["id"] == created["id"])
    assert row["full_name"] == "John Smith"
    assert row["referee_type"] == "first_umpire"
    assert "mobile_number" not in row
    assert "email" not in row


def test_the_register_is_scoped_per_account(client, refs, admin, clean_admin, as_user):
    as_user(clean_admin)
    assert client.get("/v1/referees").json() == []


def test_another_accounts_official_is_reported_as_missing(
    client, refs, admin, clean_admin, as_user
):
    as_user(admin)
    created = _create_referee(client, refs)

    as_user(clean_admin)
    assert client.get("/v1/referees").json() == []
    # Another account's official is reported as missing, not as forbidden.
    assert client.get(f"/v1/referees/{created['id']}").status_code == 404
    assert client.patch(f"/v1/referees/{created['id']}", json={"last_name": "X"}).status_code == 404
    assert client.delete(f"/v1/referees/{created['id']}").status_code == 404


def test_an_official_can_be_looked_up_by_code(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs)

    resp = client.get(f"/v1/referees/by-code/{created['referee_code']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]

    assert client.get("/v1/referees/by-code/REF-NOPE00").status_code == 404


def test_the_register_is_pageable_and_searchable(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs, first_name="Findable", last_name="Official")

    listed = client.get("/v1/referees", params={"q": "Findable"})
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [created["id"]]
    assert "x-total-count" in listed.headers

    filtered = client.get("/v1/referees", params={"referee_type": "match_referee"})
    assert filtered.status_code == 200
    assert created["id"] not in [r["id"] for r in filtered.json()]


def test_the_register_can_be_filtered(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(
        client, refs, referee_type="first_umpire", experience_years=2, certification_level="local"
    )
    veteran = _create_referee(
        client,
        refs,
        first_name="Veteran",
        last_name="Umpire",
        referee_type="match_referee",
        experience_years=15,
        certification_level="international",
    )

    # The register holds every official this account has, so a filter is
    # asked which rows it keeps - not what the whole register looks like.
    by_type = client.get("/v1/referees", params={"referee_type": "match_referee"}).json()
    assert veteran["id"] in [r["id"] for r in by_type]
    assert umpire["id"] not in [r["id"] for r in by_type]

    by_experience = client.get("/v1/referees", params={"experience_min": 10}).json()
    assert umpire["id"] not in [r["id"] for r in by_experience]
    assert veteran["id"] in [r["id"] for r in by_experience]

    by_ceiling = client.get("/v1/referees", params={"experience_max": 5}).json()
    assert veteran["id"] not in [r["id"] for r in by_ceiling]
    assert umpire["id"] in [r["id"] for r in by_ceiling]

    by_certification = client.get(
        "/v1/referees", params={"certification_level": "international"}
    ).json()
    assert veteran["id"] in [r["id"] for r in by_certification]
    assert umpire["id"] not in [r["id"] for r in by_certification]

    by_status = client.get("/v1/referees", params={"status": "active"}).json()
    assert umpire["id"] in [r["id"] for r in by_status]
    assert client.get("/v1/referees", params={"status": "suspended"}).json() == []


def test_the_register_can_be_filtered_by_nationality(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs)
    _, country_id, _, _ = refs

    by_id = client.get("/v1/referees", params={"nationality": country_id}).json()
    assert created["id"] in [r["id"] for r in by_id]

    by_name = client.get("/v1/referees", params={"nationality": "india"}).json()
    assert created["id"] in [r["id"] for r in by_name]


def test_the_register_can_be_sorted(client, refs, admin, as_user):
    as_user(admin)
    _create_referee(client, refs, first_name="Aaron", last_name="Zulu")
    _create_referee(client, refs, first_name="Marcus", last_name="Alpha")

    ascending = client.get("/v1/referees", params={"sort_by": "last_name"}).json()
    names = [r["last_name"] for r in ascending]
    assert names == sorted(names)

    descending = client.get(
        "/v1/referees", params={"sort_by": "last_name", "sort_order": "desc"}
    ).json()
    assert [r["last_name"] for r in descending] == list(reversed(names))


def test_the_register_is_paged(client, refs, admin, as_user):
    as_user(admin)
    before = client.get("/v1/referees", params={"page_size": 1}).headers["x-total-count"]
    created = [_create_referee(client, refs, first_name=f"Paged{index}") for index in range(5)]

    page = client.get("/v1/referees", params={"page": 1, "page_size": 2})
    assert page.status_code == 200
    assert len(page.json()) == 2
    # The count is of the whole register - the page is a window on it.
    assert page.headers["x-total-count"] == str(int(before) + len(created))
    assert page.headers["x-page"] == "1"
    assert page.headers["x-page-size"] == "2"
    assert page.headers["x-total-pages"] == str(
        -(-(int(before) + len(created)) // 2)  # ceiling division
    )

    second = client.get("/v1/referees", params={"page": 2, "page_size": 2}).json()
    first_ids = {r["id"] for r in page.json()}
    assert first_ids.isdisjoint({r["id"] for r in second})
    # Every official registered here is on the register, somewhere in its pages.
    total_pages = int(page.headers["x-total-pages"])
    listed = {
        r["id"]
        for page_number in range(1, total_pages + 1)
        for r in client.get("/v1/referees", params={"page": page_number, "page_size": 2}).json()
    }
    assert {c["id"] for c in created} <= listed


def test_the_register_reports_its_counts(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs)

    stats = client.get("/v1/referees/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["total"] >= 1
    assert body["active"] >= 1
    assert body["suspended"] == 0

    client.patch(f"/v1/referees/{created['id']}", json={"status": "suspended"})
    body = client.get("/v1/referees/stats").json()
    assert body["suspended"] == 1
    assert body["active"] >= 1


# --- 3. the officials panel of a match ---------------------------------------


def test_the_officials_panel_shows_the_three_slots_with_their_details(client, refs, admin, as_user):
    """The wire format this feature exists for.

    "Match Officials / Referee 1 / John Smith / Referee 2 / Jane Doe /
    Match Referee" - and behind each name, the details of a real person.
    """
    as_user(admin)
    john = _create_referee(client, refs)
    jane = _create_referee(
        client,
        refs,
        first_name="Jane",
        last_name="Doe",
        referee_type="match_referee",
        email="jane.panel@test.com",
        mobile_number="9812345673",
    )
    third = _create_referee(
        client,
        refs,
        first_name="Ann",
        last_name="Bell",
        referee_type="second_umpire",
        email="ann.bell@test.com",
        mobile_number="9812345674",
    )

    resp = _create_match(
        client,
        refs,
        _suffix="Panel",
        referee_1_id=john["id"],
        referee_2_id=third["id"],
        match_referee_id=jane["id"],
    )
    assert resp.status_code == 201, resp.text

    officials = resp.json()["officials"]
    assert officials["referee_1"]["slot"] == "referee_1"
    assert officials["referee_1"]["label"] == "Referee 1"
    assert officials["referee_1"]["name"] == "John Smith"
    assert officials["referee_2"]["label"] == "Referee 2"
    assert officials["referee_2"]["name"] == "Ann Bell"
    assert officials["match_referee"]["label"] == "Match Referee"
    assert officials["match_referee"]["name"] == "Jane Doe"

    # Every detail the register holds is on the panel too.
    slot = officials["referee_1"]
    assert slot["referee_id"] == john["id"]
    assert slot["referee_code"] == john["referee_code"]
    assert slot["first_name"] == "John"
    assert slot["last_name"] == "Smith"
    assert slot["date_of_birth"] == "1985-04-11"
    assert slot["nationality"]
    assert slot["referee_type"] == "first_umpire"
    assert slot["experience_years"] == 12
    assert slot["mobile_number"] == "9812345670"
    assert slot["email"] == john["email"]
    assert slot["status"] == "active"
    assert slot["availability_status"] == "available"

    # The appointment list behind the panel.
    assert len(officials["assignments"]) == 3
    roles = {a["role"] for a in officials["assignments"]}
    assert roles == {"first_umpire", "second_umpire", "match_referee"}


def test_the_officials_panel_is_three_slots_even_when_empty(client, refs, admin, as_user):
    as_user(admin)
    resp = _create_match(client, refs, _suffix="Empty")
    assert resp.status_code == 201, resp.text

    officials = resp.json()["officials"]
    assert officials["referee_1"] is not None
    assert officials["referee_1"]["name"] is None
    assert officials["referee_2"]["label"] == "Referee 2"
    assert officials["match_referee"]["label"] == "Match Referee"
    assert officials["assignments"] == []


def test_the_panel_is_returned_by_reading_a_match_too(client, refs, admin, as_user):
    as_user(admin)
    john = _create_referee(client, refs)
    resp = _create_match(client, refs, _suffix="Read", referee_1_id=john["id"])
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    detail = client.get(f"/v1/matches/{match_id}")
    assert detail.status_code == 200
    assert detail.json()["officials"]["referee_1"]["name"] == "John Smith"


def test_the_mirror_name_column_follows_the_linked_official(client, refs, admin, as_user):
    """The old free-text name cannot drift away from the linked record."""
    as_user(admin)
    john = _create_referee(client, refs)
    resp = _create_match(client, refs, _suffix="Mirror", referee_1_id=john["id"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["referee_1_name"] == "John Smith"

    renamed = client.patch(f"/v1/referees/{john['id']}", json={"last_name": "Jones"})
    assert renamed.status_code == 200, renamed.text

    match_id = resp.json()["id"]
    # The panel follows the record; the mirror column is rewritten on the next
    # write to the match.
    assert client.get(f"/v1/matches/{match_id}").json()["officials"]["referee_1"]["name"] == (
        "John Jones"
    )

    patched = client.patch(f"/v1/matches/{match_id}", json={"referee_1_id": john["id"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["referee_1_name"] == "John Jones"


def test_a_plain_text_official_name_still_works(client, refs, admin, as_user):
    """An account that never registered a referee still gets a scorecard."""
    as_user(admin)
    resp = _create_match(
        client, refs, _suffix="Plain", referee_1_name="Local Umpire", match_referee_name="The Ref"
    )
    assert resp.status_code == 201, resp.text

    officials = resp.json()["officials"]
    assert officials["referee_1"]["name"] == "Local Umpire"
    assert officials["referee_1"]["referee_id"] is None
    assert officials["match_referee"]["name"] == "The Ref"


def test_a_slot_can_be_unlinked_by_patching_it_to_null(client, refs, admin, as_user):
    as_user(admin)
    john = _create_referee(client, refs)
    resp = _create_match(client, refs, _suffix="Unlink", referee_1_id=john["id"])
    match_id = resp.json()["id"]

    patched = client.patch(f"/v1/matches/{match_id}", json={"referee_1_id": None})
    assert patched.status_code == 200, patched.text
    assert patched.json()["officials"]["referee_1"]["referee_id"] is None
    assert patched.json()["officials"]["referee_1"]["name"] is None
    assert patched.json()["referee_1_name"] is None
    # The appointment goes with the slot.
    assert patched.json()["officials"]["assignments"] == []


# --- 4. the rules that keep the data honest ---------------------------------


def test_one_official_cannot_stand_in_two_slots_of_the_same_match(client, refs, admin, as_user):
    as_user(admin)
    john = _create_referee(client, refs)

    resp = _create_match(
        client,
        refs,
        _suffix="Dup",
        referee_1_id=john["id"],
        match_referee_id=john["id"],
    )
    assert resp.status_code == 400
    assert "two slots" in resp.json()["detail"]


def test_an_official_from_another_account_cannot_be_named(
    client, refs, admin, other_admin, as_user
):
    as_user(other_admin)
    theirs = _create_referee(client, refs, email="theirs@test.com", mobile_number="9812345675")

    as_user(admin)
    resp = _create_match(client, refs, _suffix="Foreign", referee_1_id=theirs["id"])
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


def test_an_unknown_official_id_is_refused(client, refs, admin, as_user):
    as_user(admin)
    resp = _create_match(client, refs, _suffix="Ghost", referee_1_id=999999)
    assert resp.status_code == 400


def test_an_official_who_has_officiated_cannot_be_deleted(client, refs, admin, as_user):
    """A deleted official would leave the scorecard naming nobody."""
    as_user(admin)
    john = _create_referee(client, refs)
    resp = _create_match(client, refs, _suffix="Keep", referee_1_id=john["id"])
    assert resp.status_code == 201, resp.text

    refused = client.delete(f"/v1/referees/{john['id']}")
    assert refused.status_code == 409
    assert client.get(f"/v1/referees/{john['id']}").status_code == 200


def test_the_delete_check_says_what_deleting_would_do(client, refs, admin, as_user):
    as_user(admin)
    spare = _create_referee(client, refs)
    check = client.get(f"/v1/referees/{spare['id']}/delete-check")
    assert check.status_code == 200
    assert check.json()["can_delete"] is True

    resp = _create_match(client, refs, _suffix="Check", referee_1_id=spare["id"])
    assert resp.status_code == 201, resp.text

    check = client.get(f"/v1/referees/{spare['id']}/delete-check")
    assert check.status_code == 200
    body = check.json()
    assert body["can_delete"] is False
    assert body["upcoming_matches"] == 1
    assert body["reason"]


def test_an_official_who_has_never_officiated_is_deleted(client, refs, admin, as_user):
    as_user(admin)
    spare = _create_referee(client, refs, email="spare@test.com", mobile_number="9812345676")
    assert client.delete(f"/v1/referees/{spare['id']}").status_code == 204
    assert client.get(f"/v1/referees/{spare['id']}").status_code == 404


def test_the_matches_an_official_stands_in_are_listed(client, refs, admin, as_user):
    as_user(admin)
    john = _create_referee(client, refs)
    resp = _create_match(client, refs, _suffix="Appointed", referee_1_id=john["id"])
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    listed = client.get(f"/v1/referees/{john['id']}/matches")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["match_id"] == match_id
    assert rows[0]["role"] == "first_umpire"
    assert rows[0]["venue"] == "Wankhede"


# --- 5. the appointment book ------------------------------------------------


def test_an_official_can_be_appointed_to_a_match(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    resp = _create_match(client, refs, _suffix="Appoint")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text
    body = appointed.json()
    assert body["assignment"]["referee_id"] == umpire["id"]
    assert body["assignment"]["role"] == "first_umpire"
    assert body["officials"]["referee_1"]["name"] == "John Smith"
    assert body["warnings"] == []

    # The match's own record carries the appointment too.
    detail = client.get(f"/v1/matches/{match_id}").json()
    assert detail["officials"]["referee_1"]["referee_id"] == umpire["id"]
    assert detail["referee_1_id"] == umpire["id"]
    assert detail["referee_1_name"] == "John Smith"


def test_an_official_can_be_appointed_to_a_role_the_panel_has_no_seat_for(
    client, refs, admin, as_user
):
    as_user(admin)
    tv = _create_referee(client, refs, referee_type="third_umpire")
    resp = _create_match(client, refs, _suffix="TV")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": tv["id"], "role": "third_umpire"},
    )
    assert appointed.status_code == 201, appointed.text
    body = appointed.json()
    assert body["assignment"]["role"] == "third_umpire"
    # The three seats are untouched; the appointment lives on the list.
    assert body["officials"]["referee_1"]["referee_id"] is None
    assert len(body["officials"]["assignments"]) == 1

    listed = client.get(f"/v1/matches/{match_id}/officials")
    assert listed.status_code == 200
    assert len(listed.json()["assignments"]) == 1


def test_an_appointment_can_be_withdrawn(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    resp = _create_match(client, refs, _suffix="Withdraw")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text
    assignment_id = appointed.json()["assignment"]["id"]

    withdrawn = client.delete(f"/v1/matches/{match_id}/officials/{assignment_id}")
    assert withdrawn.status_code == 204

    detail = client.get(f"/v1/matches/{match_id}").json()
    assert detail["officials"]["referee_1"]["referee_id"] is None
    assert detail["officials"]["assignments"] == []
    assert detail["referee_1_id"] is None
    # The official is free to be appointed to something else.
    assert client.get(f"/v1/referees/{umpire['id']}/matches").json() == []


def test_a_suspended_official_is_not_appointed(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    client.patch(f"/v1/referees/{umpire['id']}", json={"status": "suspended"})

    resp = _create_match(client, refs, _suffix="Suspended")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 409
    assert "suspended" in appointed.json()["detail"].lower()


def test_an_inactive_official_is_not_appointed(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    client.patch(f"/v1/referees/{umpire['id']}", json={"status": "inactive"})

    resp = _create_match(client, refs, _suffix="Inactive")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 409


def test_an_official_cannot_be_appointed_to_a_role_their_type_cannot_fill(
    client, refs, admin, as_user
):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    resp = _create_match(client, refs, _suffix="WrongRole")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "match_referee"},
    )
    assert appointed.status_code == 400
    assert "cannot be appointed" in appointed.json()["detail"]


def test_a_role_is_filled_by_one_official(client, refs, admin, as_user):
    as_user(admin)
    first = _create_referee(client, refs, referee_type="first_umpire")
    second = _create_referee(
        client,
        refs,
        first_name="Second",
        referee_type="first_umpire",
        email="second.umpire@test.com",
    )
    resp = _create_match(client, refs, _suffix="OneRole")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": first["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": second["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 409


def test_an_official_stands_in_one_role_on_a_match(client, refs, admin, as_user):
    as_user(admin)
    official = _create_referee(client, refs, referee_type="first_umpire", email="one.role@test.com")
    resp = _create_match(client, refs, _suffix="OneOfficial")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": official["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": official["id"], "role": "second_umpire"},
    )
    assert appointed.status_code == 409


def test_an_official_from_another_account_cannot_be_appointed(
    client, refs, admin, other_admin, as_user
):
    as_user(other_admin)
    theirs = _create_referee(client, refs, email="not.mine@test.com")

    as_user(admin)
    resp = _create_match(client, refs, _suffix="NotMine")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": theirs["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 404
    assert "not found" in appointed.json()["detail"].lower()


def test_only_an_admin_may_appoint_an_official(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    resp = _create_match(client, refs, _suffix="AdminOnly")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    async def setup():
        async with async_session() as db:
            user = User(
                email="referee_scheduler@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Referee Scheduler",
                role="scorer",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    scorer = asyncio.run(setup())
    as_user(scorer)
    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 403


def test_an_appointment_on_the_same_day_carries_a_warning(client, refs, admin, as_user):
    """A double booking is a phone call, not a refusal."""
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")

    first = _create_match(client, refs, _suffix="ClashOne", match_date="2026-11-01")
    assert first.status_code == 201, first.text
    second = _create_match(client, refs, _suffix="ClashTwo", match_date="2026-11-01")
    assert second.status_code == 201, second.text

    client.post(
        f"/v1/matches/{first.json()['id']}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    appointed = client.post(
        f"/v1/matches/{second.json()['id']}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text
    warnings = appointed.json()["warnings"]
    assert warnings
    assert any("same day" in warning for warning in warnings)


def test_an_unavailable_official_carries_a_warning(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    client.patch(f"/v1/referees/{umpire['id']}", json={"availability_status": "unavailable"})

    resp = _create_match(client, refs, _suffix="Unavailable")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text
    assert any("unavailable" in warning for warning in appointed.json()["warnings"])


def test_deleting_a_match_takes_its_appointments_with_it(client, refs, admin, as_user):
    as_user(admin)
    umpire = _create_referee(client, refs, referee_type="first_umpire")
    resp = _create_match(client, refs, _suffix="Deleted")
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["id"]

    appointed = client.post(
        f"/v1/matches/{match_id}/officials",
        json={"referee_id": umpire["id"], "role": "first_umpire"},
    )
    assert appointed.status_code == 201, appointed.text

    assert client.delete(f"/v1/matches/{match_id}").status_code == 204
    # The official is free again, and their record is intact.
    assert client.get(f"/v1/referees/{umpire['id']}").status_code == 200
    assert client.get(f"/v1/referees/{umpire['id']}/matches").json() == []


# --- 6. the photo ------------------------------------------------------------


def test_the_profile_photo_is_stored_and_served(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    created = _create_referee(client, refs)

    up = client.post(
        f"/v1/referees/{created['id']}/upload-profile-image",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert up.status_code == 200, up.text

    expected = f"/uploads/referees/{created['id']}.png"
    assert up.json()["profile_image"] == expected
    assert client.get(f"/v1/referees/{created['id']}").json()["profile_image"] == expected
    assert client.get(expected).status_code == 200
    assert f"referees/{created['id']}.png" in fake_s3.objects


def test_the_profile_photo_can_be_uploaded_to_its_own_endpoint(
    client, refs, admin, as_user, fake_s3
):
    as_user(admin)
    created = _create_referee(client, refs)

    up = client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["profile_image"] == f"/uploads/referees/{created['id']}.png"


def test_a_file_that_is_not_an_image_is_refused(client, refs, admin, as_user, fake_s3):
    """The bytes decide what a file is, not the header the caller sent."""
    as_user(admin)
    created = _create_referee(client, refs)

    up = client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.png", io.BytesIO(b"#!/bin/sh\necho pwned"), "image/png")},
    )
    assert up.status_code == 400
    assert "Unsupported image type" in up.json()["detail"]
    # Nothing was written to the bucket.
    assert fake_s3.objects == {}
    assert client.get(f"/v1/referees/{created['id']}").json()["profile_image"] is None


def test_an_oversized_photo_is_refused(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    created = _create_referee(client, refs)

    payload = png_bytes() + b"0" * (5 * 1024 * 1024)
    up = client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.png", io.BytesIO(payload), "image/png")},
    )
    assert up.status_code == 400
    assert "exceeds" in up.json()["detail"]
    assert fake_s3.objects == {}


def test_replacing_a_photo_does_not_leave_the_old_one_behind(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    created = _create_referee(client, refs)

    client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    first_key = f"referees/{created['id']}.png"
    assert first_key in fake_s3.objects

    # A different type of image is stored under a different key.
    client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.jpg", io.BytesIO(b"\xff\xd8\xff\xe0jpegdata"), "image/jpeg")},
    )
    second_key = f"referees/{created['id']}.jpg"
    assert second_key in fake_s3.objects
    # The file the new photo replaced is gone.
    assert first_key not in fake_s3.objects
    assert first_key in fake_s3.deleted
    assert (
        client.get(f"/v1/referees/{created['id']}").json()["profile_image"]
        == f"/uploads/{second_key}"
    )


def test_a_photo_can_be_removed(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    created = _create_referee(client, refs)

    client.post(
        f"/v1/referees/{created['id']}/profile-photo",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    key = f"referees/{created['id']}.png"
    assert key in fake_s3.objects

    removed = client.delete(f"/v1/referees/{created['id']}/profile-photo")
    assert removed.status_code == 204
    assert client.get(f"/v1/referees/{created['id']}").json()["profile_image"] is None
    # The file went with it: no orphan left in the bucket.
    assert key not in fake_s3.objects
    assert key in fake_s3.deleted


def test_the_photo_reaches_the_officials_panel(client, refs, admin, as_user):
    as_user(admin)
    created = _create_referee(client, refs)
    up = client.post(
        f"/v1/referees/{created['id']}/upload-profile-image",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert up.status_code == 200, up.text

    resp = _create_match(client, refs, _suffix="Photo", referee_1_id=created["id"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["officials"]["referee_1"]["profile_image"] == up.json()["profile_image"]
