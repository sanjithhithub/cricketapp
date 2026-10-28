"""create the referees table, the appointment book, and the match officials slots

Revision ID: e2f3a4b5c6d7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-21 10:00:00.000000

The match officials used to be three free-text names on the match, which is
enough to print a scorecard and not enough to show a photo, a nationality, an
experience count or a way to reach the official. ``referees`` holds the people;
``matches`` gains three nullable foreign keys, one per slot, so a match can
name registered officials.

The name columns stay. A match may still name an official in plain text - an
account that has never registered a referee still gets a scorecard - and the
two cannot disagree, because the application writes the linked official's name
into the mirror column whenever a slot is linked.

``match_referee_assignments`` is the appointment book: which official stands
in which role on which match. A match has more officials than it has seats
(the TV umpire, the fourth umpire, the reserve) and more kinds of official
than it has seats for, so one column per role cannot express it. The three
slot columns on ``matches`` stay as a compatibility mirror of the first three
roles, and the rows written before this revision are carried into the book by
the backfill at the bottom of this file, so no appointment is lost.

The upgrade is written to be safe on a database that already holds part of
this schema. ``Base.metadata.create_all()`` was used to build some databases
before Alembic was adopted, and it creates new tables but never adds columns
to existing ones - so such a database ends up with the ``referees`` table
already present and the three ``matches`` columns still missing. A plain
``create_table`` would then abort the whole chain with "table referees
already exists" and leave the database exactly as broken as it was. Every
step below is therefore guarded by what the database actually looks like, so
the same revision runs cleanly on a fresh file, on a half-created file and on
one that has already been migrated.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The three official slots on a match, in the order they are stored.
MATCH_REFEREE_COLUMNS = ("referee_1_id", "referee_2_id", "match_referee_id")

# The columns of the referees table, in the order they are created.
#
# A column is (name, type, nullable, server default). The server defaults are
# only used when the column has to be added to a table that already holds
# rows; they are what stops "NOT NULL" from failing on a table with data in
# it. A fresh table gets the same defaults, which is harmless - the
# application supplies its own values on every write.
REFEREE_COLUMNS = (
    ("id", sa.Integer(), False, None),
    ("referee_code", sa.String(length=16), False, None),
    ("first_name", sa.String(length=50), False, None),
    ("last_name", sa.String(length=50), False, None),
    ("date_of_birth", sa.Date(), False, None),
    ("country_id", sa.Integer(), False, None),
    ("profile_image", sa.String(length=500), True, None),
    ("referee_type", sa.String(length=30), False, None),
    ("experience_years", sa.Integer(), False, "0"),
    ("country_code", sa.String(length=5), False, ""),
    ("mobile_number", sa.String(length=20), True, None),
    ("phone_e164", sa.String(length=20), True, None),
    ("email", sa.String(length=100), False, None),
    ("experience_level", sa.String(length=20), False, "beginner"),
    ("certification_level", sa.String(length=20), True, None),
    ("certifying_authority", sa.String(length=120), True, None),
    ("certification_number", sa.String(length=60), True, None),
    ("certification_expiry_date", sa.Date(), True, None),
    ("specializations", sa.JSON(), False, sa.text("'[]'")),
    ("languages", sa.JSON(), False, sa.text("'[]'")),
    ("address", sa.String(length=300), True, None),
    ("state_id", sa.Integer(), True, None),
    ("city_id", sa.Integer(), True, None),
    ("postal_code", sa.String(length=20), True, None),
    ("status", sa.String(length=20), False, "active"),
    ("availability_status", sa.String(length=20), False, "available"),
    ("joining_date", sa.Date(), True, None),
    ("notes", sa.Text(), True, None),
    ("created_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("updated_at", sa.DateTime(), False, sa.text("CURRENT_TIMESTAMP")),
    ("created_by_id", sa.Integer(), True, None),
    ("updated_by_id", sa.Integer(), True, None),
    ("user_id", sa.Integer(), True, None),
)

# The foreign keys the referees table carries.
REFEREE_FOREIGN_KEYS = (
    ("country_id", "countries"),
    ("state_id", "states"),
    ("city_id", "cities"),
    ("created_by_id", "users"),
    ("updated_by_id", "users"),
    ("user_id", "users"),
)

REFEREE_INDEXES = (
    ("ix_referees_email", ["email"]),
    ("ix_referees_name_lookup", ["user_id", "last_name", "first_name"]),
    ("ix_referees_phone_e164", ["phone_e164"]),
    ("ix_referees_user_id", ["user_id"]),
    ("ix_referees_user_status", ["user_id", "status"]),
    ("ix_referees_user_type", ["user_id", "referee_type"]),
)


def _foreign_key_name(column: str) -> str:
    return f"fk_matches_{column}_referees"


def _server_default(default):
    if default is None:
        return None
    if isinstance(default, str):
        # A string default is a *value*, so it has to be quoted. `sa.text()`
        # emits its argument verbatim, and an unquoted word is read by SQLite
        # as a column reference rather than a constant - which it rejects with
        # "default value of column [...] is not constant". An empty string is
        # a value too, not an absence of one.
        escaped = default.replace("'", "''")
        return sa.text(f"'{escaped}'")
    return default


def _referee_column(name: str, type_: sa.types.TypeEngine, nullable: bool, default) -> sa.Column:
    return sa.Column(
        name,
        type_,
        nullable=nullable,
        server_default=_server_default(default),
    )


def _has_unique_constraint(inspector, table: str, name: str) -> bool:
    """Whether a named unique constraint is already on the table."""
    for constraint in inspector.get_unique_constraints(table):
        if constraint.get("name") == name:
            return True
    return False


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "referees" not in tables:
        columns = [
            _referee_column(name, type_, nullable, default)
            for name, type_, nullable, default in REFEREE_COLUMNS
        ]
        foreign_keys = [
            sa.ForeignKeyConstraint([column], [f"{table}.id"])
            for column, table in REFEREE_FOREIGN_KEYS
        ]
        op.create_table(
            "referees",
            *columns,
            *foreign_keys,
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("referee_code", name="uq_referees_referee_code"),
            sa.UniqueConstraint("user_id", "email", name="uq_referees_user_email"),
        )
    else:
        _reconcile_referees(inspector)

    _create_referee_indexes()

    _create_assignment_table(inspector)
    _add_match_columns(inspector)
    _backfill_assignments()


def _reconcile_referees(inspector) -> None:
    """Bring an existing referees table up to the shape the models describe.

    A database built by ``create_all`` before the register grew its
    administrative columns has the table but not the columns. They are added
    here, each with a server default so the rows already in the table survive
    the NOT NULL.

    The columns and their foreign keys are added in one rebuild. Adding the
    column alone would leave ``state_id``, ``city_id`` and the two audit
    columns pointing at nothing: SQLite cannot attach a foreign key to an
    existing column, so a second pass would be needed to get them back.
    """
    existing_columns = {column["name"] for column in inspector.get_columns("referees")}
    missing = [
        (name, type_, nullable, default)
        for name, type_, nullable, default in REFEREE_COLUMNS
        if name not in existing_columns
    ]

    existing_foreign_keys = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in inspector.get_foreign_keys("referees")
        if len(fk["constrained_columns"]) == 1
    }
    missing_foreign_keys = [
        (column, table)
        for column, table in REFEREE_FOREIGN_KEYS
        if (column, table) not in existing_foreign_keys
    ]

    needs_user_email = not _has_unique_constraint(
        inspector, "referees", "uq_referees_user_email"
    )

    if not missing and not missing_foreign_keys and not needs_user_email:
        return

    with op.batch_alter_table("referees", recreate="always") as batch_op:
        for name, type_, nullable, default in missing:
            batch_op.add_column(_referee_column(name, type_, nullable, default))
        for column, table in missing_foreign_keys:
            batch_op.create_foreign_key(
                f"fk_referees_{column}_{table}",
                table,
                [column],
                ["id"],
            )
        if needs_user_email:
            batch_op.create_unique_constraint("uq_referees_user_email", ["user_id", "email"])


def _create_referee_indexes() -> None:
    """Create the indexes the models declare, skipping any that are already there.

    A database that is only being reconciled - rather than built from nothing -
    already carries the older indexes. Re-running the revision must not fail on
    the first one it meets.
    """
    live = sa.inspect(op.get_bind())
    existing = {index["name"] for index in live.get_indexes("referees")}
    for name, columns in REFEREE_INDEXES:
        if name not in existing:
            op.create_index(name, "referees", columns)


def _create_assignment_table(inspector) -> None:
    """The appointment book: one official, one match, one role."""
    if "match_referee_assignments" in set(inspector.get_table_names()):
        return

    op.create_table(
        "match_referee_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("referee_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("assigned_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referee_id"], ["referees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "role", name="uq_match_referee_assignments_match_role"),
        sa.UniqueConstraint(
            "match_id", "referee_id", name="uq_match_referee_assignments_match_referee"
        ),
    )
    op.create_index(
        "ix_match_referee_assignments_match_id", "match_referee_assignments", ["match_id"]
    )
    op.create_index(
        "ix_match_referee_assignments_referee_id", "match_referee_assignments", ["referee_id"]
    )


def _add_match_columns(inspector) -> None:
    """Give the match its three official slots."""
    existing_columns = {c["name"] for c in inspector.get_columns("matches")}
    existing_foreign_keys = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in inspector.get_foreign_keys("matches")
        if len(fk["constrained_columns"]) == 1
    }

    missing_columns = [c for c in MATCH_REFEREE_COLUMNS if c not in existing_columns]
    missing_foreign_keys = [
        c for c in MATCH_REFEREE_COLUMNS if (c, "referees") not in existing_foreign_keys
    ]

    if not missing_columns and not missing_foreign_keys:
        return

    # SQLite cannot add a column with a foreign key to an existing table, so
    # the table is rebuilt: copy the rows across, swap the names, drop the
    # old one. `innings` and `tournament_fixtures` both point at `matches`, and
    # the rebuild has to leave them pointing at the rebuilt table rather than
    # at the copy that is about to be dropped.
    with op.batch_alter_table("matches", recreate="always") as batch_op:
        for column in missing_columns:
            batch_op.add_column(sa.Column(column, sa.Integer(), nullable=True))
        for column in missing_foreign_keys:
            batch_op.create_foreign_key(
                _foreign_key_name(column),
                "referees",
                [column],
                ["id"],
            )


def _backfill_assignments() -> None:
    """Carry the matches that already name an official into the book.

    A match written before the book existed names its officials in its own
    columns. Those appointments are real - they are on a scorecard - so they
    are copied into the book rather than left behind, which would make the
    officials panel and the delete check disagree with each other.

    The two uniqueness rules are applied while copying: a match has one
    official per role, and one role per official. A match that named the same
    person in two slots is a mistake in the data, and it is left as it was
    rather than being turned into two appointments that cannot exist.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, referee_1_id, referee_2_id, match_referee_id FROM matches"
        )
    ).fetchall()

    if not rows:
        return

    taken: set[tuple[int, object]] = set()
    for match_id, referee_1_id, referee_2_id, match_referee_id in rows:
        slots = (
            ("first_umpire", referee_1_id),
            ("second_umpire", referee_2_id),
            ("match_referee", match_referee_id),
        )
        for role, referee_id in slots:
            if referee_id is None:
                continue
            if (match_id, role) in taken or (match_id, referee_id) in taken:
                continue
            taken.add((match_id, role))
            taken.add((match_id, referee_id))
            bind.execute(
                sa.text(
                    "INSERT INTO match_referee_assignments "
                    "(match_id, referee_id, role, assigned_at) "
                    "VALUES (:match_id, :referee_id, :role, CURRENT_TIMESTAMP)"
                ),
                {"match_id": match_id, "referee_id": referee_id, "role": role},
            )


def downgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_constraint("fk_matches_match_referee_id_referees", type_="foreignkey")
        batch_op.drop_constraint("fk_matches_referee_2_id_referees", type_="foreignkey")
        batch_op.drop_constraint("fk_matches_referee_1_id_referees", type_="foreignkey")
        batch_op.drop_column("match_referee_id")
        batch_op.drop_column("referee_2_id")
        batch_op.drop_column("referee_1_id")

    op.drop_index("ix_match_referee_assignments_referee_id", table_name="match_referee_assignments")
    op.drop_index("ix_match_referee_assignments_match_id", table_name="match_referee_assignments")
    op.drop_table("match_referee_assignments")

    for name, _columns in REFEREE_INDEXES:
        op.drop_index(name, table_name="referees")
    op.drop_table("referees")
