"""IPC session tests: protocol dispatch + scan/task methods (PRD M3).

Session is driven in-process with a captured emit callback; no Tauri, no
subprocess. Network/rclone-touching methods (start_send, receive) are only
tested for their guard paths here - the transfer loop itself reuses the
e2e-verified engine functions.
"""
import threading
import time
from pathlib import Path

import pytest

from lanmigrate import ipc, taskstore


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


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


def test_start_receive_uses_custom_port(session, tmp_path, monkeypatch):
    """PRD F13: a standard user whose firewall blocks 2022 can receive on a
    port that is already allowed (e.g. 8080). The chosen port must reach the
    SFTP server, the mDNS announcement, and the firewall check alike."""
    import threading

    from lanmigrate import discovery, engine, preflight

    class FakeProc:
        def __init__(self):
            self._done = threading.Event()

        def poll(self):
            return 0 if self._done.is_set() else None

        def wait(self):
            self._done.wait()
            return 0

        def terminate(self):
            self._done.set()

    seen = {}
    monkeypatch.setattr(preflight, "port_available", lambda port: True)
    monkeypatch.setattr(preflight, "is_admin", lambda: False)

    def fake_fw(port):
        seen["firewall"] = port
        return True

    def fake_serve(directory, port, user, password, capture_log=False):
        seen["serve"] = port
        return FakeProc()

    class FakeAnnouncer:
        def __init__(self, port, fingerprint, name=None):
            seen["announce"] = port

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(preflight, "firewall_rule_exists", fake_fw)
    monkeypatch.setattr(engine, "serve_sftp", fake_serve)
    monkeypatch.setattr(discovery, "Announcer", FakeAnnouncer)
    monkeypatch.setattr(discovery, "local_ip", lambda: "10.0.0.5")

    reply = call(session, "start_receive",
                 {"directory": str(tmp_path / "in"), "port": 8080,
                  "code": "123456"})
    assert reply["ok"] is True
    assert reply["result"]["port"] == 8080
    assert seen == {"firewall": 8080, "serve": 8080, "announce": 8080}
    assert call(session, "stop_receive", rid=2)["ok"] is True


class _FakeServeProc:
    """Stands in for the rclone serve sftp Popen. `lines` feeds the stderr
    log parser; wait() blocks until terminate() like the real server."""

    def __init__(self, lines=None, dead=False):
        self._done = threading.Event()
        if dead:
            self._done.set()
        self.stderr = list(lines) if lines is not None else None

    def poll(self):
        return 1 if self._done.is_set() else None

    def wait(self):
        self._done.wait()
        return 1

    def terminate(self):
        self._done.set()


class _RecordingAnnouncer:
    def __init__(self, port, fingerprint, name=None):
        self.port = port
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _patch_receive(monkeypatch, serve):
    from lanmigrate import discovery, engine, preflight

    announcers = []

    def make_announcer(port, fingerprint, name=None):
        ann = _RecordingAnnouncer(port, fingerprint, name)
        announcers.append(ann)
        return ann

    monkeypatch.setattr(preflight, "port_available", lambda port: True)
    monkeypatch.setattr(preflight, "is_admin", lambda: False)
    monkeypatch.setattr(preflight, "firewall_rule_exists", lambda port: True)
    monkeypatch.setattr(engine, "serve_sftp", serve)
    monkeypatch.setattr(discovery, "Announcer", make_announcer)
    monkeypatch.setattr(discovery, "local_ip", lambda: "10.0.0.5")
    return announcers


def test_receive_restart_after_stop(session, tmp_path, monkeypatch):
    """PRD F15: stop -> start must work in the same session (no app restart).
    The first announcer must be fully stopped before the second registers,
    or zeroconf raises NonUniqueNameException."""
    announcers = _patch_receive(
        monkeypatch,
        lambda d, p, u, pw, capture_log=False: _FakeServeProc())
    assert call(session, "start_receive",
                {"directory": str(tmp_path / "in"), "port": 2022})["ok"]
    assert call(session, "stop_receive", rid=2)["ok"]
    assert announcers[0].stopped is True
    reply = call(session, "start_receive",
                 {"directory": str(tmp_path / "in2"), "port": 2022}, rid=3)
    assert reply["ok"] is True
    assert len(announcers) == 2 and announcers[1].started
    call(session, "stop_receive", rid=4)


