"""All ORM models, imported in one place.

Importing a model class registers its table on `Base.metadata`. Alembic
imports this package so that autogenerate sees every table; a model that
isn't imported here is invisible to migrations.
"""

from app.models.account import Account, AccountType, BankCode
from app.models.category import Category, CategoryKind
from app.models.merchant import Merchant
from app.models.refresh_token import RefreshToken
from app.models.statement_upload import StatementUpload, UploadStatus
from app.models.transaction import CategorySource, Channel, Transaction
from app.models.user import DEFAULT_USER_SETTINGS, User

__all__ = [
    "Account",
    "AccountType",
    "BankCode",
    "DEFAULT_USER_SETTINGS",
    "Category",
    "CategoryKind",
    "CategorySource",
    "Channel",
    "Merchant",
    "RefreshToken",
    "StatementUpload",
    "Transaction",
    "UploadStatus",
    "User",
]
