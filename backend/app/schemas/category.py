"""Response model for `GET /categories`."""

import uuid

from pydantic import BaseModel, ConfigDict

from app.models import CategoryKind


class CategoryResponse(BaseModel):
    """One category the user may put a transaction in.

    The endpoint returns the system categories (the seeded defaults, shared
    by everyone) and the user's own in one list, because that is exactly
    the set `PATCH /transactions/{id}` accepts - the client should not have
    to merge two lists to build a dropdown.

    `is_system` is how the client tells them apart: system categories are
    read-only, so the UI must not offer "rename" or "delete" for them
    (user categories CRUD arrives in Phase 2).

    `user_id` is deliberately NOT exposed: it would be either null or the
    caller's own id, so it carries no information the client needs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    # The stable code the rest of the system refers to a category by
    # ("food-dining"); the merchants dictionary and the LLM both use slugs.
    slug: str
    kind: CategoryKind
    parent_id: uuid.UUID | None
    icon: str | None
    color: str | None
    # Read from the `is_system` property on the ORM model (from_attributes
    # takes properties as well as columns).
    is_system: bool
