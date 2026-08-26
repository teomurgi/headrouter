"""Frozen-binary entry point for the Headrouter gateway.

Used as the PyInstaller entry script for the self-contained gateway binary
that the tray app spawns. Wraps uvicorn programmatically so the frozen app
does not depend on the uvicorn CLI console script.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _ensure_providers_file() -> str:
    """Resolve the providers.json the gateway edits at runtime.

    Packaged installs run with an unpredictable (often read-only) cwd, so the
    persistent config lives in a per-user XDG path. Seed it from the bundled
    example template on first run so the admin UI has a file to edit.
    """
    cfg_dir = _xdg_config_home() / "headrouter"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / "providers.json"
    if not target.exists():
        # Seed an empty, valid config. The example template references env vars
        # that are not set, which would fail validation on first boot; users
        # build their config via the admin UI.
        target.write_text('{"providers": [], "keys": []}\n', encoding="utf-8")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Headrouter gateway server")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--no-prefetch", action="store_true", help="skip compression model prefetch")
    args = p.parse_args(argv)

    if args.no_prefetch:
        os.environ["COMPRESSION_PREFETCH_ENABLED"] = "0"

    # Default to a per-user, writable providers.json unless the caller pointed
    # the gateway at one explicitly.
    if not (os.environ.get("GATEWAY_PROVIDERS_FILE") or os.environ.get("GATEWAY_PROVIDERS")):
        os.environ["GATEWAY_PROVIDERS_FILE"] = _ensure_providers_file()

    uvicorn.run(
        "app:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
