"""
Date-sorted backup: download directly to YYYY/MM/DD/ folders
without a staging directory (safe for 2+ TB libraries).
"""

import asyncio
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import BACKUP_DIR, ICLOUD_SERVICE, RCLONE_REMOTE, RCLONE_SOURCE, MAX_TRANSFER, RCLONE_ARGS, log
from .state import state

# How many parallel copyto transfers
PARALLEL_TRANSFERS = 4       # reduced from 8 to ease Apple rate limits
MAX_RETRIES = 3              # retry transient failures
RETRY_DELAY_BASE = 2.0       # base seconds for exponential backoff
RATE_LIMIT_DELAY = 0.3       # small delay between spawns to smooth request rate


# ---- Helpers ----

def _classify_error(stderr_text: str) -> str:
    """Return a short category string for an rclone stderr message."""
    stderr_lower = stderr_text.lower()
    if "rate" in stderr_lower or "429" in stderr_text or "too many" in stderr_lower:
        return "rate-limit"
    if "timeout" in stderr_lower or "timed out" in stderr_lower:
        return "timeout"
    if "not found" in stderr_lower or "404" in stderr_text or "no such" in stderr_lower:
        return "not-found"
    if "auth" in stderr_lower or "unauthorized" in stderr_lower or "401" in stderr_text:
        return "auth"
    if "max transfer" in stderr_lower:
        return "max-transfer"
    if "connection" in stderr_lower or "reset" in stderr_lower:
        return "connection"
    if "space" in stderr_lower or "disk" in stderr_lower:
        return "disk-full"
    return "other"


async def _copy_fullwidth_slash(source: str, dest: str) -> tuple[bool, str, str]:
    """Download a file whose name contains ／ (U+FF0F, iCloud-encoded slash).

    rclone copyto decodes ／→/ and tries to navigate a non-existent sub-
    directory, causing 'directory not found'.  Using rclone copy on the
    parent directory lets rclone work from the cached listing instead of
    navigating by path, which avoids the issue.
    """
    source_path = Path(source)
    parent_dir = str(source_path.parent)
    filename = source_path.name          # original name with ／
    dest_path = Path(dest)               # already has _ instead of ／

    extra_args = RCLONE_ARGS.split() if RCLONE_ARGS else []
    last_err = ""
    last_cat = "other"

    for attempt in range(1, MAX_RETRIES + 1):
        tmpdir = tempfile.mkdtemp(prefix="rclone_fwslash_")
        try:
            proc = await asyncio.create_subprocess_exec(
                "rclone", "copy",
                f"{RCLONE_REMOTE}:{parent_dir}",
                tmpdir,
                "--iclouddrive-service", ICLOUD_SERVICE,
                "--no-traverse",
                "--low-level-retries", "2",
                "--filter", f"+ {filename}",
                "--filter", "- *",
                *extra_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()
            stderr_text = stderr_bytes.decode(errors="replace")

            if proc.returncode == 0:
                downloaded = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                if downloaded:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(downloaded[0]), str(dest_path))
                    return True, "", ""
                # rclone exited 0 but no file was written → filter didn't match
                last_cat = "not-found"
                last_err = "No file downloaded after rclone copy (filter may not match)"
                break  # no point retrying

            last_err = stderr_text
            last_cat = _classify_error(stderr_text)

            if last_cat in ("not-found", "auth", "max-transfer", "disk-full"):
                break
            if "max transfer limit reached" in stderr_text:
                raise MaxTransferReached()
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
                log.debug("copy (fwslash) retry %d/%d for %s after %.1fs",
                          attempt, MAX_RETRIES, source, delay)
                await asyncio.sleep(delay)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    log.warning("copy (fullwidth-slash) FAILED after %d attempts for %s: %s",
                MAX_RETRIES, source, last_err[:200])
    return False, last_cat, last_err[:300]


