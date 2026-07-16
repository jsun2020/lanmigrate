"""Startup self-check (PRD F11)."""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from lanmigrate import preflight, rclone_bin


def test_is_admin_returns_bool():
    assert isinstance(preflight.is_admin(), bool)


def test_port_available_on_free_port():
    with socket.socket() as probe:
        probe.bind(("", 0))
        free_port = probe.getsockname()[1]
    assert preflight.port_available(free_port) is True


def test_port_available_false_when_occupied():
    with socket.socket() as holder:
        holder.bind(("", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert preflight.port_available(port) is False


def test_rclone_source_missing(monkeypatch):
    monkeypatch.setattr(rclone_bin, "find_rclone", lambda: None)
    assert preflight.rclone_source() == ("missing", None)


def test_rclone_source_local(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / rclone_bin._exe_name()
    monkeypatch.setattr(rclone_bin, "find_rclone", lambda: exe)
    monkeypatch.setattr(rclone_bin, "default_bin_dir", lambda: tmp_path / "bin")
    monkeypatch.delenv("LANMIGRATE_RCLONE", raising=False)
    assert preflight.rclone_source() == ("local", exe)


def test_rclone_source_env(monkeypatch, tmp_path):
    exe = tmp_path / "custom" / rclone_bin._exe_name()
    monkeypatch.setattr(rclone_bin, "find_rclone", lambda: exe)
    monkeypatch.setenv("LANMIGRATE_RCLONE", str(exe))
    assert preflight.rclone_source() == ("env", exe)


def _fake_netsh(monkeypatch, calls, returncode=0):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")
    monkeypatch.setattr(preflight.subprocess, "run", run)


def test_ensure_firewall_rule_idempotent(monkeypatch):
    """Existing rule: no add command issued."""
    calls: list[list[str]] = []
    _fake_netsh(monkeypatch, calls, returncode=0)  # show rule -> found
    ok, msg = preflight.ensure_firewall_rule(2022)
    assert ok is True
    assert len(calls) == 1 and "show" in calls[0]


def test_ensure_firewall_rule_adds_when_missing(monkeypatch):
    seen: list[list[str]] = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        # show -> not found (rc 1); add -> success (rc 0)
        rc = 1 if "show" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", run)
    ok, msg = preflight.ensure_firewall_rule(2022)
    assert ok is True
    add_cmd = seen[-1]
    assert "add" in add_cmd and "localport=2022" in add_cmd


def test_check_report_shape(monkeypatch):
    monkeypatch.setattr(preflight, "firewall_rule_exists", lambda port: True)
    monkeypatch.setattr(
        rclone_bin, "find_rclone", lambda: Path("C:/x/rclone.exe"))
    report = preflight.check(port=2022)
    assert set(report) == {
        "admin", "rclone", "rclone_source", "port", "port_free", "firewall_rule"}
    assert report["port"] == 2022
    assert isinstance(report["admin"], bool)
