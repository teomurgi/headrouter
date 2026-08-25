"""GET /v1/models — list the models available to the authenticated key (INV-2)."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    settings = request.app.state.settings
    ids = settings.models_for_key(getattr(request.state, "gateway_key", None))
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "headrouter"}
            for mid in ids
        ],
    }
