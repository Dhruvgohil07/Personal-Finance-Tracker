"""The SQLAlchemy declarative `Base` class and shared column helpers.

Every ORM model (one Python class per table) inherits from `Base`:

    class Category(Base):
        __tablename__ = "categories"
        ...

`Base.metadata` is then a registry of ALL tables. Alembic compares that
registry with the real database to autogenerate migrations, so every model
module must be imported before Alembic looks at it (see `app/models/__init__.py`).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, MetaData, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming convention for constraints and indexes.
#
# Without it, Postgres invents names like "categories_slug_key", and Alembic
# can't reliably find a constraint again later to drop or change it. With it,
# every constraint gets a predictable name, e.g.
#   uq_categories_user_id_slug, ck_categories_kind, fk_categories_parent_id_categories
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- Reusable columns -------------------------------------------------------


def str_enum_column(enum_cls: type[StrEnum], name: str) -> Enum:
    """Column type for a Python StrEnum, stored as VARCHAR + CHECK (ADR 002).

    Usage: `kind: Mapped[CategoryKind] = mapped_column(str_enum_column(CategoryKind, "kind"))`

    - native_enum=False: a VARCHAR column, not a Postgres ENUM type
    - create_constraint=True: plus a CHECK (column IN (...)) constraint,
      named ck_<table>_<name> by our naming convention
    - values_callable: store the enum *values* ("expense"), not the member
      names ("EXPENSE")
    One shared definition, so every enum column in the schema behaves the same.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        name=name,
        length=20,
        values_callable=lambda cls: [member.value for member in cls],
    )


# Mixins are small classes whose columns get copied into every model that
# inherits from them, so we don't repeat these definitions in every table.


class UUIDPrimaryKeyMixin:
    # default=uuid.uuid4: the ORM generates the id in Python (so we know it
    #   before the INSERT runs).
    # server_default=gen_random_uuid(): the database generates one too, for
    #   rows inserted with raw SQL (e.g. in a migration).
    # sort_order=-1: put `id` first in CREATE TABLE (mixin columns go last otherwise).
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
        sort_order=-1,
    )


class TimestampMixin:
    # timezone=True -> Postgres `timestamptz` (stored in UTC, never ambiguous).
    # server_default=now(): the database fills the value on INSERT.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # onupdate=now(): the ORM sets it on every UPDATE it performs.
    # Note: an UPDATE written in raw SQL must set updated_at itself.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
