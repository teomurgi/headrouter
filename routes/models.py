"""GET /v1/models — list configured gateway model aliases."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    settings = request.app.state.settings
    ids = list(settings.routes.keys())
    if settings.default_route is not None and "default" not in ids:
        ids.append("default")
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "headrouter"}
            for mid in ids
        ],
    }
