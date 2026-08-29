"""Feature tests for the tray app and gateway launcher desktop logic.

These exercise the behaviour a desktop user depends on: the per-user .env and
providers.json files are created with safe permissions, files open in a text
editor (never the web browser), and the launch command resolves correctly.
GUI side effects (pystray icon, webbrowser, subprocess) are mocked.
"""

from __future__ import annotations

import os
import stat
import sys
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

import gateway_launcher
import tray_app


# --- .env template creation --------------------------------------------------


def test_ensure_env_file_creates_template_with_0600(tmp_path, monkeypatch):
    env_file = tmp_path / "headrouter" / ".env"
    monkeypatch.setattr(tray_app, "ENV_FILE", env_file)

    tray_app._ensure_env_file()

    assert env_file.exists()
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600
    text = env_file.read_text()
    assert "GATEWAY_API_KEYS" in text
    # Template is fully commented out — safe to load as-is.
    for line in text.splitlines():
        line = line.strip()
        assert not line or line.startswith("#")


def test_ensure_env_file_is_idempotent(tmp_path, monkeypatch):
    env_file = tmp_path / "headrouter" / ".env"
    monkeypatch.setattr(tray_app, "ENV_FILE", env_file)
    env_file.parent.mkdir(parents=True)
    env_file.write_text("CUSTOM=1\n", encoding="utf-8")

    tray_app._ensure_env_file()  # must not clobber existing content

    assert env_file.read_text() == "CUSTOM=1\n"


# --- files open in a text editor, not the browser ----------------------------


def test_open_in_text_editor_uses_gui_editor_not_browser(monkeypatch):
    opened = {}
    monkeypatch.setattr(tray_app.subprocess, "Popen",
                        lambda argv, **kw: opened.setdefault("argv", argv))
    monkeypatch.setattr(tray_app.webbrowser, "open",
                        lambda *_a, **_k: opened.setdefault("browser", True))
    # Force the fallback list path: no VISUAL/EDITOR.
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    # Pretend gedit exists.
    monkeypatch.setattr(tray_app.shutil, "which",
                        lambda name: "/usr/bin/gedit" if name == "gedit" else None)
    monkeypatch.setattr(tray_app.sys, "platform", "linux")

    tray_app._open_in_text_editor(Path("/tmp/x/.env"))

    assert "browser" not in opened
    assert opened["argv"] == ["/usr/bin/gedit", "/tmp/x/.env"]


def test_open_in_text_editor_skips_terminal_editors(monkeypatch):
    """A terminal $EDITOR (vim/nano) must not be detached from a tray app."""
    opened = {}
    monkeypatch.setattr(tray_app.subprocess, "Popen",
                        lambda argv, **kw: opened.setdefault("argv", argv))
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("VISUAL", "nano")
    monkeypatch.setattr(tray_app.shutil, "which",
                        lambda name: "/usr/bin/kate" if name == "kate" else ("/usr/bin/vim" if name == "vim" else None))
    monkeypatch.setattr(tray_app.sys, "platform", "linux")

    tray_app._open_in_text_editor(Path("/tmp/x/.env"))

    assert opened["argv"][0] == "/usr/bin/kate"


def test_open_in_text_editor_macos_uses_open_dash_t(monkeypatch):
    opened = {}
    monkeypatch.setattr(tray_app.subprocess, "Popen",
                        lambda argv, **kw: opened.setdefault("argv", argv))
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")

    tray_app._open_in_text_editor(Path("/tmp/x/.env"))

    assert opened["argv"] == ["open", "-t", "/tmp/x/.env"]


# --- providers.json discovery / seeding --------------------------------------


def test_ensure_providers_file_seeds_valid_empty_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "headrouter" / "providers.json"

    result = gateway_launcher._ensure_providers_file()

    assert result == str(target)
    assert target.exists()
    import json as _json
    assert _json.loads(target.read_text()) == {"providers": [], "keys": []}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_ensure_providers_file_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "headrouter" / "providers.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"providers": [{"id": "x"}], "keys": []}')

    gateway_launcher._ensure_providers_file()

    assert '"id": "x"' in target.read_text()


# --- launch command resolution ------------------------------------------------


def _args(**kw):
    base = dict(module=False, python=None, port=8000)
    base.update(kw)
    return Namespace(**base)


def test_resolve_launch_module_mode():
    args = _args(module=True, port=9000)
    cmd = tray_app._resolve_launch(args)
    assert cmd[1:] == ["-m", "uvicorn", "app:app", "--port", "9000"]


def test_resolve_launch_frozen_gateway_binary(tmp_path, monkeypatch):
    fake_bin = tmp_path / "headrouter-gateway"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    # Point the candidate search at our tmp dir.
    monkeypatch.setattr(tray_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tray_app.sys, "executable", str(tmp_path / "headrouter-tray"))

    cmd = tray_app._resolve_launch(_args())

    assert cmd[0] == str(fake_bin.resolve())
    assert cmd[1:] == ["--port", "8000"]


# --- PyInstaller env leakage ---------------------------------------------------
#
# Root cause of "Open logs / Edit environment do nothing" on the packaged
# Ubuntu build: PyInstaller's runtime hooks inject LD_LIBRARY_PATH,
# GI_TYPELIB_PATH, GTK_PATH, etc. into the *frozen tray process itself* at
# runtime (they never appear in /proc/<tray>/environ, which is why the bug was
# invisible to `env`-based debugging). Any system GUI binary spawned with that
# inherited environment dynamically links against the bundled (older) glib and
# dies instantly with e.g. "symbol lookup error: ... g_sort_array".