def test_receive_cleans_leftover_announcer(session, tmp_path, monkeypatch):
    """A start that fails AFTER registering mDNS used to leak a live
    announcer that made every retry die with NonUniqueNameException."""
    calls = {"n": 0}

    def flaky_serve(d, p, u, pw, capture_log=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return _FakeServeProc()

    announcers = _patch_receive(monkeypatch, flaky_serve)
    first = call(session, "start_receive",
                 {"directory": str(tmp_path / "in"), "port": 2022})
    assert first["ok"] is False
    retry = call(session, "start_receive",
                 {"directory": str(tmp_path / "in"), "port": 2022}, rid=2)
    assert retry["ok"] is True
    assert announcers[0].stopped is True  # leftover cleaned before re-register
    call(session, "stop_receive", rid=3)


def test_receive_activity_events_from_serve_log(session, tmp_path, monkeypatch):
    """PRD F16: SSH logins and landed files in the serve log become
    receive_activity events with a running file counter."""
    lines = [
        "2026/07/22 INFO  : serve sftp 10.0.0.2:5->10.0.0.5:2022: "
        "SSH login from u using SSH-2.0-rclone/v1.74.4\n",
        "2026/07/22 INFO  : a.txt.123.partial: Moved (server-side) to: a.txt\n",
        "2026/07/22 NOTICE: unrelated noise\n",
        "2026/07/22 INFO  : b.txt.456.partial: Moved (server-side) to: b.txt\n",
    ]
    _patch_receive(monkeypatch,
                   lambda d, p, u, pw, capture_log=False: _FakeServeProc(lines))
    sess, events = session
    assert call(session, "start_receive",
                {"directory": str(tmp_path / "in"), "port": 2022})["ok"]
    assert wait_until(lambda: sum(
        1 for e in list(events)
        if e.get("event") == "receive_activity" and e.get("kind") == "file") == 2)
    acts = [e for e in events if e.get("event") == "receive_activity"]
    assert acts[0]["kind"] == "login"
    assert [a["value"] for a in acts if a["kind"] == "file"] == ["a.txt", "b.txt"]
    assert [a["files"] for a in acts if a["kind"] == "file"] == [1, 2]
    call(session, "stop_receive", rid=2)


def test_receive_self_death_stops_announcer(session, tmp_path, monkeypatch):
    """If the SFTP server dies on its own, the announcer must come down with
    it - otherwise the next start hits NonUniqueNameException (PRD F15)."""
    announcers = _patch_receive(
        monkeypatch,
        lambda d, p, u, pw, capture_log=False: _FakeServeProc(dead=True))
    sess, events = session
    assert call(session, "start_receive",
                {"directory": str(tmp_path / "in"), "port": 2022})["ok"]
    assert wait_until(lambda: any(
        e.get("event") == "receive_stopped" for e in list(events)))
    assert wait_until(lambda: announcers[0].stopped)


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
    assert tasks[0]["running"] is False
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

    def fake_worker(t, max_rounds, wait):
        started.update(task=t)

    monkeypatch.setattr(sess, "_start_worker", fake_worker)
    reply = call(session, "sync")
    assert reply["ok"] is True
    assert reply["result"]["task"]["task_id"] == "t-done"
    assert started["task"].conflict == "update"  # sync = update semantics
    assert started["task"].rounds_completed == 0  # fresh pass


def test_check_dest_reports_overlap(session, tmp_path, monkeypatch):
    """Names present on both sides are returned; conflict=true (PRD F12)."""
    from lanmigrate import engine

    (tmp_path / "Documents").mkdir()
    (tmp_path / "Photos").mkdir()
    (tmp_path / "only-local.txt").write_text("x")
    monkeypatch.setattr(engine, "obscure", lambda pw: "OBSC")
    monkeypatch.setattr(engine, "list_remote",
                        lambda remote: ["Documents", "Photos", "only-remote"])
    reply = call(session, "check_dest",
                 {"host": "10.0.0.2", "code": "123456", "source": str(tmp_path)})
    assert reply["ok"] is True
    r = reply["result"]
    assert r["conflict"] is True
    assert r["existing"] == ["Documents", "Photos"]


def test_check_dest_fails_open_on_probe_error(session, tmp_path, monkeypatch):
    """A broken probe must never block a migration: conflict=false + error."""
    from lanmigrate import engine

    monkeypatch.setattr(engine, "obscure", lambda pw: "OBSC")

    def boom(remote):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(engine, "list_remote", boom)
    reply = call(session, "check_dest",
                 {"host": "10.0.0.2", "code": "123456", "source": str(tmp_path)})
    assert reply["ok"] is True
    r = reply["result"]
    assert r["conflict"] is False
    assert "connection refused" in r["error"]


def test_concurrent_sends_and_targeted_cancel(session, tmp_path, monkeypatch):
    """PRD F14: two folders transfer at the same time; events carry task_id;
    cancel can target one task without touching the other."""
    from lanmigrate import engine

    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(engine, "obscure", lambda pw: "OBSC")

    src1 = tmp_path / "src1"
    src1.mkdir()
    (src1 / "f.txt").write_text("x")
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / "g.txt").write_text("y")
    key1, key2 = str(src1.resolve()), str(src2.resolve())

    running = {}  # resolved source -> Event that unblocks its fake rclone

    class _FakeRcloneProc:
        def __init__(self, ev):
            self._ev = ev

        def terminate(self):
            self._ev.set()

    def fake_run_copy(source, remote, filter_file=None, log_file=None,
                      on_progress=None, on_start=None, conflict="overwrite"):
        ev = threading.Event()
        running[str(source)] = ev
        if on_start:
            on_start(_FakeRcloneProc(ev))
        ev.wait(timeout=10)
        return 0

    monkeypatch.setattr(engine, "run_copy", fake_run_copy)

    sess, events = session
    assert call(session, "scan", {"path": str(src1)})["ok"]
    assert call(session, "scan", {"path": str(src2)}, rid=2)["ok"]

    r1 = call(session, "start_send",
              {"source": str(src1), "host": "10.0.0.9", "code": "111111"}, rid=3)
    assert r1["ok"] is True
    t1 = r1["result"]["task_id"]
    assert wait_until(lambda: key1 in running)

    # the SAME folder twice is blocked...
    dup = call(session, "start_send",
               {"source": str(src1), "host": "10.0.0.9", "code": "111111"}, rid=4)
    assert dup["ok"] is False
    assert "已在传输中" in dup["error"]

    # ...but a DIFFERENT folder starts concurrently (scan report per path)
    r2 = call(session, "start_send",
              {"source": str(src2), "host": "10.0.0.9", "code": "111111"}, rid=5)
    assert r2["ok"] is True
    t2 = r2["result"]["task_id"]
    assert t2 != t1
    assert wait_until(lambda: key2 in running)

    # while both run, the resume banner offers neither of them
    assert call(session, "latest_incomplete", rid=6)["result"]["task"] is None

    # cancel ONLY task 1
    c1 = call(session, "cancel_send", {"task_id": t1}, rid=7)
    assert c1["result"]["cancelled"] is True

    def done_for(tid):
        return next((e for e in list(events)
                     if e.get("event") == "send_done"
                     and e.get("task_id") == tid), None)

    assert wait_until(lambda: done_for(t1) is not None)
    assert done_for(t1)["cancelled"] is True
    assert done_for(t2) is None  # task 2 keeps running

    # cancel-all brings down the remaining transfer
    c2 = call(session, "cancel_send", rid=8)
    assert c2["result"]["cancelled"] is True
    assert wait_until(lambda: done_for(t2) is not None)


