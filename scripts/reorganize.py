"""
Date-sorted backup: download directly to YYYY/MM/DD/ folders
without a staging directory (safe for 2+ TB libraries).
"""

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

from .config import BACKUP_DIR, ICLOUD_SERVICE, RCLONE_REMOTE, RCLONE_SOURCE, MAX_TRANSFER, log

# How many parallel copyto transfers
PARALLEL_TRANSFERS = 8


async def _copyto(source: str, dest: str) -> bool:
    """Copy a single file via rclone copyto. Returns True on success."""
    proc = await asyncio.create_subprocess_exec(
        "rclone", "copyto",
        f"{RCLONE_REMOTE}:{source}",
        dest,
        "--iclouddrive-service", ICLOUD_SERVICE,
        "--ignore-existing",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return True
    stderr_text = stderr.decode(errors="replace")
    if "max transfer limit reached" in stderr_text:
        raise MaxTransferReached()
    log.warning("copyto failed for %s: %s", source, stderr_text[:200])
    return False


class MaxTransferReached(Exception):
    pass


async def backup_by_date() -> tuple[int, int, str]:
    """
    Full date-sorted backup: list files with metadata, then copy
    new/changed files directly into YYYY/MM/DD/ folders.

    Returns (files_copied, errors, summary_suffix).
    """
    # 1. Get file listing with metadata
    log.info("Fetching file metadata for date-sorted backup...")
    args = [
        "rclone", "lsjson",
        f"{RCLONE_REMOTE}:{RCLONE_SOURCE}",
        "--iclouddrive-service", ICLOUD_SERVICE,
        "--metadata",
        "--recursive",
        "--files-only",
        "--no-mimetype",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        log.error("rclone lsjson timed out")
        return 0, 0, ""
    except Exception as e:
        log.error("rclone lsjson failed: %s", e)
        return 0, 0, ""

    entries = json.loads(stdout.decode())
    log.info("Got metadata for %d files", len(entries))

    # 2. Build transfer list: (source_path, dest_abs_path)
    tasks = []
    skipped = 0
    transfer_bytes = 0

    for entry in entries:
        path = entry.get("Path", "")
        size = entry.get("Size", 0)
        if not path:
            continue

        # Parse date
        added_raw = entry.get("Metadata", {}).get("added-time", "")
        try:
            added_raw = added_raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(added_raw)
        except (ValueError, TypeError):
            dt = datetime(2000, 1, 1)  # fallback

        dest_dir = Path(BACKUP_DIR) / f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
        dest_file = dest_dir / Path(path).name

        # Skip already existing files (incremental)
        if dest_file.exists() and dest_file.stat().st_size > 0:
            skipped += 1
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        tasks.append((path, str(dest_file), size))

    log.info("%d new files to transfer, %d already present", len(tasks), skipped)

    if not tasks:
        return 0, 0, f" (all {skipped} files up to date)"

    # 3. Parallel download with semaphore
    sem = asyncio.Semaphore(PARALLEL_TRANSFERS)
    copied = 0
    errors = 0
    total_bytes = 0
    max_reached = False

    async def transfer_one(source: str, dest: str, size: int):
        nonlocal copied, errors, total_bytes, max_reached
        async with sem:
            if max_reached:
                return
            try:
                ok = await _copyto(source, dest)
                if ok:
                    copied += 1
                    total_bytes += size
                else:
                    errors += 1
            except MaxTransferReached:
                max_reached = True

    await asyncio.gather(*[transfer_one(s, d, sz) for s, d, sz in tasks])

    # 4. Summary
    suffix = ""
    if max_reached:
        suffix += f" (max-transfer limit reached)"
    suffix += f"\n📅 Sorted into {copied} files (YYYY/MM/DD)"
    if skipped:
        suffix += f"\n📁 {skipped} files already present"

    log.info("Date-sorted backup: %d copied, %d errors, %d skipped", copied, errors, skipped)
    return copied, errors, suffix
