"""Migration task persistence (PRD F4, C.3-5).

Tasks live in ~/.lanmigrate/tasks/<task-id>.json. Writes are atomic
(temp file + os.replace) so an interrupt can never leave a half-written
JSON behind.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TASKS_DIR = Path.home() / ".lanmigrate" / "tasks"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MigrationTask:
    task_id: str
    source: str
    host: str
    port: int
    user: str
    obscured_pass: str
    dest: str = "/"
    conflict: str = "overwrite"  # same-name handling (PRD F12); resume reuses it
    device_fp: str = ""  # receiver fingerprint; survives IP changes (PRD F1)
    filter_lines: list[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    rounds_completed: int = 0
    saved_bytes: int = 0
    total_bytes: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def filter_file(self) -> Path:
        return TASKS_DIR / f"{self.task_id}.filter.txt"

    @property
    def log_file(self) -> Path:
        return TASKS_DIR / f"{self.task_id}.log"


def new_task_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def save(task: MigrationTask) -> Path:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    task.updated_at = _now()
    target = _task_path(task.task_id)
    fd, tmp = tempfile.mkstemp(dir=TASKS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(task), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)  # atomic on the same filesystem
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # keep the filter file next to the task so resume uses the same rules
    task.filter_file.write_text("\n".join(task.filter_lines) + "\n", encoding="utf-8")
    return target


def load(task_id: str) -> MigrationTask:
    with open(_task_path(task_id), encoding="utf-8") as fh:
        return MigrationTask(**json.load(fh))


def all_tasks() -> list[MigrationTask]:
    if not TASKS_DIR.is_dir():
        return []
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                tasks.append(MigrationTask(**json.load(fh)))
        except (OSError, ValueError, TypeError):
            continue  # skip corrupt/foreign files, never crash resume
    return tasks


def latest_incomplete() -> MigrationTask | None:
    candidates = [t for t in all_tasks() if t.status != STATUS_DONE]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.updated_at)


def latest_task() -> MigrationTask | None:
    """Most recent task regardless of status (sync re-runs done tasks)."""
    tasks = all_tasks()
    if not tasks:
        return None
    return max(tasks, key=lambda t: t.updated_at)