def test_resume_by_id_runs_tasks_to_two_devices_concurrently(
        session, tmp_path, monkeypatch):
    """PRD F17: task records are per-device and never overwrite each other.
    An OLDER incomplete task (the Mac) stays resumable by id after a newer
    one (the Windows PC) was created, and both can transfer at once."""
    from lanmigrate import engine

    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")

    src_mac = tmp_path / "for-mac"
    src_mac.mkdir()
    src_win = tmp_path / "for-win"
    src_win.mkdir()
    taskstore.save(taskstore.MigrationTask(
        task_id="t-mac", source=str(src_mac), host="10.0.0.20", port=2022,
        user="lanmigrate", obscured_pass="xx", rounds_completed=2))
    time.sleep(1.1)  # updated_at has 1s resolution; make t-win strictly newer
    taskstore.save(taskstore.MigrationTask(
        task_id="t-win", source=str(src_win), host="10.0.0.30", port=2022,
        user="lanmigrate", obscured_pass="yy"))

    # default resume (no id) offers only the newest -> the Mac task would be
    # unreachable without the explicit task_id path the GUI list now uses
    latest = call(session, "latest_incomplete")["result"]["task"]
    assert latest["task_id"] == "t-win"

    running = {}  # resolved source -> Event that unblocks its fake rclone

    class _FakeRcloneProc:
        def __init__(self, ev):
            self._ev = ev

        def terminate(self):
            self._ev.set()

    def fake_run_copy(source, remote, filter_file=None, log_file=None,
                      on_progress=None, on_start=None, conflict="overwrite"):
        ev = threading.Event()
        running[str(source)] = ev
        if on_start:
            on_start(_FakeRcloneProc(ev))
        ev.wait(timeout=10)
        return 0

    monkeypatch.setattr(engine, "run_copy", fake_run_copy)

    r_mac = call(session, "resume", {"task_id": "t-mac"}, rid=2)
    assert r_mac["ok"] is True
    assert r_mac["result"]["task"]["host"] == "10.0.0.20"
    assert wait_until(lambda: str(src_mac) in running)

    # both devices transfer at the same time
    r_win = call(session, "resume", {"task_id": "t-win"}, rid=3)
    assert r_win["ok"] is True
    assert wait_until(lambda: str(src_win) in running)

    by_id = {t["task_id"]: t
             for t in call(session, "list_tasks", rid=4)["result"]["tasks"]}
    assert by_id["t-mac"]["running"] and by_id["t-win"]["running"]

    # a running task must not be resumable a second time
    dup = call(session, "resume", {"task_id": "t-mac"}, rid=5)
    assert dup["ok"] is False
    assert "已在传输中" in dup["error"]

    assert call(session, "cancel_send", rid=6)["result"]["cancelled"] is True
    sess, events = session
    assert wait_until(lambda: sum(
        1 for e in list(events) if e.get("event") == "send_done") == 2)


def test_start_send_rejects_unknown_conflict(session):
    reply = call(session, "start_send",
                 {"source": "C:/x", "host": "1.2.3.4", "code": "123456",
                  "conflict": "ask"})
    assert reply["ok"] is False
    assert "同名文件处理" in reply["error"]
