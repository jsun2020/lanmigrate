"""JSON-lines IPC backend for the desktop GUI (PRD M3).

The Tauri shell spawns `lanmigrate ipc` and talks newline-delimited JSON
over stdin/stdout (always UTF-8, regardless of console codepage).

  request : {"id": <int>, "method": "<name>", "params": {...}}
  reply   : {"id": <int>, "ok": true, "result": {...}}
            {"id": <int>, "ok": false, "error": "<message>"}
  event   : {"event": "<name>", ...}   (pushed by long-running operations)

stdout is the protocol channel; nothing else may print to it. The transfer
loop runs on a worker thread so cancel_send stays responsive.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from . import __version__, discovery, engine, pairing, scanner, taskstore


class IpcError(Exception):
    """Request failure whose message is shown to the user in the GUI."""


def _task_info(t: taskstore.MigrationTask) -> dict:
    return {
        "task_id": t.task_id,
        "source": t.source,
        "host": t.host,
        "port": t.port,
        "dest": t.dest,
        "status": t.status,
        "rounds_completed": t.rounds_completed,
        "saved_bytes": t.saved_bytes,
        "total_bytes": t.total_bytes,
        "updated_at": t.updated_at,
    }


class Session:
    """One GUI connection. `ipc_<method>` methods are the callable requests;
    `emit` receives every outbound dict (the stdio loop serializes writes)."""

    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit
        self._send_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._rclone_proc = None  # active rclone copy Popen, if any
        self._receive_proc = None
        self._announcer: Optional[discovery.Announcer] = None
        self._scan_report: Optional[scanner.ScanReport] = None

    # ------------------------------------------------------------ dispatch

    def handle(self, request: dict) -> None:
        rid = request.get("id")
        method = str(request.get("method", ""))
        params = request.get("params") or {}
        func = getattr(self, f"ipc_{method}", None)
        if func is None or not method:
            self._emit({"id": rid, "ok": False, "error": f"unknown method: {method}"})
            return
        try:
            result = func(**params)
            self._emit({"id": rid, "ok": True, "result": result})
        except IpcError as exc:
            self._emit({"id": rid, "ok": False, "error": str(exc)})
        except Exception as exc:  # report, never kill the loop (fail loud in GUI)
            self._emit({"id": rid, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=5)})

    def shutdown(self) -> None:
        self._cancel.set()
        proc = self._rclone_proc
        if proc is not None:
            proc.terminate()
        self.ipc_stop_receive()

    # ------------------------------------------------------------ simple

    def ipc_ping(self) -> dict:
        return {"version": __version__}

    def ipc_prepare(self) -> dict:
        """Locate rclone; download on first run (the GUI shows the status)."""
        from . import rclone_bin

        found = rclone_bin.find_rclone()
        if found:
            return {"rclone": str(found), "downloaded": False}
        self._emit({"event": "status",
                    "msg": "首次运行: 正在下载 rclone(约 25MB,需 1~3 分钟)…"})
        path = rclone_bin.download_rclone()
        return {"rclone": str(path), "downloaded": True}

    def ipc_local_info(self) -> dict:
        return {
            "ip": discovery.local_ip(),
            "name": socket.gethostname(),
            "fingerprint": pairing.device_fingerprint(),
        }

    def ipc_list_tasks(self) -> dict:
        return {"tasks": [_task_info(t) for t in taskstore.all_tasks()]}

    def ipc_latest_incomplete(self) -> dict:
        task = taskstore.latest_incomplete()
        return {"task": _task_info(task) if task else None}

    # ------------------------------------------------------------ scan

    def ipc_scan(self, path: str, full: bool = False) -> dict:
        src = Path(path)
        if not src.is_dir():
            raise IpcError(f"目录不存在: {path}")
        last = [0.0]

        def progress(files: int, size: int, rel: str) -> None:
            now = time.monotonic()
            if now - last[0] < 0.3:
                return
            last[0] = now
            self._emit({"event": "scan_progress",
                        "files": files, "bytes": size, "rel": rel})

        report = scanner.scan(src, on_progress=progress, compute_sizes=full)
        self._scan_report = report
        return {
            "path": str(report.root),
            "file_count": report.file_count,
            "total_bytes": report.total_bytes,
            "saved_bytes": report.saved_bytes,
            "exclusions": [{"rel": e.rel, "rule": e.rule, "size": e.size}
                           for e in report.exclusions],
            "large_git_dirs": [{"rel": r, "size": s}
                               for r, s in report.large_git_dirs],
        }

    # ------------------------------------------------------------ discover

    def ipc_discover(self, timeout: float = 5.0) -> dict:
        receivers = discovery.discover(timeout=timeout)
        return {"receivers": [{"name": r.name, "host": r.host, "port": r.port,
                               "fingerprint": r.fingerprint} for r in receivers]}

    def ipc_recall_device(self, fingerprint: str) -> dict:
        remembered = pairing.recall_device(fingerprint) if fingerprint else None
        return {"device": remembered}

    # ------------------------------------------------------------ receive

    def ipc_start_receive(self, directory: str, port: int = 2022,
                          code: Optional[str] = None) -> dict:
        if self._receive_proc is not None and self._receive_proc.poll() is None:
            raise IpcError("接收服务已在运行")
        target = Path(directory).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        code = code or pairing.generate_code()
        password = pairing.session_password(code)
        fp = pairing.device_fingerprint()

        self._announcer = discovery.Announcer(port=port, fingerprint=fp)
        try:
            self._announcer.start()
            mdns = True
        except OSError:
            self._announcer = None  # mDNS blocked; manual IP entry still works
            mdns = False

        self._receive_proc = engine.serve_sftp(target, port, pairing.SFTP_USER, password)
        threading.Thread(target=self._watch_receive,
                         args=(self._receive_proc,), daemon=True).start()
        return {"code": code, "ip": discovery.local_ip(), "port": port,
                "directory": str(target), "mdns": mdns}

    def _watch_receive(self, proc) -> None:
        rc = proc.wait()
        if self._receive_proc is proc:  # died on its own (e.g. port in use)
            self._receive_proc = None
            self._emit({"event": "receive_stopped", "exit_code": rc})

    def ipc_stop_receive(self) -> dict:
        proc, self._receive_proc = self._receive_proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
        if self._announcer is not None:
            try:
                self._announcer.stop()
            except Exception:
                pass
            self._announcer = None
        return {"stopped": proc is not None}

    # ------------------------------------------------------------ send

    def ipc_start_send(self, source: str, host: str, code: str,
                       port: int = 2022, fingerprint: str = "", dest: str = "/",
                       enabled: Optional[list[str]] = None,
                       max_rounds: int = 100, wait: int = 60) -> dict:
        if self._send_thread is not None and self._send_thread.is_alive():
            raise IpcError("已有传输任务在进行中")
        report = self._scan_report
        if report is None or str(report.root) != str(Path(source).resolve()):
            raise IpcError("请先扫描源目录")
        enabled_set = (set(enabled) if enabled is not None
                       else {e.rel for e in report.exclusions})
        filter_lines = scanner.build_filter_lines(report, enabled_set)
        saved = sum(e.size for e in report.exclusions
                    if e.rel in enabled_set and e.size > 0)
        password = pairing.session_password(code)
        if fingerprint:
            pairing.remember_device(fingerprint, host, code)

        task = taskstore.MigrationTask(
            task_id=taskstore.new_task_id(),
            source=str(Path(source).resolve()),
            host=host,
            port=port,
            user=pairing.SFTP_USER,
            obscured_pass=engine.obscure(password),
            dest=dest,
            device_fp=fingerprint,
            filter_lines=filter_lines,
            saved_bytes=saved,
            total_bytes=report.total_bytes,
        )
        taskstore.save(task)
        self._start_worker(task, max_rounds, wait)
        return {"task_id": task.task_id}

    def ipc_resume(self, task_id: Optional[str] = None,
                   max_rounds: int = 100, wait: int = 60) -> dict:
        if self._send_thread is not None and self._send_thread.is_alive():
            raise IpcError("已有传输任务在进行中")
        task = taskstore.load(task_id) if task_id else taskstore.latest_incomplete()
        if task is None:
            raise IpcError("没有未完成的任务")
        # IP may have changed: re-discover by device fingerprint (PRD F1/F4)
        if task.device_fp:
            for r in discovery.discover(timeout=5.0):
                if (r.fingerprint == task.device_fp
                        and (r.host, r.port) != (task.host, task.port)):
                    self._emit({"event": "status",
                                "msg": f"设备 IP 已变化: {task.host} -> {r.host}"})
                    task.host, task.port = r.host, r.port
                    taskstore.save(task)
                    break
        self._start_worker(task, max_rounds, wait)
        return {"task": _task_info(task)}

    def ipc_sync(self, task_id: Optional[str] = None,
                 max_rounds: int = 100, wait: int = 60) -> dict:
        """Incremental sync (PRD F9): re-run any task, done ones included.
        --update mode never overwrites files that are newer on the receiver."""
        if self._send_thread is not None and self._send_thread.is_alive():
            raise IpcError("已有传输任务在进行中")
        task = taskstore.load(task_id) if task_id else taskstore.latest_task()
        if task is None:
            raise IpcError("没有可同步的任务,请先完成一次迁移")
        if task.device_fp:
            for r in discovery.discover(timeout=5.0):
                if (r.fingerprint == task.device_fp
                        and (r.host, r.port) != (task.host, task.port)):
                    self._emit({"event": "status",
                                "msg": f"设备 IP 已变化: {task.host} -> {r.host}"})
                    task.host, task.port = r.host, r.port
                    taskstore.save(task)
                    break
        task.rounds_completed = 0  # fresh pass over the whole tree
        self._start_worker(task, max_rounds, wait, update=True)
        return {"task": _task_info(task)}

    def ipc_cancel_send(self) -> dict:
        running = self._send_thread is not None and self._send_thread.is_alive()
        self._cancel.set()
        proc = self._rclone_proc
        if proc is not None:
            proc.terminate()
        return {"cancelled": running}

    # ------------------------------------------------------------ worker

    def _start_worker(self, task: taskstore.MigrationTask,
                      max_rounds: int, wait: int, update: bool = False) -> None:
        self._cancel.clear()
        self._send_thread = threading.Thread(
            target=self._transfer_loop, args=(task, max_rounds, wait, update),
            daemon=True)
        self._send_thread.start()

    def _transfer_loop(self, task: taskstore.MigrationTask,
                       max_rounds: int, wait: int, update: bool = False) -> None:
        """Unattended rerun-until-exit-0 loop (same semantics as cli._run_task,
        but events instead of Rich output, and cancellable between/inside rounds)."""
        task.status = taskstore.STATUS_RUNNING
        taskstore.save(task)
        remote = engine.sftp_remote(task.host, task.port, task.user,
                                    task.obscured_pass, task.dest)
        started = time.time()
        last = engine.Progress()

        def on_progress(p: engine.Progress) -> None:
            nonlocal last
            last = p
            self._emit({"event": "transfer_progress",
                        "bytes": p.bytes_done, "total": p.total_bytes,
                        "speed": p.speed, "eta": p.eta, "current": p.current,
                        "transfers": p.transfers,
                        "total_transfers": p.total_transfers,
                        "errors": p.errors})

        def set_proc(proc) -> None:
            self._rclone_proc = proc
            if self._cancel.is_set():  # cancel raced the process start
                proc.terminate()

        try:
            for rnd in range(task.rounds_completed + 1, max_rounds + 1):
                if self._cancel.is_set():
                    break
                self._emit({"event": "round", "round": rnd})
                rc = engine.run_copy(Path(task.source), remote,
                                     filter_file=task.filter_file,
                                     log_file=task.log_file,
                                     on_progress=on_progress,
                                     on_start=set_proc,
                                     update=update)
                self._rclone_proc = None
                if self._cancel.is_set():
                    break
                task.rounds_completed = rnd
                taskstore.save(task)
                if rc == 0:
                    task.status = taskstore.STATUS_DONE
                    taskstore.save(task)
                    self._emit({"event": "send_done", "ok": True,
                                "task_id": task.task_id, "rounds": rnd,
                                "bytes": last.bytes_done,
                                "elapsed": time.time() - started,
                                "saved_bytes": task.saved_bytes,
                                "log": str(task.log_file)})
                    return
                self._emit({"event": "round_failed", "round": rnd,
                            "exit_code": rc, "wait": wait,
                            "errors": last.errors})
                if self._cancel.wait(wait):
                    break
            taskstore.save(task)  # interrupt-safe: persist before reporting
            if self._cancel.is_set():
                self._emit({"event": "send_done", "ok": False, "cancelled": True,
                            "task_id": task.task_id,
                            "bytes": last.bytes_done})
            else:
                self._emit({"event": "send_done", "ok": False, "cancelled": False,
                            "task_id": task.task_id,
                            "error": f"已达最大轮数({max_rounds})仍未完成",
                            "log": str(task.log_file)})
        except Exception as exc:
            taskstore.save(task)
            self._emit({"event": "send_done", "ok": False, "cancelled": False,
                        "task_id": task.task_id,
                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._rclone_proc = None


# ---------------------------------------------------------------- stdio loop


def run_stdio() -> None:
    """Blocking main loop: one JSON request per stdin line. Exits when the
    GUI closes the pipe (EOF), cleaning up any running child processes."""
    engine.GUI_MODE = True
    # Windows pipes default to the locale codec (GBK); the protocol is UTF-8.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    lock = threading.Lock()

    def emit(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    session = Session(emit)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                emit({"id": None, "ok": False, "error": "invalid json"})
                continue
            session.handle(request)
    finally:
        session.shutdown()
