# LanMigrate M1 MVP Implementation Plan

Source of truth: `prd.md` Appendix B (verified rclone params) + Appendix C (module layout, dev order, constraints).
Stack: Python 3.14 (venv `.venv`), Typer + Rich + zeroconf, rclone engine (`~/.lanmigrate/bin/rclone.exe`, v1.74.4).

## Stage 1: rclone_bin.py + engine.py
**Goal**: Python drives rclone copy with live progress; serve sftp wrapper for receiver.
**Success Criteria**: Copy a small fixture dir via Python, progress events parsed from `--use-json-log` stderr; all baseline params from PRD B.3 centralized in engine.py.
**Tests**: test_engine.py - progress line parsing (mocked rclone output), param assembly, exit-code handling; live smoke copy local->local.
**Status**: Complete

## Stage 2: scanner.py + rules.toml
**Goal**: Context-aware dependency exclusion (marker file => exclude sibling dep dirs) + global junk rules; report with sizes.
**Success Criteria**: PRD F3 acceptance - package.json dir excludes node_modules; bare `build` dir without marker NOT excluded; report shows dirs + GB saved.
**Tests**: test_scanner.py positive/negative cases over fixture tree.
**Status**: Complete

## Stage 3: taskstore.py
**Goal**: Task persistence in ~/.lanmigrate/tasks/<id>.json, atomic write, resume detection.
**Success Criteria**: Round-trip save/load; interrupted write never corrupts JSON (temp file + os.replace).
**Tests**: test_taskstore.py.
**Status**: Complete

## Stage 4: discovery.py + pairing.py
**Goal**: mDNS broadcast/browse of _lanmigrate._tcp.local, manual IP fallback, 6-digit pairing code, device fingerprint persistence.
**Success Criteria**: Self-discovery on this machine within 5s; pairing code verify pass/fail paths.
**Tests**: test_pairing.py; live loopback discovery check.
**Status**: Complete

## Stage 5: cli.py integration
**Goal**: `lanmigrate send/receive/resume` per PRD Appendix A flow; unattended loop-until-clean transfer.
**Success Criteria**: End-to-end local test: receive (serve sftp on loopback) + send, mid-transfer kill + resume skips completed files.
**Tests**: e2e script over loopback SFTP.
**Status**: Complete

## Stage 6: Docs + packaging
**Goal**: README (Win-Win + Win-Mac usage), pyproject.toml, .gitignore, commits.
**Success Criteria**: Fresh-clone instructions work; git history clean.
**Status**: Complete
