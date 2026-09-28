"""All ORM models, imported in one place.

Importing a model class registers its table on `Base.metadata`. Alembic
imports this package so that autogenerate sees every table; a model that
isn't imported here is invisible to migrations.
"""

from app.models.category import Category, CategoryKind
from app.models.merchant import Merchant
from app.models.refresh_token import RefreshToken
from app.models.user import DEFAULT_USER_SETTINGS, User

__all__ = [
    "DEFAULT_USER_SETTINGS",
    "Category",
    "CategoryKind",
    "Merchant",
    "RefreshToken",
    "User",
]
