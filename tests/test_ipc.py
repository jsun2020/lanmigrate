"""IPC session tests: protocol dispatch + scan/task methods (PRD M3).

Session is driven in-process with a captured emit callback; no Tauri, no
subprocess. Network/rclone-touching methods (start_send, receive) are only
tested for their guard paths here - the transfer loop itself reuses the
e2e-verified engine functions.
"""
from pathlib import Path

import pytest

from lanmigrate import ipc, taskstore


@pytest.fixture
def session():
    events: list[dict] = []
    return ipc.Session(events.append), events


def call(sess_events, method, params=None, rid=1):
    sess, events = sess_events
    sess.handle({"id": rid, "method": method, "params": params or {}})
    reply = next(e for e in reversed(events) if e.get("id") == rid)
    return reply


def test_ping(session):
    reply = call(session, "ping")
    assert reply["ok"] is True
    assert reply["result"]["version"]


def test_prepare_reports_admin_without_download(session, monkeypatch, tmp_path):
    """With rclone already present (bundled/local), prepare never downloads
    and reports the admin flag for the GUI hint (PRD F10/F11)."""
    from lanmigrate import rclone_bin

    exe = tmp_path / "rclone.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(rclone_bin, "find_rclone", lambda: exe)
    reply = call(session, "prepare")
    assert reply["ok"] is True
    r = reply["result"]
    assert r["downloaded"] is False
    assert isinstance(r["admin"], bool)


def test_start_receive_rejects_busy_port(session, monkeypatch):
    from lanmigrate import preflight

    monkeypatch.setattr(preflight, "port_available", lambda port: False)
    reply = call(session, "start_receive",
                 {"directory": "~/Migration", "port": 2022})
    assert reply["ok"] is False
    assert "被占用" in reply["error"]


def test_unknown_method(session):
    reply = call(session, "no_such_method")
    assert reply["ok"] is False
    assert "unknown method" in reply["error"]


def test_bad_params_reported_not_fatal(session):
    reply = call(session, "scan", {"bogus_arg": 1})
    assert reply["ok"] is False
    # loop must survive: a later request still works
    assert call(session, "ping", rid=2)["ok"] is True


def test_scan_missing_dir(session):
    reply = call(session, "scan", {"path": "Z:/definitely/not/here"})
    assert reply["ok"] is False
    assert "目录不存在" in reply["error"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    node = tmp_path / "proj"
    (node / "node_modules" / "x").mkdir(parents=True)
    (node / "node_modules" / "x" / "i.js").write_bytes(b"x" * 100)
    (node / "package.json").write_text("{}")
    (node / "app.js").write_bytes(b"y" * 10)
    return tmp_path


def test_scan_fast_result_shape(session, tree):
    reply = call(session, "scan", {"path": str(tree)})
    assert reply["ok"] is True
    r = reply["result"]
    assert r["file_count"] == 2  # package.json + app.js (excluded dir not walked)
    assert r["exclusions"] == [{"rel": "proj/node_modules",
                                "rule": "Node.js", "size": -1}]
    assert r["total_bytes"] == 0  # fast scan: sizes unknown


def test_scan_full_computes_sizes(session, tree):
    reply = call(session, "scan", {"path": str(tree), "full": True})
    r = reply["result"]
    assert r["exclusions"][0]["size"] == 100
    assert r["saved_bytes"] == 100
    assert r["total_bytes"] == 112  # 100 excluded + 10 app.js + 2 package.json


def test_start_send_requires_scan(session):
    reply = call(session, "start_send",
                 {"source": "C:/anything", "host": "1.2.3.4", "code": "123456"})
    assert reply["ok"] is False
    assert "扫描" in reply["error"]


def test_cancel_send_idle_is_safe(session):
    reply = call(session, "cancel_send")
    assert reply["ok"] is True
    assert reply["result"]["cancelled"] is False


def test_stop_receive_idle_is_safe(session):
    reply = call(session, "stop_receive")
    assert reply["ok"] is True
    assert reply["result"]["stopped"] is False


def test_list_tasks_and_latest(session, tmp_path, monkeypatch):
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    reply = call(session, "list_tasks")
    assert reply["result"]["tasks"] == []
    assert call(session, "latest_incomplete", rid=2)["result"]["task"] is None

    task = taskstore.MigrationTask(
        task_id="t1", source="C:/src", host="10.0.0.2", port=2022,
        user="lanmigrate", obscured_pass="xx")
    taskstore.save(task)
    tasks = call(session, "list_tasks", rid=3)["result"]["tasks"]
    assert len(tasks) == 1 and tasks[0]["task_id"] == "t1"
    latest = call(session, "latest_incomplete", rid=4)["result"]["task"]
    assert latest["task_id"] == "t1"


def test_resume_without_tasks(session, tmp_path, monkeypatch):
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    reply = call(session, "resume")
    assert reply["ok"] is False
    assert "没有未完成的任务" in reply["error"]


def test_sync_without_tasks(session, tmp_path, monkeypatch):
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    reply = call(session, "sync")
    assert reply["ok"] is False
    assert "没有可同步的任务" in reply["error"]


def test_sync_reruns_done_task_in_update_mode(session, tmp_path, monkeypatch):
    """sync must pick a DONE task (resume won't), reset rounds, and start
    the worker with update=True."""
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    task = taskstore.MigrationTask(
        task_id="t-done", source="C:/src", host="10.0.0.2", port=2022,
        user="lanmigrate", obscured_pass="xx",
        status=taskstore.STATUS_DONE, rounds_completed=3)
    taskstore.save(task)

    sess, _events = session
    started = {}

    def fake_worker(t, max_rounds, wait, update=False):
        started.update(task=t, update=update)

    monkeypatch.setattr(sess, "_start_worker", fake_worker)
    reply = call(session, "sync")
    assert reply["ok"] is True
    assert reply["result"]["task"]["task_id"] == "t-done"
    assert started["update"] is True
    assert started["task"].rounds_completed == 0  # fresh pass
