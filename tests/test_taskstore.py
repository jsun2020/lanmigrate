"""Taskstore tests: round-trip, atomicity, resume selection (PRD F4)."""
import json
from pathlib import Path

import pytest

from lanmigrate import taskstore


@pytest.fixture(autouse=True)
def isolated_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    yield


def make_task(**kw):
    defaults = dict(
        task_id=taskstore.new_task_id(),
        source="D:/projects",
        host="192.168.1.8",
        port=2022,
        user="lanmigrate",
        obscured_pass="OBSC",
        filter_lines=["- /a/node_modules/**"],
    )
    defaults.update(kw)
    return taskstore.MigrationTask(**defaults)


def test_round_trip():
    task = make_task()
    taskstore.save(task)
    loaded = taskstore.load(task.task_id)
    assert loaded == task
    assert loaded.filter_file.read_text(encoding="utf-8").startswith("- /a/node_modules/**")


def test_save_is_valid_json_and_no_tmp_left():
    task = make_task()
    path = taskstore.save(task)
    json.loads(Path(path).read_text(encoding="utf-8"))
    leftovers = list(taskstore.TASKS_DIR.glob("*.tmp"))
    assert leftovers == []


def test_latest_incomplete_skips_done():
    t1 = make_task(task_id="20260101-000000-aaaaaa", status=taskstore.STATUS_DONE)
    t2 = make_task(task_id="20260102-000000-bbbbbb", status=taskstore.STATUS_RUNNING)
    taskstore.save(t1)
    taskstore.save(t2)
    latest = taskstore.latest_incomplete()
    assert latest is not None
    assert latest.task_id == t2.task_id


def test_latest_incomplete_none_when_all_done():
    taskstore.save(make_task(status=taskstore.STATUS_DONE))
    assert taskstore.latest_incomplete() is None


def test_corrupt_task_file_is_skipped():
    taskstore.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    (taskstore.TASKS_DIR / "broken.json").write_text("{half", encoding="utf-8")
    taskstore.save(make_task())
    assert len(taskstore.all_tasks()) == 1


def test_unique_task_ids():
    ids = {taskstore.new_task_id() for _ in range(20)}
    assert len(ids) == 20
