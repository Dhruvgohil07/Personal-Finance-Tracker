"""All ORM models, imported in one place.

Importing a model class registers its table on `Base.metadata`. Alembic
imports this package so that autogenerate sees every table; a model that
isn't imported here is invisible to migrations.
"""

from app.models.category import Category, CategoryKind
from app.models.merchant import Merchant

__all__ = ["Category", "CategoryKind", "Merchant"]
