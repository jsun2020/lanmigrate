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


def test_sftp_remote_string():
    r = engine.sftp_remote("192.168.1.8", 2022, "lanmigrate", "OBSC", "/")
    assert r == ":sftp,host=192.168.1.8,port=2022,user=lanmigrate,pass=OBSC:/"
    r2 = engine.sftp_remote("h", 22, "u", "p", "sub/dir")
    assert r2.endswith(":/sub/dir")