async def _copyto(source: str, dest: str) -> tuple[bool, str, str]:
    """Copy a single file via rclone copyto with retries.
    Returns (success, error_category, error_message)."""
    # Files with ／ (U+FF0F) need a different download strategy
    if "\uff0f" in Path(source).name:
        return await _copy_fullwidth_slash(source, dest)

    last_err = ""
    last_cat = "other"
    # Build extra args from RCLONE_ARGS
    extra_args = RCLONE_ARGS.split() if RCLONE_ARGS else []
    for attempt in range(1, MAX_RETRIES + 1):
        proc = await asyncio.create_subprocess_exec(
            "rclone", "copyto",
            f"{RCLONE_REMOTE}:{source}",
            dest,
            "--iclouddrive-service", ICLOUD_SERVICE,
            "--ignore-existing",
            "--low-level-retries", "2",
            *extra_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "", ""

        stderr_text = stderr.decode(errors="replace")
        last_err = stderr_text
        last_cat = _classify_error(stderr_text)

        # Don't retry permanent errors
        if last_cat in ("not-found", "auth", "max-transfer", "disk-full"):
            break

        if "max transfer limit reached" in stderr_text:
            raise MaxTransferReached()

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
            log.debug("copyto retry %d/%d for %s after %.1fs: %s",
                      attempt, MAX_RETRIES, source, delay, stderr_text[:100])
            await asyncio.sleep(delay)

    log.warning("copyto FAILED after %d attempts for %s: %s",
                MAX_RETRIES, source, last_err[:200])
    return False, last_cat, last_err[:300]


class MaxTransferReached(Exception):
    pass


def _create_favorites_symlinks(favorites_entries: list[dict]) -> int:
    """
    Scan the backup directory for actual files and create symlinks
    in BACKUP_DIR/Favorites/ pointing to the real dated files.

    Returns the number of symlinks created.
    """
    if not favorites_entries:
        return 0

    backup_root = Path(BACKUP_DIR)
    favorites_dir = backup_root / "Favorites"
    favorites_dir.mkdir(parents=True, exist_ok=True)

    # Build filename → absolute path mapping by scanning YYYY/MM/DD/ dirs
    # Only match against files that actually exist on disk
    filename_map: dict[str, Path] = {}
    for date_dir in backup_root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
        if not date_dir.is_dir():
            continue
        try:
            for file_path in date_dir.iterdir():
                if file_path.is_file() and not file_path.is_symlink():
                    # Store first occurrence (or overwrite with newer – doesn't matter)
                    if file_path.name not in filename_map:
                        filename_map[file_path.name] = file_path
        except PermissionError:
            continue

    if not filename_map:
        log.warning("No backed-up files found for Favorites symlink matching")
        return 0

    created = 0
    for fav in favorites_entries:
        filename = fav["filename"]
        real_file = filename_map.get(filename)
        if real_file is None:
            continue

        symlink_path = favorites_dir / filename
        if symlink_path.exists():
            continue  # already linked

        try:
            # Relative symlink so the backup stays portable
            rel_target = os.path.relpath(real_file, symlink_path.parent)
            symlink_path.symlink_to(rel_target)
            created += 1
        except OSError as e:
            log.debug("Symlink failed for %s: %s", filename, e)

    if created:
        log.info("Created %d Favorites symlinks (matched %d of %d entries)",
                 created, created, len(favorites_entries))
    else:
        log.info("No Favorites symlinks created (0 of %d entries matched)", len(favorites_entries))

    return created


async def backup_by_date() -> tuple[int, int, str]:
    """
    Full date-sorted backup: list files with metadata, then copy
    new/changed files directly into YYYY/MM/DD/ folders.

    Returns (files_copied, errors, summary_suffix).
    """
    # 1. Get file listing (ModTime enthält das Aufnahmedatum, kein --metadata nötig)
    log.info("Fetching file list for date-sorted backup...")
    args = [
        "rclone", "lsjson",
        f"{RCLONE_REMOTE}:{RCLONE_SOURCE}",
        "--iclouddrive-service", ICLOUD_SERVICE,
        "--recursive",
        "--files-only",
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
    log.info("Got %d files from iCloud", len(entries))

    # 2. Build transfer list: (source_path, dest_abs_path)
    #    Also collect Favorites entries for later symlink creation
    tasks = []
    favorites_entries: list[dict] = []  # {"path": ..., "filename": ...}
    skipped = 0
    skipped_favorites = 0
    transfer_bytes = 0

    for entry in entries:
        path = entry.get("Path", "")
        size = entry.get("Size", 0)
        if not path:
            continue

        # Skip virtual Favorites album – originals are already in PrimarySync/
        # rclone can list these paths but cannot download them (path encoding bug)
        # We still remember them to create symlinks later.
        if "/Favorites/" in path:
            skipped_favorites += 1
            favorites_entries.append({
                "path": path,
                "filename": Path(path).name,
            })
            continue

        # Parse date from ModTime (enthält das originale Aufnahmedatum via photo.AssetDate)
        mod_time = entry.get("ModTime", "")
        try:
            dt = datetime.fromisoformat(mod_time)
        except (ValueError, TypeError):
            dt = datetime(2000, 1, 1)  # fallback

        dest_dir = Path(BACKUP_DIR) / f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
        raw_name = Path(path).name
        # ／ (U+FF0F fullwidth solidus) is iCloud's encoding of a literal /
        # in asset IDs. Replace it with _ so the local filename is clean.
        FULLWIDTH_SLASH = "\uff0f"
        safe_name = raw_name.replace(FULLWIDTH_SLASH, "_") if FULLWIDTH_SLASH in raw_name else raw_name
        dest_file = dest_dir / safe_name

        # Skip already existing files (incremental)
        if dest_file.exists() and dest_file.stat().st_size > 0:
            skipped += 1
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        tasks.append((path, str(dest_file), size))

    if skipped_favorites:
        log.info("Skipped %d Favorites entries (virtual album, originals in main library)", skipped_favorites)
    log.info("%d new files to transfer, %d already present", len(tasks), skipped)

    if not tasks:
        # No new files, but still update Favorites symlinks
        favorites_linked = 0
        if favorites_entries:
            favorites_linked = _create_favorites_symlinks(favorites_entries)
        extra = f" (all {skipped} files up to date)"
        if favorites_linked:
            extra += f"\n🔗 {favorites_linked} Favorites symlinks created"
        elif skipped_favorites:
            extra += f"\n🔗 0 Favorites could be matched via symlinks"
        return 0, 0, extra

    # 3. Parallel download with semaphore + error tracking
    sem = asyncio.Semaphore(PARALLEL_TRANSFERS)
    copied = 0
    errors = 0
    total_bytes = 0
    max_reached = False
    error_entries: list[dict] = []  # collect error details
    error_counter = Counter()

    async def transfer_one(source: str, dest: str, size: int):
        nonlocal copied, errors, total_bytes, max_reached, error_entries
        async with sem:
            if max_reached:
                return
            try:
                ok, cat, msg = await _copyto(source, dest)
                if ok:
                    copied += 1
                    total_bytes += size
                else:
                    errors += 1
                    error_counter[cat] += 1
                    error_entries.append({
                        "path": source,
                        "category": cat,
                        "error": msg[:200],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
            except MaxTransferReached:
                max_reached = True

    # Spawn all tasks - semaphore limits concurrency
    coros = [transfer_one(s, d, sz) for s, d, sz in tasks]
    await asyncio.gather(*coros)

    # 3b. Create Favorites symlinks (match by filename against backed-up files)
    favorites_linked = 0
    if favorites_entries:
        favorites_linked = _create_favorites_symlinks(favorites_entries)

    # Build categorized error summary
    if error_entries:
        summary_lines = [f"{cat}: {count}" for cat, count in error_counter.most_common()]
        summary = ", ".join(summary_lines)
    else:
        summary = ""

    # Persist error details
    state.record_errors(error_entries, summary)

    # 4. Summary
    suffix = ""
    if max_reached:
        suffix += f" (max-transfer limit reached)"
    suffix += f"\n📅 Sorted into {copied} files (YYYY/MM/DD)"
    if skipped:
        suffix += f"\n📁 {skipped} files already present"
    if skipped_favorites:
        suffix += f"\n⭐ {skipped_favorites} Favorites skipped (virtual album)"
        if favorites_linked:
            suffix += f"\n🔗 {favorites_linked} Favorites symlinks created"
        else:
            suffix += f"\n🔗 0 Favorites could be matched via symlinks"

    log.info("Date-sorted backup: %d copied, %d errors, %d skipped", copied, errors, skipped)
    return copied, errors, suffix
