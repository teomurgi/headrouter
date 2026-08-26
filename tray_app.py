"""Headrouter tray frontend.

A small system-tray / app-indicator that manages the Headrouter gateway as a
child process. Runs on the *system* Python (which has the AppIndicator / gi
bindings); the gateway itself runs on the project interpreter that has
headroom-ai / onnxruntime installed.

Run from source (Linux):
    /usr/bin/python3 tray_app.py --python .venv/bin/python

Resolution order for the gateway launch (first match wins):
    1. --python / --module explicit flags
    2. a frozen gateway binary next to this app (packaged installs)
    3. HEADROUTER_PYTHON env var
    4. <repo>/.venv/bin/python (source checkouts)
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import pystray
from pystray import MenuItem
from PIL import Image, ImageDraw

APP_NAME = "Headrouter"
HEALTH_PATH = "/health"
ADMIN_PATH = "/admin"
HELP_PATH = "/help"

_state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "headrouter"
_state_dir.mkdir(parents=True, exist_ok=True)
LOG_FILE = _state_dir / "gateway.log"
def _resource_base() -> Path:
    """Directory that bundled data files (icon) live in.

    Frozen (PyInstaller) apps extract data into sys._MEIPASS; from source the
    repo root (this file's directory) is used.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


ICON_PATH = _resource_base() / "static" / "icon.png"
ICON_STOPPED_PATH = _resource_base() / "static" / "icon-stopped.png"


def _load_icon(path: Path | None = None) -> Image.Image:
    path = path or ICON_PATH
    if path.exists():
        return Image.open(path).convert("RGBA")
    # Fallback: draw a simple droplet so the tray always has an image.
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 18, 54, 58), fill=(56, 132, 255, 255))
    d.polygon([(32, 4), (14, 30), (50, 30)], fill=(56, 132, 255, 255))
    return img


def _greyed(img: Image.Image) -> Image.Image:
    """Grayscale + dim a copy of an image for the stopped state."""
    img = img.convert("RGBA")
    gray = img.convert("L")
    r, g, b = gray, gray, gray
    a = img.getchannel("A").point(lambda v: int(v * 0.55))
    return Image.merge("RGBA", (r, g, b, a))


class GatewayTray:
    def __init__(self, host: str, port: int, launch_cmd: list[str], autostart: bool):
        self.host = host
        self.port = port
        self.launch_cmd = launch_cmd
        self.autostart = autostart
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._log_fh = None
        self._icon_running = _load_icon(ICON_PATH)
        # Prefer a pre-generated grey icon; fall back to deriving one.
        if ICON_STOPPED_PATH.exists():
            self._icon_stopped = _load_icon(ICON_STOPPED_PATH)
        else:
            self._icon_stopped = _greyed(self._icon_running)
        self.icon = pystray.Icon(APP_NAME, self._icon_running, APP_NAME, menu=self._menu())

    # ----- gateway lifecycle -------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._log_fh = open(LOG_FILE, "ab", buffering=0)
            env = dict(os.environ)
            env.setdefault("HOST", self.host)
            env.setdefault("PORT", str(self.port))
            try:
                self.proc = subprocess.Popen(
                    self.launch_cmd,
                    stdout=self._log_fh,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,  # own process group for clean shutdown
                )
            except Exception as exc:  # surface in the tooltip/title
                self.proc = None
                self._log_fh.close()
                self._log_fh = None
                self.icon.title = f"{APP_NAME}: failed to start ({exc})"
                return
            proc = self.proc
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        self._refresh()

    def stop(self) -> None:
        with self._lock:
            if not self.is_running():
                return
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self.proc = None
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
        self._refresh()

    def restart(self) -> None:
        self.stop()
        # give the port a moment to free up
        time.sleep(0.4)
        self.start()

    def _watch(self, proc: subprocess.Popen) -> None:
        proc.wait()
        with self._lock:
            if proc is self.proc:  # not superseded by a restart
                self.proc = None
        self._refresh()

    # ----- menu ---------------------------------------------------------------
    def _status(self) -> str:
        if self.is_running():
            return f"{APP_NAME}: running on :{self.port}"
        return f"{APP_NAME}: stopped"

    def _refresh(self) -> None:
        self.icon.title = self._status()
        self.icon.icon = self._icon_running if self.is_running() else self._icon_stopped
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            MenuItem(lambda _i: self._status(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            MenuItem(
                "Open admin UI",
                lambda _i, _it: webbrowser.open(self.base_url + ADMIN_PATH),
                enabled=lambda _i: self.is_running(),
            ),
            MenuItem(
                "Open help",
                lambda _i, _it: webbrowser.open(self.base_url + HELP_PATH),
                enabled=lambda _i: self.is_running(),
            ),
            MenuItem(
                "Open logs",
                lambda _i, _it: webbrowser.open(LOG_FILE.as_uri()),
            ),
            pystray.Menu.SEPARATOR,
            MenuItem(
                "Start", lambda _i, _it: self.start(),
                enabled=lambda _i: not self.is_running(),
            ),
            MenuItem(
                "Stop", lambda _i, _it: self.stop(),
                enabled=lambda _i: self.is_running(),
            ),
            MenuItem(
                "Restart", lambda _i, _it: self.restart(),
                enabled=lambda _i: self.is_running(),
            ),
            pystray.Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )

    def _quit(self, icon, _item) -> None:
        self.stop()
        icon.stop()

    # ----- entry ---------------------------------------------------------------
    def run(self) -> None:
        if self.autostart:
            self.start()
        self._refresh()
        self.icon.run()  # blocks on the main loop


def _resolve_launch(args) -> list[str]:
    # In a frozen (PyInstaller) binary, __file__ points into the temporary
    # _MEIPASS extraction dir, not the binary's location. Resolve the gateway
    # relative to the actual executable when frozen; relative to the source
    # file otherwise.
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
    else:
        here = Path(__file__).resolve().parent
    if args.module:
        return [args.python or sys.executable, "-m", "uvicorn", "app:app", "--port", str(args.port)]
    # Packaged install: a frozen gateway binary shipped alongside the tray.
    # Linux: same dir as the tray binary, or a sibling bin/ dir.
    # macOS .app: executable is Contents/MacOS/headrouter-tray; the gateway is
    # embedded under Contents/bin or Contents/Resources/bin.
    candidates = [
        here / "headrouter-gateway",
        here.parent / "bin" / "headrouter-gateway",
        here / ".." / "bin" / "headrouter-gateway",          # Contents/bin
        here / ".." / "Resources" / "bin" / "headrouter-gateway",  # Contents/Resources/bin
    ]
    for cand in candidates:
        cand = cand.resolve()
        if cand.exists() and os.access(cand, os.X_OK):
            return [str(cand), "--port", str(args.port)]
    python = args.python or os.environ.get("HEADROUTER_PYTHON") or str(here / ".venv" / "bin" / "python")
    return [python, "-m", "uvicorn", "app:app", "--port", str(args.port)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Headrouter tray controller")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--python", help="interpreter used to run the gateway (source mode)")
    p.add_argument("--module", action="store_true", help="force 'python -m uvicorn app:app' launch")
    p.add_argument("--no-autostart", action="store_true", help="do not start the gateway on launch")
    args = p.parse_args(argv)

    cmd = _resolve_launch(args)
    tray = GatewayTray(args.host, args.port, cmd, autostart=not args.no_autostart)
    tray.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
