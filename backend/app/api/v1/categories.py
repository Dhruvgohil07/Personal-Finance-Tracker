"""GET /api/v1/categories.

Read-only in Phase 1, and it exists for one concrete reason:
`PATCH /transactions/{id}` needs a `category_id`, and this is where the
client finds one. Creating, renaming and deleting your own categories
(SPEC §10) arrives in Phase 2, which is why there is no POST/PATCH/DELETE
here yet.

The list is small (25 seeded system categories plus whatever the user adds
later), so it is returned whole - no pagination.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.category import CategoryResponse
from app.services import categories as categories_service

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get(
    "",
    response_model=list[CategoryResponse],
    summary="List the categories you can use",
)
def list_categories(user: CurrentUser, db: DbSession) -> list[CategoryResponse]:
    """The system categories plus your own, grouped by kind and named.

    `is_system` is true for the shared defaults, which are read-only.
    """
    categories = categories_service.list_categories(db, user.id)
    return [CategoryResponse.model_validate(category) for category in categories]
