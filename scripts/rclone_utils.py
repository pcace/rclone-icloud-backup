"""
rclone command wrappers: config check, auth validation, backup execution.
"""

import asyncio
import re
from datetime import datetime, timezone

from .config import (
    BACKUP_DIR,
    DRY_RUN,
    ICLOUD_SERVICE,
    MAX_TRANSFER,
    RCLONE_ARGS,
    RCLONE_CONFIG_FILE,
    RCLONE_REMOTE,
    RCLONE_SOURCE,
    SORT_BY_DATE,
    log,
)
from .reorganize import backup_by_date
from .state import state

# Guard to prevent concurrent backup runs
_backup_running = False


def rclone_config_exists() -> bool:
    """Check if rclone config file exists and contains the remote."""
    if not RCLONE_CONFIG_FILE.exists():
        return False
    content = RCLONE_CONFIG_FILE.read_text()
    return f"[{RCLONE_REMOTE}]" in content


async def run_rclone(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run rclone command and return (returncode, stdout, stderr)."""
    # Add extra args from RCLONE_ARGS env variable
    extra_args = RCLONE_ARGS.split() if RCLONE_ARGS else []
    cmd = ["rclone"] + args + extra_args
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return (-1, "", "Timeout")
    return (proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


async def check_auth() -> tuple[bool, str]:
    """Check if rclone authentication is still valid."""
    rc, stdout, stderr = await run_rclone([
        "lsd", f"{RCLONE_REMOTE}:",
        "--iclouddrive-service", ICLOUD_SERVICE,
        "--max-depth", "1",
    ], timeout=30)

    if rc == 0:
        log.info("Auth check: OK")
        return True, stdout.strip()
    else:
        log.warning("Auth check: FAILED (rc=%d) %s", rc, stderr[:500])
        return False, stderr[:500]


async def run_backup() -> tuple[int, str]:
    """Run incremental backup. Returns (files_copied, summary)."""
    global _backup_running

    if _backup_running:
        log.warning("Backup already running – skipping")
        return -1, "⏭ <b>Skipped</b>\nA backup is already in progress."

    _backup_running = True
    try:
        return await _do_run_backup()
    finally:
        _backup_running = False


async def _do_run_backup() -> tuple[int, str]:
    """Internal backup implementation."""
    start = datetime.now(timezone.utc)

    # ---- Date-sorted backup (direct to YYYY/MM/DD, no staging) ----
    if SORT_BY_DATE and not DRY_RUN:
        log.info("Date-sorted backup of %s -> %s/YYYY/MM/DD", RCLONE_REMOTE, BACKUP_DIR)
        files_copied, errors, suffix = await backup_by_date()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        elapsed_str = f"{elapsed / 60:.1f} min" if elapsed >= 60 else f"{elapsed:.0f}s"

        summary = (
            f"📦 <b>Backup complete</b>\n"
            f"⏱ Duration: {elapsed_str}\n"
            f"📁 New files: {files_copied}\n"
            f"❌ Errors: {errors}\n"
            f"📂 Target: <code>{BACKUP_DIR}</code>"
            f"{suffix}"
        )
        log.info("Backup done: %d files, %d errors, %s", files_copied, errors, elapsed_str)

        state.data["last_backup"] = datetime.now(timezone.utc).isoformat()
        state.data["last_backup_files"] = files_copied
        state.data["last_backup_errors"] = errors
        state.save()
        return files_copied, summary

    # ---- Flat backup (album structure) ----
    rclone_args = [
        "copy", f"{RCLONE_REMOTE}:{RCLONE_SOURCE}", BACKUP_DIR,
        "--iclouddrive-service", ICLOUD_SERVICE,
        "--ignore-existing",
        "--progress",
        "--stats", "1m",
        "--stats-one-line",
        "--verbose",
    ]

    if DRY_RUN:
        rclone_args.append("--dry-run")
        log.info("DRY_RUN enabled – no files will be transferred")
    if MAX_TRANSFER:
        rclone_args.extend(["--max-transfer", MAX_TRANSFER])

    mode_tag = " 🧪 DRY-RUN" if DRY_RUN else ""
    limit_tag = f" (max {MAX_TRANSFER})" if MAX_TRANSFER else ""
    log.info("Flat backup of %s -> %s%s%s", RCLONE_REMOTE, BACKUP_DIR, mode_tag, limit_tag)

    rc, stdout, stderr = await run_rclone(rclone_args, timeout=7200)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    elapsed_str = f"{elapsed / 60:.1f} min" if elapsed >= 60 else f"{elapsed:.0f}s"

    files_copied = 0
    errors = 0
    for line in stderr.splitlines():
        m = re.search(r"xfr#(\d+)", line)
        if m:
            files_copied = max(files_copied, int(m.group(1)))
    errors = sum(1 for line in stderr.splitlines() if line.startswith("ERROR"))
    notice_match = re.search(r"Failed to copy with (\d+) errors", stderr)
    if notice_match:
        errors = max(errors, int(notice_match.group(1)))

    summary = (
        f"📦 <b>Backup complete</b>{mode_tag}{limit_tag}\n"
        f"⏱ Duration: {elapsed_str}\n"
        f"📁 New files: {files_copied}\n"
        f"❌ Errors: {errors}\n"
        f"📂 Target: <code>{BACKUP_DIR}</code>"
    )

    log.info("Backup done: %d files, %d errors, %s", files_copied, errors, elapsed_str)

    state.data["last_backup"] = datetime.now(timezone.utc).isoformat()
    state.data["last_backup_files"] = files_copied
    state.data["last_backup_errors"] = errors
    state.save()

    return files_copied, summary
