"""
Scheduled jobs: periodic auth check, periodic backup, initial setup.
"""

from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from .config import (
    APPLE_ID,
    APPLE_PASSWORD,
    APPLE_PASSWORD_OBSCURED,
    ICLOUD_SERVICE,
    INIT_AUTO,
    RCLONE_CONFIG_FILE,
    RCLONE_REMOTE,
    TELEGRAM_CHAT_ID,
    log,
)
from .rclone_utils import check_auth, rclone_config_exists, run_backup
from .state import state


async def scheduled_auth_check(context: ContextTypes.DEFAULT_TYPE):
    """Periodic auth check. Sends Telegram notification if auth is invalid."""
    chat_id = TELEGRAM_CHAT_ID
    if not chat_id:
        return

    auth_ok, _ = await check_auth()
    state.data["auth_valid"] = auth_ok
    state.data["last_auth_check"] = datetime.now(timezone.utc).isoformat()
    state.save()

    if not auth_ok:
        log.warning("Auth invalid – sending notification")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ja, neu authentifizieren", callback_data="reauth_yes"),
                InlineKeyboardButton("❌ Nein, spaeter", callback_data="reauth_no"),
            ]
        ])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ <b>iCloud Authentifizierung ist abgelaufen!</b>\n\nNeu authentifizieren?",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.error("Failed to send Telegram notification: %s", e)


async def scheduled_backup(context: ContextTypes.DEFAULT_TYPE):
    """Periodic backup job."""
    chat_id = TELEGRAM_CHAT_ID

    auth_ok, _ = await check_auth()
    state.data["auth_valid"] = auth_ok
    state.save()

    if not auth_ok:
        log.warning("Skipping backup – auth is invalid")
        if chat_id:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ja, neu authentifizieren", callback_data="reauth_yes"),
                    InlineKeyboardButton("❌ Nein, spaeter", callback_data="reauth_no"),
                ]
            ])
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Backup uebersprungen – Authentifizierung ist abgelaufen.\n\nNeu authentifizieren?",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    files, summary = await run_backup()

    if chat_id and summary:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            log.error("Failed to send backup summary: %s", e)


async def send_backup_result(chat_id: str, app: Application):
    """Send backup result via Telegram (used after successful re-auth)."""
    auth_ok, _ = await check_auth()
    if not auth_ok:
        return
    files, summary = await run_backup()
    try:
        await app.bot.send_message(chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error("Failed to send backup result: %s", e)


def _create_initial_config():
    """Create a minimal rclone config file from env vars.
    After this, rclone config reconnect must be run to obtain trust token + cookies."""
    import subprocess

    RCLONE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Use pre-obscured password if available, otherwise obscure the plaintext one
    if APPLE_PASSWORD_OBSCURED:
        obscured = APPLE_PASSWORD_OBSCURED
        log.info("Using pre-obscured password from APPLE_PASSWORD_OBSCURED")
    else:
        try:
            result = subprocess.run(
                ["rclone", "obscure", APPLE_PASSWORD],
                capture_output=True, text=True, timeout=10,
            )
            obscured = result.stdout.strip()
        except Exception:
            obscured = APPLE_PASSWORD  # fallback

    config = (
        f"[{RCLONE_REMOTE}]\n"
        f"type = iclouddrive\n"
        f"service = {ICLOUD_SERVICE}\n"
        f"apple_id = {APPLE_ID}\n"
        f"password = {obscured}\n"
    )
    RCLONE_CONFIG_FILE.write_text(config)
    log.info("Initial rclone config created for remote '%s'", RCLONE_REMOTE)


async def ensure_rclone_config(app: Application):
    """Check if rclone config exists. If INIT_AUTO is set, create it automatically."""
    if rclone_config_exists():
        log.info("rclone config found for remote '%s'", RCLONE_REMOTE)
        return True

    log.warning("No rclone config found – needs initial setup")

    if INIT_AUTO:
        log.info("INIT_AUTO enabled – creating initial config and triggering re-auth")
        _create_initial_config()

        if TELEGRAM_CHAT_ID:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ja, jetzt einrichten", callback_data="reauth_yes"),
                    InlineKeyboardButton("❌ Spaeter", callback_data="reauth_no"),
                ]
            ])
            try:
                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        "🆕 <b>Ersteinrichtung</b>\n\n"
                        "Initiale rclone-Konfiguration wurde erstellt.\n"
                        "Jetzt 2FA-Code bereithalten und Authentifizierung starten:"
                    ),
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.error("Failed to send setup message: %s", e)
        return False

    # Manual setup instructions
    if TELEGRAM_CHAT_ID:
        try:
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    "🆕 <b>Ersteinrichtung erforderlich</b>\n\n"
                    "Es wurde noch keine rclone-Konfiguration gefunden.\n\n"
                    "Bitte ausfuehren:\n\n"
                    "<pre>docker-compose exec rclone-icloud-backup rclone config</pre>\n\n"
                    "Waehle dann:\n"
                    "• Storage: <code>iclouddrive</code>\n"
                    "• Service: <code>photos</code>\n"
                    f"• Remote-Name: <code>{RCLONE_REMOTE}</code>\n\n"
                    "Oder setze <code>INIT_AUTO=true</code> in der .env fuer automatische Einrichtung."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.error("Failed to send setup message: %s", e)

    return False
