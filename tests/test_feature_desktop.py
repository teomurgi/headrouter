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
