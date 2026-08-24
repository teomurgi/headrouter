"""GET /health and GET /metrics."""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    return {
        "status": "ok",
        "compression": {
            "enabled": state.settings.compression_enabled,
            "engine_available": state.compression.engine_available,
        },
        "routed_models": len(state.settings.routes),
        "providers": sorted(state.settings.known_provider_names()),
    }


@router.get("/metrics")
async def metrics(request: Request):
    return PlainTextResponse(request.app.state.metrics.prometheus(), media_type="text/plain")
