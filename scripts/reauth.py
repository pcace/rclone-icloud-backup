"""
pexpect-based rclone re-authentication with 2FA handling via Telegram.
"""

import asyncio
import concurrent.futures
import os
from typing import Optional

import pexpect
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application

from .config import (
    APPLE_ID,
    APPLE_PASSWORD,
    APPLE_PASSWORD_OBSCURED,
    RCLONE_CONFIG_FILE,
    RCLONE_REMOTE,
    log,
)
from .state import state

# Global reference to active pexpect child (set by re-auth, read by 2FA feeder)
_active_pexpect_child: Optional[pexpect.spawn] = None
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def get_active_child() -> Optional[pexpect.spawn]:
    return _active_pexpect_child


# ---------------------------------------------------------------------------
# Synchronous re-auth (runs in thread pool)
# ---------------------------------------------------------------------------
def _do_reauth_sync() -> tuple[bool, str]:
    """
    Synchronous re-auth using pexpect. Runs in thread pool.
    Returns (success, error_message).
    Sets _active_pexpect_child for 2FA input from Telegram.
    """
    global _active_pexpect_child

    log.info("Starting re-authentication for remote '%s'", RCLONE_REMOTE)

    child = pexpect.spawn(
        f"rclone config reconnect {RCLONE_REMOTE}:",
        encoding="utf-8",
        timeout=120,
        env={
            **os.environ,
            "RCLONE_CONFIG": str(RCLONE_CONFIG_FILE),
        },
    )

    _active_pexpect_child = child
    log.info("rclone reconnect spawned (pid=%d)", child.pid)

    try:
        idx = child.expect([
            r"[Pp]assword",          # 0
            r"config_2fa",           # 1
            r"2FA|two.factor|code",  # 2
            r"Enter.*value",         # 3
            pexpect.EOF,             # 4
            pexpect.TIMEOUT,         # 5
        ], timeout=60)

        if idx == 0:
            pwd = APPLE_PASSWORD or APPLE_PASSWORD_OBSCURED
            log.info("Sending password...")
            child.sendline(pwd)
            idx = child.expect([
                r"config_2fa",
                r"2FA|two.factor|code",
                r"Enter.*value",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ], timeout=60)

        if idx in (1, 2, 3):
            log.info("2FA required – waiting for code via Telegram...")
            state.set_pending_2fa(True)

            try:
                child.expect(pexpect.EOF, timeout=300)
            except pexpect.TIMEOUT:
                log.warning("2FA timeout after 5 minutes")
                _active_pexpect_child = None
                state.set_pending_2fa(False)
                child.terminate(force=True)
                return (False, "2FA timeout (5 min)")
            except Exception:
                pass

        elif idx == 4:
            log.info("Re-auth process ended early (EOF)")
        elif idx == 5:
            _active_pexpect_child = None
            state.set_pending_2fa(False)
            child.terminate(force=True)
            return (False, "Timeout starting re-auth")

        child.close()
        exit_code = child.exitstatus

        _active_pexpect_child = None
        state.set_pending_2fa(False)

        if exit_code == 0:
            log.info("Re-authentication successful")
            return (True, "")
        else:
            log.error("Re-authentication failed with exit code %d", exit_code)
            return (False, f"Re-auth failed (exit {exit_code})")

    except Exception as e:
        log.exception("Re-auth error")
        _active_pexpect_child = None
        state.set_pending_2fa(False)
        try:
            child.terminate(force=True)
        except Exception:
            pass
        return (False, str(e))


# ---------------------------------------------------------------------------
# Async wrappers (called from the bot's event loop)
# ---------------------------------------------------------------------------
async def start_reauth_in_thread() -> asyncio.Future:
    """Start re-auth in thread pool. Returns a Future that resolves to (success, error)."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_thread_pool, _do_reauth_sync)


async def poll_for_2fa_prompt(app: Application, chat_id: str, future: asyncio.Future):
    """Poll until 2FA is needed or re-auth completes, then send prompt."""
    for _ in range(60):
        child = get_active_child()
        if state.pending_2fa and child and child.isalive():
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🔐 <b>2FA code required</b>\n\n"
                    "Send your 6-digit Apple 2FA code (or 'sms' for SMS):"
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        if future.done():
            return
        await asyncio.sleep(1)


async def feed_2fa_code(code: str) -> tuple[bool, str]:
    """Feed 2FA code to the waiting pexpect child."""
    child = get_active_child()

    if not child or not child.isalive():
        return (False, "No active auth process.")

    if not state.pending_2fa:
        return (False, "No 2FA code expected right now.")

    log.info("Feeding 2FA code to rclone...")
    child.sendline(code)

    return (True, "2FA code sent to rclone.")
