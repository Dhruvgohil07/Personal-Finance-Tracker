"""Global merchant dictionary (SPEC §5 `merchants`).

Maps a normalized merchant key found in narrations (e.g. "ZOMATO") to a
display name and a default category. Shared by all users, so it must never
contain personal data (no people's names, no UPI IDs of individuals).
"""

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Merchant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchants"

    # unique=True creates a unique constraint named uq_merchants_normalized_key.
    normalized_key: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    # A slug, not a foreign key: slugs are only unique per user, so there is
    # no single row to point at. A test checks every seeded slug exists.
    default_category_slug: Mapped[str]

    def __repr__(self) -> str:
        return f"Merchant(normalized_key={self.normalized_key!r})"