def test_clean_child_env_strips_pyinstaller_vars_when_frozen(monkeypatch):
    monkeypatch.setattr(tray_app.sys, "frozen", True, raising=False)
    dirty = {
        "LD_LIBRARY_PATH": "/tmp/_MEIxxxx",
        "GI_TYPELIB_PATH": "/tmp/_MEIxxxx/gi_typelibs",
        "GIO_MODULE_DIR": "/tmp/_MEIxxxx/gio_modules",
        "GTK_PATH": "/tmp/_MEIxxxx/gtk",
        "XDG_DATA_DIRS": "/tmp/_MEIxxxx/share",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/u",
    }
    clean = tray_app._clean_child_env(dirty)
    for var in ("LD_LIBRARY_PATH", "GI_TYPELIB_PATH", "GIO_MODULE_DIR", "GTK_PATH", "XDG_DATA_DIRS"):
        assert var not in clean, f"{var} leaked into child env"
    # Normal variables are preserved untouched.
    assert clean["PATH"] == "/usr/bin:/bin"
    assert clean["HOME"] == "/home/u"


def test_clean_child_env_restores_orig_values(monkeypatch):
    """PyInstaller saves the user's real values in *_ORIG; restore them."""
    monkeypatch.setattr(tray_app.sys, "frozen", True, raising=False)
    dirty = {
        "LD_LIBRARY_PATH": "/tmp/_MEIxxxx",
        "LD_LIBRARY_PATH_ORIG": "/opt/myapp/lib",
        "LD_PRELOAD": "/tmp/_MEIxxxx/preload.so",
        "LD_PRELOAD_ORIG": "/opt/myapp/preload.so",
    }
    clean = tray_app._clean_child_env(dirty)
    assert clean["LD_LIBRARY_PATH"] == "/opt/myapp/lib"
    assert clean["LD_PRELOAD"] == "/opt/myapp/preload.so"
    assert "LD_LIBRARY_PATH_ORIG" not in clean
    assert "LD_PRELOAD_ORIG" not in clean


def test_clean_child_env_noop_when_not_frozen(monkeypatch):
    monkeypatch.delattr(tray_app.sys, "frozen", raising=False)
    env = {"LD_LIBRARY_PATH": "/opt/myapp/lib", "PATH": "/usr/bin"}
    # Unfrozen (source) runs must not have their environment rewritten.
    assert tray_app._clean_child_env(env) == env


def test_spawn_passes_sanitized_env(monkeypatch):
    """The helper spawn path must use _clean_child_env, not os.environ."""
    monkeypatch.setattr(tray_app.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.setenv("GI_TYPELIB_PATH", "/tmp/_MEIxxxx/gi_typelibs")
    captured = {}
    monkeypatch.setattr(
        tray_app.subprocess, "Popen",
        lambda argv, **kw: captured.update(argv=argv, env=kw.get("env")),
    )

    tray_app._spawn(["/usr/bin/gedit", "/tmp/x/.env"])

    assert captured["argv"] == ["/usr/bin/gedit", "/tmp/x/.env"]
    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert "GI_TYPELIB_PATH" not in captured["env"]


def test_open_in_text_editor_spawns_with_clean_env(monkeypatch):
    """End-to-end: opening a file must not leak PyInstaller vars to the editor."""
    monkeypatch.setattr(tray_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.setattr(tray_app.shutil, "which",
                        lambda name: "/usr/bin/gedit" if name == "gedit" else None)
    captured = {}
    monkeypatch.setattr(
        tray_app.subprocess, "Popen",
        lambda argv, **kw: captured.update(argv=argv, env=kw.get("env")),
    )

    tray_app._open_in_text_editor(Path("/tmp/x/.env"))

    assert captured["argv"] == ["/usr/bin/gedit", "/tmp/x/.env"]
    assert "LD_LIBRARY_PATH" not in captured["env"]


# --- tray menu wiring ----------------------------------------------------------


def _make_tray():
    with mock.patch.object(tray_app.pystray, "Icon"):
        return tray_app.GatewayTray("127.0.0.1", 8123, ["true"], autostart=False)


def _menu_item(tray, label):
    for item in tray._menu():
        try:
            if item.text == label:
                return item
        except Exception:
            continue  # separators have no text
    raise AssertionError(f"menu item {label!r} not found")


def test_menu_has_logs_and_environment_entries():
    """The menu must expose 'Open logs' and 'Edit environment' entries."""
    tray = _make_tray()
    _menu_item(tray, "Open logs")
    _menu_item(tray, "Edit environment")


def test_menu_open_logs_invokes_editor_on_log_file(monkeypatch):
    """Clicking 'Open logs' must open LOG_FILE in the text editor."""
    opened = {}
    monkeypatch.setattr(tray_app, "_open_in_text_editor",
                        lambda p: opened.setdefault("path", p))
    tray = _make_tray()
    item = _menu_item(tray, "Open logs")

    item._action(tray.icon, item)  # simulate a menu click

    assert opened["path"] == tray_app.LOG_FILE


def test_menu_edit_environment_creates_and_opens_env_file(tmp_path, monkeypatch):
    """Clicking 'Edit environment' must ensure the .env exists, then open it."""
    env_file = tmp_path / "headrouter" / ".env"
    monkeypatch.setattr(tray_app, "ENV_FILE", env_file)
    opened = {}
    monkeypatch.setattr(tray_app, "_open_in_text_editor",
                        lambda p: opened.setdefault("path", p))
    tray = _make_tray()
    item = _menu_item(tray, "Edit environment")

    item._action(tray.icon, item)

    assert env_file.exists()
    assert opened["path"] == env_file
