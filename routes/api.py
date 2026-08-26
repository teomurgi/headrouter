from fastapi import APIRouter

from .chat import router as chat_router
from .health import router as health_router
from .models import router as models_router
from .admin import router as admin_router

api_router = APIRouter()
api_router.include_router(chat_router)
api_router.include_router(models_router)
api_router.include_router(health_router)
api_router.include_router(admin_router)

__all__ = ["api_router"]
