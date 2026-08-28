"""Feature tests for the console-script entry points.

The packaged CLI is `headrouter-gateway` (gateway_launcher:main) and
`headrouter-tray` (tray_app:main). These tests verify argument parsing and
how the parsed args wire into the runtime, without starting real servers.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

import gateway_launcher
import tray_app


def test_gateway_launcher_defaults_to_localhost_without_keys(monkeypatch, tmp_path):
    """With no gateway keys configured, the launcher must not expose 0.0.0.0."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("GATEWAY_API_KEYS", "GATEWAY_PROVIDERS_FILE", "GATEWAY_PROVIDERS", "HOST"):
        monkeypatch.delenv(var, raising=False)

    captured = {}

    def fake_run(app, host, port, **kw):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(gateway_launcher.uvicorn, "run", fake_run)

    gateway_launcher.main([])

    assert captured["host"] == "127.0.0.1"


def test_gateway_launcher_binds_all_interfaces_with_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GATEWAY_API_KEYS", "hr_secret")
    monkeypatch.delenv("HOST", raising=False)

    captured = {}
    monkeypatch.setattr(gateway_launcher.uvicorn, "run",
                        lambda app, host, port, **kw: captured.update(host=host))

    gateway_launcher.main([])

    assert captured["host"] == "0.0.0.0"


def test_gateway_launcher_no_prefetch_sets_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("COMPRESSION_PREFETCH_ENABLED", raising=False)
    monkeypatch.setattr(gateway_launcher.uvicorn, "run", lambda *a, **k: None)

    gateway_launcher.main(["--no-prefetch"])

    assert os.environ["COMPRESSION_PREFETCH_ENABLED"] == "0"


def test_gateway_launcher_explicit_port(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured = {}
    monkeypatch.setattr(gateway_launcher.uvicorn, "run",
                        lambda app, host, port, **kw: captured.update(port=port))

    gateway_launcher.main(["--port", "9100"])

    assert captured["port"] == 9100


def test_tray_main_builds_tray_with_launch_cmd(monkeypatch):
    """tray main() parses args and constructs a GatewayTray with the launch cmd."""
    built = {}

    class FakeTray:
        def __init__(self, host, port, launch_cmd, autostart):
            built.update(host=host, port=port, autostart=autostart, launch_cmd=launch_cmd)

        def run(self):
            built["ran"] = True

    monkeypatch.setattr(tray_app, "GatewayTray", FakeTray)
    monkeypatch.setattr(tray_app, "_resolve_launch", lambda args: ["echo", "gw"])

    rc = tray_app.main(["--port", "9200", "--no-autostart"])

    assert rc == 0
    assert built["port"] == 9200
    assert built["autostart"] is False
    assert built["launch_cmd"] == ["echo", "gw"]
    assert built["ran"] is True


def test_tray_main_autostart_by_default(monkeypatch):
    built = {}

    class FakeTray:
        def __init__(self, host, port, launch_cmd, autostart):
            built["autostart"] = autostart

        def run(self):
            pass

    monkeypatch.setattr(tray_app, "GatewayTray", FakeTray)
    monkeypatch.setattr(tray_app, "_resolve_launch", lambda args: ["echo"])

    tray_app.main([])

    assert built["autostart"] is True
