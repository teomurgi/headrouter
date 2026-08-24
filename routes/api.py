from .chat import router as chat_router
from .health import router as health_router
from .models import router as models_router

api_router = chat_router
api_router.include_router(models_router)
api_router.include_router(health_router)

__all__ = ["api_router"]
