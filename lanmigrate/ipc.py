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


class _SendJob:
    """One in-flight transfer worker (PRD F14: several may run at once)."""

    def __init__(self, source: str):
        self.source = source
        self.thread: Optional[threading.Thread] = None
        self.cancel = threading.Event()
        self.proc = None  # active rclone copy Popen, if any

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class Session:
    """One GUI connection. `ipc_<method>` methods are the callable requests;
    `emit` receives every outbound dict (the stdio loop serializes writes)."""

    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit
        self._jobs: dict[str, _SendJob] = {}  # task_id -> worker (PRD F14)
        self._receive_proc = None
        self._announcer: Optional[discovery.Announcer] = None
        self._scan_reports: dict[str, scanner.ScanReport] = {}  # by root path

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
        self.ipc_cancel_send()
        self.ipc_stop_receive()

    # ------------------------------------------------------------ simple

    def ipc_ping(self) -> dict:
        return {"version": __version__}

    def ipc_prepare(self) -> dict:
        """Locate rclone (bundled builds install it locally, no download);
        download only as a last resort. Reports admin status so the GUI can
        show the standard-user firewall hint (PRD F11)."""
        from . import preflight, rclone_bin

        found = rclone_bin.find_rclone()
        downloaded = False
        if not found:
            self._emit({"event": "status",
                        "msg": "首次运行: 正在下载 rclone(约 25MB,需 1~3 分钟)…"})
            found = rclone_bin.download_rclone()
            downloaded = True
        return {"rclone": str(found), "downloaded": downloaded,
                "admin": preflight.is_admin()}

    def ipc_local_info(self) -> dict:
        return {
            "ip": discovery.local_ip(),
            "name": socket.gethostname(),
            "fingerprint": pairing.device_fingerprint(),
        }

    def ipc_list_tasks(self) -> dict:
        # PRD F17: the GUI resume list must not offer a task that is being
        # transferred right now, so each entry carries its live running state.
        running = {tid for tid, j in self._jobs.items() if j.running}
        return {"tasks": [dict(_task_info(t), running=t.task_id in running)
                          for t in taskstore.all_tasks()]}

    def ipc_latest_incomplete(self) -> dict:
        task = self._latest_incomplete_idle()
        return {"task": _task_info(task) if task else None}

    def _latest_incomplete_idle(self) -> Optional[taskstore.MigrationTask]:
        """Latest incomplete task that is not being transferred right now -
        an actively running task must not be offered for "resume" (PRD F14)."""
        running = {tid for tid, j in self._jobs.items() if j.running}
        candidates = [t for t in taskstore.all_tasks()
                      if t.status != taskstore.STATUS_DONE
                      and t.task_id not in running]
        return max(candidates, key=lambda t: t.updated_at) if candidates else None

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
        self._scan_reports[str(report.root)] = report
        try:  # PRD F18: remember the result so a re-send can skip the walk
            scanner.save_report(report)
        except Exception:
            pass  # cache is a convenience, never a blocker
        return self._scan_result(report)

    def _scan_result(self, report: scanner.ScanReport) -> dict:
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

    def ipc_scan_cache(self, path: str) -> dict:
        """PRD F18: offer the last scan of this folder instead of re-walking
        a huge tree. The cache only drives exclusion suggestions; the actual
        transfer always copies the live directory contents."""
        src = Path(path)
        if not src.is_dir():
            raise IpcError(f"目录不存在: {path}")
        loaded = scanner.load_report(src)
        if loaded is None:
            return {"cached": False}
        report, scanned_at = loaded
        self._scan_reports[str(report.root)] = report
        return dict(self._scan_result(report), cached=True,
                    cached_at=scanned_at)

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
        from . import preflight

        if self._receive_proc is not None and self._receive_proc.poll() is None:
            raise IpcError("接收服务已在运行")
        # PRD F15: a dead server or a partially-failed previous start may have
        # left the mDNS announcer registered. Its still-live Zeroconf instance
        # would "defend" our service name and make the re-registration below
        # fail with NonUniqueNameException - clean up unconditionally first.
        self.ipc_stop_receive()
        if not preflight.port_available(port):
            raise IpcError(f"端口 {port} 已被占用,请先关闭占用它的程序")
        # PRD F11: elevated -> create the inbound allow rule automatically;
        # standard user -> tell the GUI so it can show guidance.
        if preflight.is_admin():
            firewall_ok, firewall_msg = preflight.ensure_firewall_rule(port)
        else:
            firewall_ok = preflight.firewall_rule_exists(port)
            firewall_msg = ("" if firewall_ok
                            else "标准用户:防火墙可能拦截发送端的连接")
        target = Path(directory).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        code = code or pairing.generate_code()
        password = pairing.session_password(code)
        fp = pairing.device_fingerprint()

        self._announcer = discovery.Announcer(port=port, fingerprint=fp)
        try:
            self._announcer.start()
            mdns = True
        except Exception:  # mDNS blocked/conflicted; manual IP entry works
            self._announcer = None
            mdns = False

        self._receive_proc = engine.serve_sftp(target, port, pairing.SFTP_USER,
                                               password, capture_log=True)
        threading.Thread(target=self._watch_receive,
                         args=(self._receive_proc,), daemon=True).start()
        return {"code": code, "ip": discovery.local_ip(), "port": port,
                "directory": str(target), "mdns": mdns,
                "firewall_ok": firewall_ok, "firewall_msg": firewall_msg}

    def _watch_receive(self, proc) -> None:
        # Drain the serve log for the whole process lifetime (a full pipe
        # would block rclone) and surface activity to the GUI (PRD F16):
        # the receiver finally sees "sender connected" / "N files landed".
        files = 0
        stderr = getattr(proc, "stderr", None)
        if stderr is not None:
            for line in stderr:
                parsed = engine.parse_serve_line(line)
                if parsed is None:
                    continue
                kind, value = parsed
                if kind == "file":
                    files += 1
                self._emit({"event": "receive_activity", "kind": kind,
                            "value": value, "files": files})
        rc = proc.wait()
        if self._receive_proc is proc:  # died on its own (e.g. port in use)
            self._receive_proc = None
            self.ipc_stop_receive()  # PRD F15: never leave the announcer up
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

    def ipc_check_dest(self, host: str, code: str, source: str,
                       port: int = 2022, dest: str = "/") -> dict:
        """Same-name conflict probe (PRD F12): which top-level entries of the
        source already exist at the receiver? Fails OPEN (conflict=false +
        error text) - a broken probe must never block a migration."""
        try:
            password = pairing.session_password(code)
            remote = engine.sftp_remote(host, port, pairing.SFTP_USER,
                                        engine.obscure(password), dest)
            remote_names = set(engine.list_remote(remote))
            local_names = [p.name for p in Path(source).iterdir()]
            overlap = sorted(n for n in local_names if n in remote_names)
            return {"conflict": bool(overlap), "existing": overlap[:20],
                    "existing_total": len(overlap)}
        except Exception as exc:
            return {"conflict": False, "existing": [], "existing_total": 0,
                    "error": f"{type(exc).__name__}: {exc}"}

    def ipc_start_send(self, source: str, host: str, code: str,
                       port: int = 2022, fingerprint: str = "", dest: str = "/",
                       enabled: Optional[list[str]] = None,
                       conflict: str = engine.CONFLICT_OVERWRITE,
                       max_rounds: int = 100, wait: int = 60) -> dict:
        src_key = str(Path(source).resolve())
        # PRD F14: transfers run in parallel; only the SAME folder twice is
        # blocked (it would just make two rclone processes fight each other).
        if any(j.running and j.source == src_key for j in list(self._jobs.values())):
            raise IpcError("该文件夹已在传输中")
        if conflict not in engine.CONFLICT_MODES:
            raise IpcError(f"未知的同名文件处理方式: {conflict}")
        report = self._scan_reports.get(src_key)
        if report is None:
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
            conflict=conflict,
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
        task = taskstore.load(task_id) if task_id else self._latest_incomplete_idle()
        if task is None:
            raise IpcError("没有未完成的任务")
        self._reject_if_running(task)
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
        task = taskstore.load(task_id) if task_id else taskstore.latest_task()
        if task is None:
            raise IpcError("没有可同步的任务,请先完成一次迁移")
        self._reject_if_running(task)
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
        task.conflict = engine.CONFLICT_UPDATE  # sync = update semantics
        self._start_worker(task, max_rounds, wait)
        return {"task": _task_info(task)}

    def ipc_update_code(self, task_id: str, code: str) -> dict:
        """PRD F18: re-pair a saved task after the receiver's pairing code
        changed (e.g. receiver restarted without code reuse). Re-derives the
        session password so resume can authenticate again."""
        task = taskstore.load(task_id)
        self._reject_if_running(task)
        task.obscured_pass = engine.obscure(pairing.session_password(code))
        taskstore.save(task)
        if task.device_fp:  # keep the sender-side auto-fill in step with it
            pairing.remember_device(task.device_fp, task.host, code)
        return {"task": _task_info(task)}

    def ipc_cancel_send(self, task_id: Optional[str] = None) -> dict:
        """Cancel one running transfer (task_id given) or all of them."""
        hit = False
        for tid, job in list(self._jobs.items()):
            if task_id is not None and tid != task_id:
                continue
            if not job.running:
                continue
            hit = True
            job.cancel.set()
            proc = job.proc
            if proc is not None:
                proc.terminate()
        return {"cancelled": hit}

    # ------------------------------------------------------------ worker

    def _classify_round(self, task: taskstore.MigrationTask,
                        offset: int) -> tuple[str, str]:
        """Classify one failed round from its slice of the task log (capped
        at the last 256KB so a round with thousands of file errors stays
        cheap). Any read problem degrades to ("other", "")."""
        try:
            size = task.log_file.stat().st_size
            with open(task.log_file, encoding="utf-8", errors="replace") as fh:
                fh.seek(max(offset, size - 262144))
                return engine.classify_copy_errors(fh.read())
        except OSError:
            return ("other", "")

    def _reject_if_running(self, task: taskstore.MigrationTask) -> None:
        job = self._jobs.get(task.task_id)
        if job is not None and job.running:
            raise IpcError("该任务已在传输中")

    def _start_worker(self, task: taskstore.MigrationTask,
                      max_rounds: int, wait: int) -> None:
        job = _SendJob(source=task.source)
        self._jobs[task.task_id] = job
        job.thread = threading.Thread(
            target=self._transfer_loop, args=(job, task, max_rounds, wait),
            daemon=True)
        job.thread.start()

    def _transfer_loop(self, job: _SendJob, task: taskstore.MigrationTask,
                       max_rounds: int, wait: int) -> None:
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
            self._emit({"event": "transfer_progress", "task_id": task.task_id,
                        "bytes": p.bytes_done, "total": p.total_bytes,
                        "speed": p.speed, "eta": p.eta, "current": p.current,
                        "transfers": p.transfers,
                        "total_transfers": p.total_transfers,
                        "errors": p.errors})

        def set_proc(proc) -> None:
            job.proc = proc
            if job.cancel.is_set():  # cancel raced the process start
                proc.terminate()

        try:
            for rnd in range(task.rounds_completed + 1, max_rounds + 1):
                if job.cancel.is_set():
                    break
                self._emit({"event": "round", "task_id": task.task_id,
                            "round": rnd})
                log_offset = (task.log_file.stat().st_size
                              if task.log_file.exists() else 0)
                rc = engine.run_copy(Path(task.source), remote,
                                     filter_file=task.filter_file,
                                     log_file=task.log_file,
                                     on_progress=on_progress,
                                     on_start=set_proc,
                                     conflict=task.conflict)
                job.proc = None
                if job.cancel.is_set():
                    break
                task.rounds_completed = rnd
                taskstore.save(task)
                if rc == 0:
                    task.status = taskstore.STATUS_DONE
                    taskstore.save(task)
                    self._emit({"event": "send_done", "ok": True,
                                "task_id": task.task_id, "rounds": rnd,
                                "source": task.source,
                                "bytes": last.bytes_done,
                                "elapsed": time.time() - started,
                                "saved_bytes": task.saved_bytes,
                                "log": str(task.log_file)})
                    return
                # PRD F18: read THIS round's log slice to tell the user why.
                # An auth failure can never heal by retrying - the password
                # is derived from the pairing code - so stop and ask for the
                # receiver's current code instead of looping 100 rounds.
                reason, detail = self._classify_round(task, log_offset)
                if reason == "auth":
                    self._emit({
                        "event": "send_done", "ok": False, "cancelled": False,
                        "need_code": True, "task_id": task.task_id,
                        "source": task.source,
                        "error": "配对码不匹配:接收端的配对码已更换"
                                 "(接收端重启后若未勾选“沿用上次的配对码”"
                                 "会生成新码)。请查看接收端当前显示的配对码。"})
                    return
                self._emit({"event": "round_failed", "task_id": task.task_id,
                            "round": rnd, "exit_code": rc, "wait": wait,
                            "errors": last.errors,
                            "reason": reason, "detail": detail})
                if job.cancel.wait(wait):
                    break
            taskstore.save(task)  # interrupt-safe: persist before reporting
            if job.cancel.is_set():
                self._emit({"event": "send_done", "ok": False, "cancelled": True,
                            "task_id": task.task_id, "source": task.source,
                            "bytes": last.bytes_done})
            else:
                self._emit({"event": "send_done", "ok": False, "cancelled": False,
                            "task_id": task.task_id, "source": task.source,
                            "error": f"已达最大轮数({max_rounds})仍未完成",
                            "log": str(task.log_file)})
        except Exception as exc:
            taskstore.save(task)
            self._emit({"event": "send_done", "ok": False, "cancelled": False,
                        "task_id": task.task_id, "source": task.source,
                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            job.proc = None


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
