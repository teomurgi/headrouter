from .api import api_router
from .proxy import router as proxy_router

api_router.include_router(proxy_router)

__all__ = ["api_router"]
