"""
Entry point – wires up the Telegram bot, handlers, and scheduled jobs.
"""

import asyncio
import sys
from pathlib import Path

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import (
    APPLE_PASSWORD,
    APPLE_PASSWORD_OBSCURED,
    AUTH_CHECK_INTERVAL_MINUTES,
    BACKUP_DIR,
    BACKUP_INTERVAL_HOURS,
    FIRST_BACKUP_DELAY_MINUTES,
    ICLOUD_SERVICE,
    RCLONE_CONFIG_DIR,
    RCLONE_REMOTE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    log,
)
from .handlers import (
    cmd_backup,
    cmd_errors,
    cmd_logs,
    cmd_reauth,
    cmd_start,
    cmd_status,
    handle_callback,
    handle_message,
)
from .scheduler import ensure_rclone_config, scheduled_auth_check, scheduled_backup


async def _on_post_init(app: Application):
    """Called by PTB after the bot is fully initialized."""
    await ensure_rclone_config(app)


def main():
    """Start the orchestrator."""
    log.info("=" * 60)
    log.info("iCloud Photos Backup Orchestrator starting")
    log.info("Remote: %s | Service: %s | Backup dir: %s", RCLONE_REMOTE, ICLOUD_SERVICE, BACKUP_DIR)
    log.info("=" * 60)

    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID is not set. Notifications disabled.")
    if APPLE_PASSWORD and not APPLE_PASSWORD_OBSCURED:
        log.warning("APPLE_PASSWORD is plaintext – consider using APPLE_PASSWORD_OBSCURED. "
                     "Generate: rclone obscure YOUR_PASSWORD")

    # Ensure directories exist
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    RCLONE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Build Telegram application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reauth", cmd_reauth))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule periodic jobs
    if TELEGRAM_CHAT_ID:
        chat_id = TELEGRAM_CHAT_ID
        app.job_queue.run_repeating(
            scheduled_auth_check,
            interval=AUTH_CHECK_INTERVAL_MINUTES * 60,
            first=60,
            chat_id=chat_id,
            name="auth_check",
        )
        app.job_queue.run_repeating(
            scheduled_backup,
            interval=BACKUP_INTERVAL_HOURS * 3600,
            first=FIRST_BACKUP_DELAY_MINUTES * 60,
            chat_id=chat_id,
            name="backup",
        )
        log.info(
            "Scheduled: auth check every %d min, backup every %d hours",
            AUTH_CHECK_INTERVAL_MINUTES,
            BACKUP_INTERVAL_HOURS,
        )

    # Wire post_init for the rclone config check
    app.post_init = _on_post_init

    log.info("Starting Telegram bot...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
