#!/usr/bin/env python3
"""Timestamp and retain the SWu/IKE process stream without unbounded SD-card growth."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import time


DEFAULT_SEGMENT_BYTES = 10 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 7

# macOS native port: the engine tree runs as root while the control plane runs as the
# logged-in user, which must read charon.log through the shared data dir. The per-line
# instance directory is already 0700 user-owned, so group/other bits are never reached;
# relax the file mode there only. Linux container behavior is unchanged.
_FILE_MODE = 0o644 if sys.platform == "darwin" else 0o600


def timestamp_line(line: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S%z")
    return f"[{stamp}] {line.rstrip(chr(13) + chr(10))}\n"


def _archive_name(archive_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    candidate = archive_dir / f"charon-{stamp}.log"
    sequence = 1
    while candidate.exists():
        candidate = archive_dir / f"charon-{stamp}-{sequence}.log"
        sequence += 1
    return candidate


def prune_archives(archive_dir: Path, retention_days: int = DEFAULT_RETENTION_DAYS,
                   max_total_bytes: int = DEFAULT_TOTAL_BYTES, now: float | None = None) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    candidates = sorted(archive_dir.glob("charon-*.log"),
                        key=lambda path: (path.stat().st_mtime, path.name))
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass

    candidates = sorted(archive_dir.glob("charon-*.log"),
                        key=lambda path: (path.stat().st_mtime, path.name))
    total = sum(path.stat().st_size for path in candidates)
    for path in candidates:
        if total <= max_total_bytes:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except FileNotFoundError:
            pass


def rotate_current(current: Path, archive_dir: Path, retention_days: int,
                   max_total_bytes: int) -> Path | None:
    """Archive a non-empty current log, truncating it only after the copy succeeds."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    if not current.exists() or current.stat().st_size == 0:
        prune_archives(archive_dir, retention_days, max_total_bytes)
        return None
    target = _archive_name(archive_dir)
    shutil.copy2(current, target)
    os.chmod(target, _FILE_MODE)
    current.write_text("")
    os.chmod(current, _FILE_MODE)
    prune_archives(archive_dir, retention_days, max_total_bytes)
    return target


def capture(source, current: Path, archive_dir: Path,
            segment_bytes: int = DEFAULT_SEGMENT_BYTES,
            retention_days: int = DEFAULT_RETENTION_DAYS,
            max_total_bytes: int = DEFAULT_TOTAL_BYTES) -> None:
    current.parent.mkdir(parents=True, exist_ok=True)
    rotate_current(current, archive_dir, retention_days, max_total_bytes)
    target = current.open("a", encoding="utf-8", buffering=1)
    os.chmod(current, _FILE_MODE)
    try:
        for line in source:
            target.write(timestamp_line(line))
            if target.tell() >= segment_bytes:
                target.close()
                rotate_current(current, archive_dir, retention_days, max_total_bytes)
                target = current.open("a", encoding="utf-8", buffering=1)
                os.chmod(current, _FILE_MODE)
    finally:
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--segment-bytes", type=int, default=DEFAULT_SEGMENT_BYTES)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_TOTAL_BYTES)
    args = parser.parse_args()
    capture(sys.stdin, Path(args.current), Path(args.archive_dir), args.segment_bytes,
            args.retention_days, args.max_total_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
