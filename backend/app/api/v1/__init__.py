"""Collects every v1 router into one `api_router`, mounted at /api/v1 in main.py.

Adding a feature later = write app/api/v1/<feature>.py and include it here.
"""

from fastapi import APIRouter

from app.api.v1 import auth, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
