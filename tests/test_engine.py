"""Engine tests: JSON-log progress parsing and command assembly (PRD C.3-1,3)."""
from pathlib import Path

from lanmigrate import engine

STATS_LINE = (
    '{"level":"info","msg":"...","stats":{"bytes":52428800,"totalBytes":104857600,'
    '"speed":10485760.5,"eta":5,"errors":1,"transfers":3,"totalTransfers":10,'
    '"transferring":[{"name":"projects/demo/bigfile.bin","size":1000}]},'
    '"time":"2026-07-13T10:00:00.000000+08:00"}'
)


def test_parse_stats_line():
    p = engine.parse_progress_line(STATS_LINE)
    assert p is not None
    assert p.bytes_done == 52428800
    assert p.total_bytes == 104857600
    assert p.percent == 50.0
    assert p.speed == 10485760.5
    assert p.eta == 5
    assert p.errors == 1
    assert p.transfers == 3
    assert p.total_transfers == 10
    assert p.current == "projects/demo/bigfile.bin"


def test_parse_regular_log_line_returns_none():
    line = '{"level":"info","msg":"Copied (new)","object":"a.txt","time":"..."}'
    assert engine.parse_progress_line(line) is None


def test_parse_non_json_returns_none():
    assert engine.parse_progress_line("Transferred: 12 MiB / 100 MiB") is None
    assert engine.parse_progress_line("") is None
    assert engine.parse_progress_line("{broken json") is None


def test_percent_zero_total():
    assert engine.Progress().percent == 0.0


def test_build_copy_cmd_baseline_flags():
    cmd = engine.build_copy_cmd(
        Path("rclone.exe"), Path("D:/src"), ":sftp,host=1.2.3.4:/", Path("f.txt")
    )
    joined = " ".join(cmd)
    # Baseline params from PRD B.3 must all be present
    for flag in ["--transfers 8", "--checkers 16", "--partial-suffix .part",
                 "--retries 5", "--retries-sleep 15s", "--low-level-retries 20",
                 "--skip-links", "--create-empty-src-dirs",
                 "--use-json-log", "--filter-from"]:
        assert flag in joined, flag


def test_build_copy_cmd_conflict_modes():
    base = engine.build_copy_cmd(Path("rclone.exe"), Path("D:/s"), ":sftp:/")
    assert "--update" not in base and "--suffix" not in base  # overwrite = default

    sync = engine.build_copy_cmd(Path("rclone.exe"), Path("D:/s"), ":sftp:/",
                                 conflict=engine.CONFLICT_UPDATE)
    assert "--update" in sync  # newer receiver files preserved

    keep = engine.build_copy_cmd(Path("rclone.exe"), Path("D:/s"), ":sftp:/",
                                 conflict=engine.CONFLICT_KEEP_BOTH)
    assert "--suffix" in keep and "--suffix-keep-extension" in keep
    suffix = keep[keep.index("--suffix") + 1]
    assert suffix.startswith("-old-")  # receiver's version renamed, both kept


def test_build_copy_cmd_rejects_unknown_conflict():
    import pytest
    with pytest.raises(ValueError):
        engine.build_copy_cmd(Path("rclone.exe"), Path("D:/s"), ":sftp:/",
                              conflict="ask-me")


def test_latest_task_includes_done(tmp_path, monkeypatch):
    from lanmigrate import taskstore
    monkeypatch.setattr(taskstore, "TASKS_DIR", tmp_path / "tasks")
    assert taskstore.latest_task() is None
    done = taskstore.MigrationTask(
        task_id="t-old", source="s", host="h", port=1, user="u",
        obscured_pass="p", status=taskstore.STATUS_DONE)
    taskstore.save(done)
    assert taskstore.latest_task().task_id == "t-old"
    assert taskstore.latest_incomplete() is None  # resume still skips done


def test_parse_serve_line_classifies_activity():
    """Receiver-side activity comes from `serve sftp -v` stderr (PRD F16);
    line shapes verified empirically against rclone v1.74."""
    login = ("2026/07/22 13:39:21 INFO  : serve sftp 10.0.0.2:64735->"
             "10.0.0.5:2022: SSH login from u using SSH-2.0-rclone/v1.74.4")
    moved = ("2026/07/22 13:39:21 INFO  : hello.txt.5a7f61d4.partial: "
             "Moved (server-side) to: hello.txt")
    assert engine.parse_serve_line(login) == (
        "login", "u using SSH-2.0-rclone/v1.74.4")
    assert engine.parse_serve_line(moved) == ("file", "hello.txt")
    assert engine.parse_serve_line(
        "2026/07/22 13:39:19 NOTICE: SFTP server listening on :2022") is None
    assert engine.parse_serve_line("") is None


def test_sftp_remote_string():
    r = engine.sftp_remote("192.168.1.8", 2022, "lanmigrate", "OBSC", "/")
    assert r == ":sftp,host=192.168.1.8,port=2022,user=lanmigrate,pass=OBSC:/"
    r2 = engine.sftp_remote("h", 22, "u", "p", "sub/dir")
    assert r2.endswith(":/sub/dir")


def test_classify_copy_errors_auth_connect_other():
    """PRD F18: round-failure triage. Auth = unhealable (stop and re-pair);
    connect = receiver down (keep retrying); other = e.g. locked files."""
    auth = (
        '{"level":"error","msg":"Failed to create file system: NewFs: '
        "couldn't connect SSH: ssh: handshake failed: ssh: unable to "
        'authenticate, attempted methods [none password]"}'
    )
    cat, detail = engine.classify_copy_errors(auth)
    assert cat == "auth"
    assert "authenticate" in detail

    conn = (
        '{"level":"error","msg":"Failed to create file system: NewFs: '
        "dial tcp 192.168.1.2:2026: connectex: No connection could be made "
        'because the target machine actively refused it."}'
    )
    assert engine.classify_copy_errors(conn)[0] == "connect"

    locked = '{"level":"error","msg":"Failed to copy: sftp: open NTUSER.DAT: locked"}'
    cat, detail = engine.classify_copy_errors(locked)
    assert cat == "other"
    assert "Failed to copy" in detail

    # auth wins even when connect markers appear in the same slice
    assert engine.classify_copy_errors(conn + "\n" + auth)[0] == "auth"
    assert engine.classify_copy_errors("")[0] == "other"
