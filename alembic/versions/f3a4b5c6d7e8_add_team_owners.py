"""create the team owners register and the ownership stakes

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-10-22 10:00:00.000000

Ownership was a free-text name in a column on the team: "Mr. Anil Sharma
(Chairman)". That is enough to print a fixture list and not enough to show a
photo, a nationality, a share of the club or a way to reach the person.

Two tables replace it, and the split is the whole point:

``team_owners``
    The people. A name, a date of birth, a nationality, a way to reach them,
    and the organisation the ownership is held through when it is held through
    one at all.

``team_ownerships``
    The stakes. Which owner holds which team, what kind of stake it is, how
    much of the team it is, when it started, when it ended, and whether it is
    still live. A person may own four teams - outright one, a third of
    another, and none of the other two any more - so the terms cannot live on
    the owner.

The rules the tables are built around:

* the live stakes of a team are unique on (team, owner), enforced by a
  *partial* unique index. An owner who hands a team over and takes it back
  later has two records - the old one ended and kept, the new one live -
  rather than one record that has been rewritten;
* the live stakes of a team add up to no more than a hundred percent. They
  are not required to add up to a hundred: a club that does not split its
  equity does not have to pretend to;
* deleting an owner must not take a team, its players or its matches with it,
  so the foreign key from the stakes to the owner is RESTRICT. Deleting a
  team does remove the stakes that only existed because of it.

``users`` gains ``google_sub``: the Google account's own identifier, which is
stable for the life of that account unlike the email address. A Google sign-in
is matched on it, so a sign-in cannot be taken over by changing an address.

Nothing is dropped, reset or rewritten. The teams, the players, the matches
and the users are left exactly as they were.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The columns of the owners table, in the order they are created.
#
# A column is (name, type, nullable, server default). The server defaults are
# only used when the column has to be added to a table that already holds
# rows; they are what stops "NOT NULL" from failing on a table with data in
# it. A fresh table gets the same defaults, which is harmless - the
# application supplies its own values on every write.
OWNER_COLUMNS = (
    ("id", sa.Integer(), False, None),
    ("user_id", sa.Integer(), True, None),
    ("linked_user_id", sa.Integer(), True, None),
    ("first_name", sa.String(length=50), False, None),
    ("last_name", sa.String(length=50), False, None),
    ("date_of_birth", sa.Date(), False, None),
    ("gender", sa.String(length=20), False, None),
    ("country_id", sa.Integer(), False, None),
    ("profile_image", sa.String(length=500), True, None),
    ("email", sa.String(length=100), False, None),
    ("country_code", sa.String(length=5), False, ""),
    ("mobile_number", sa.String(length=20), True, None),
    ("phone_e164", sa.String(length=20), True, None),
    ("ownership_type", sa.String(length=30), False, None),
    ("ownership_status", sa.String(length=20), False, "active"),
    ("organization_name", sa.String(length=150), True, None),
    ("registration_number", sa.String(length=80), True, None),
    ("notes", sa.Text(), True, None),
    ("created_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("updated_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("created_by_id", sa.Integer(), True, None),
    ("updated_by_id", sa.Integer(), True, None),
)

# The foreign keys the owners table carries.
OWNER_FOREIGN_KEYS = (
    ("user_id", "users"),
    ("linked_user_id", "users"),
    ("country_id", "countries"),
    ("created_by_id", "users"),
    ("updated_by_id", "users"),
)

OWNER_INDEXES = (
    ("ix_team_owners_email", ["email"]),
    ("ix_team_owners_name_lookup", ["user_id", "last_name", "first_name"]),
    ("ix_team_owners_phone_e164", ["phone_e164"]),
    ("ix_team_owners_user_id", ["user_id"]),
    ("ix_team_owners_linked_user_id", ["linked_user_id"]),
    ("ix_team_owners_user_status", ["user_id", "ownership_status"]),
    ("ix_team_owners_user_type", ["user_id", "ownership_type"]),
)

# The columns of the stakes table.
OWNERSHIP_COLUMNS = (
    ("id", sa.Integer(), False, None),
    ("team_id", sa.Integer(), False, None),
    ("owner_id", sa.Integer(), False, None),
    ("ownership_type", sa.String(length=30), False, None),
    ("ownership_percentage", sa.Float(), True, None),
    ("ownership_start_date", sa.Date(), True, None),
    ("ownership_end_date", sa.Date(), True, None),
    ("ownership_status", sa.String(length=20), False, "active"),
    ("created_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("updated_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("created_by_id", sa.Integer(), True, None),
    ("updated_by_id", sa.Integer(), True, None),
)

# The foreign keys the stakes table carries, and what happens to the row when
# the thing it points at goes away.
#
# A team going away removes the stakes that only existed because of it: they
# are part of the team's record, not of the owner's. An owner going away must
# not remove a team, so the other way round is RESTRICT.
OWNERSHIP_FOREIGN_KEYS = (
    ("team_id", "teams", "CASCADE"),
    ("owner_id", "team_owners", "RESTRICT"),
    ("created_by_id", "users", None),
    ("updated_by_id", "users", None),
)

OWNERSHIP_INDEXES = (
    ("ix_team_ownerships_team_id", ["team_id"]),
    ("ix_team_ownerships_owner_id", ["owner_id"]),
    ("ix_team_ownerships_team_status", ["team_id", "ownership_status"]),
)


def _server_default(default):
    if default is None:
        return None
    if isinstance(default, str):
        # A string default is a *value*, so it has to be quoted. `sa.text()`
        # emits its argument verbatim, and an unquoted word is read by Postgres
        # as a column reference - which it rejects outright with "cannot use
        # column reference in DEFAULT expression". SQLite is more forgiving and
        # accepts the bare word, so an unquoted default passes every test here
        # and still breaks the Postgres deploy. An empty string is a value too,
        # not an absence of one.
        escaped = default.replace("'", "''")
        return sa.text(f"'{escaped}'")
    return default


def _column(name: str, type_: sa.types.TypeEngine, nullable: bool, default) -> sa.Column:
    return sa.Column(
        name,
        type_,
        nullable=nullable,
        server_default=_server_default(default),
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    _add_google_sub(inspector)
    _create_owners_table(inspector, tables)
    _create_ownerships_table(inspector, tables)


def _add_google_sub(inspector) -> None:
    """Give the user table the Google account's own identifier.

    ``sub`` is stable for the life of the Google account, unlike the email
    address, and it is proved genuine by the token's signature before it
    reaches this code. A sign-in is matched on it, so changing an address
    cannot hand an account to somebody else.

    The uniqueness is a unique index rather than a constraint on the column:
    SQLite cannot add a UNIQUE column to a table that already holds rows, and
    an index gets the same answer without rebuilding the table.
    """
    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    if "google_sub" not in existing_columns:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.add_column(sa.Column("google_sub", sa.String(length=255), nullable=True))

    index_names = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_google_sub" not in index_names:
        op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def _create_owners_table(inspector, tables: set[str]) -> None:
    """The register of owners."""
    if "team_owners" in tables:
        return

    columns = [
        _column(name, type_, nullable, default)
        for name, type_, nullable, default in OWNER_COLUMNS
    ]
    foreign_keys = [
        sa.ForeignKeyConstraint([column], [f"{table}.id"])
        for column, table in OWNER_FOREIGN_KEYS
    ]
    op.create_table(
        "team_owners",
        *columns,
        *foreign_keys,
        sa.PrimaryKeyConstraint("id"),
        # One address per club. The stored value is lower-cased by the
        # application, so this is already case-insensitive.
        sa.UniqueConstraint("user_id", "email", name="uq_team_owners_user_email"),
    )

    for name, columns_ in OWNER_INDEXES:
        op.create_index(name, "team_owners", columns_)


def _create_ownerships_table(inspector, tables: set[str]) -> None:
    """The stake book: who holds which team, on what terms."""
    if "team_ownerships" in tables:
        return

    columns = [
        _column(name, type_, nullable, default)
        for name, type_, nullable, default in OWNERSHIP_COLUMNS
    ]
    foreign_keys = [
        sa.ForeignKeyConstraint([column], [f"{table}.id"], ondelete=ondelete)
        for column, table, ondelete in OWNERSHIP_FOREIGN_KEYS
    ]
    op.create_table(
        "team_ownerships",
        *columns,
        *foreign_keys,
        sa.PrimaryKeyConstraint("id"),
    )

    for name, columns_ in OWNERSHIP_INDEXES:
        op.create_index(name, "team_ownerships", columns_)

    # One live stake per (team, owner). The index is partial - it covers only
    # the live rows - so an ended stake keeps its place in the history without
    # blocking a fresh one.
    op.create_index(
        "uq_team_ownerships_active",
        "team_ownerships",
        ["team_id", "owner_id"],
        unique=True,
        sqlite_where=sa.text("ownership_status = 'active'"),
        postgresql_where=sa.text("ownership_status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_team_ownerships_active", table_name="team_ownerships")
    for name, _columns in OWNERSHIP_INDEXES:
        op.drop_index(name, table_name="team_ownerships")
    op.drop_table("team_ownerships")

    for name, _columns in OWNER_INDEXES:
        op.drop_index(name, table_name="team_owners")
    op.drop_table("team_owners")

    op.drop_index("ix_users_google_sub", table_name="users")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("google_sub")
