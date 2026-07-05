"""
Telegram bot command and message handlers.
"""

import asyncio
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import APPLE_ID, APPLE_PASSWORD, APPLE_PASSWORD_OBSCURED, TELEGRAM_CHAT_ID, log
from .rclone_utils import check_auth, run_backup
from .reauth import feed_2fa_code, poll_for_2fa_prompt, start_reauth_in_thread
from .scheduler import send_backup_result
from .state import state


def _authorized(update: Update) -> bool:
    """Check if the message comes from the configured chat ID."""
    if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not _authorized(update):
        return
    auth_ok, _ = await check_auth()

    last_backup = state.data.get("last_backup", "Never")
    if last_backup and last_backup != "Never":
        try:
            dt = datetime.fromisoformat(last_backup)
            last_backup = dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass

    text = (
        f"📷 <b>iCloud Photos Backup</b>\n\n"
        f"🔐 Auth: {'✅ OK' if auth_ok else '❌ Expired'}\n"
        f"📁 Last backup: {last_backup}\n"
        f"📊 New files: {state.data.get('last_backup_files', '—')}\n\n"
        f"<b>Commands:</b>\n"
        f"/status – Status\n"
        f"/backup – Start backup\n"
        f"/reauth – Re-authenticate\n"
        f"/logs – Last backup stats\n"
        f"/errors – Error details"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    if not _authorized(update):
        return
    auth_ok, _ = await check_auth()

    last_backup = state.data.get("last_backup", "Never")
    files = state.data.get("last_backup_files", 0)
    errors = state.data.get("last_backup_errors", 0)

    text = (
        f"🔐 <b>Auth:</b> {'✅ Valid' if auth_ok else '❌ Expired'}\n"
        f"📁 <b>Last backup:</b> {last_backup}\n"
        f"📊 <b>New files:</b> {files}\n"
        f"⚠️ <b>Errors:</b> {errors}\n"
    )
    if errors:
        text += "\n💡 <i>Use /errors for details.</i>\n"
    if not auth_ok:
        text += "\n⚠️ <i>Auth expired. Use /reauth to renew.</i>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backup command."""
    if not _authorized(update):
        return
    msg = await update.message.reply_text("🔄 Starting backup...")

    auth_ok, _ = await check_auth()
    if not auth_ok:
        await msg.edit_text("❌ Auth expired. Use /reauth first.")
        return

    files, summary = await run_backup()
    await msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def cmd_reauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reauth command."""
    if not _authorized(update):
        return
    if not APPLE_ID or (not APPLE_PASSWORD and not APPLE_PASSWORD_OBSCURED):
        await update.message.reply_text("❌ APPLE_ID or APPLE_PASSWORD not set.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, re-authenticate", callback_data="reauth_yes"),
            InlineKeyboardButton("❌ Later", callback_data="reauth_no"),
        ]
    ])
    await update.message.reply_text(
        "⚠️ <b>Renew iCloud authentication?</b>\n\n"
        "You will be asked for your 2FA code.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logs command."""
    if not _authorized(update):
        return
    last_backup = state.data.get("last_backup", "Never")
    files = state.data.get("last_backup_files", 0)
    errors = state.data.get("last_backup_errors", 0)

    text = (
        f"📋 <b>Last backup</b>\n\n"
        f"📅 Time: {last_backup}\n"
        f"📁 New files: {files}\n"
        f"❌ Errors: {errors}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /errors command – shows categorized error summary + samples."""
    if not _authorized(update):
        return

    error_list, summary = state.get_last_errors()

    if not error_list:
        await update.message.reply_text("✅ No errors recorded in the last backup.")
        return

    total = state.data.get("last_backup_errors", len(error_list))

    text = f"⚠️ <b>Last backup errors ({total} total)</b>\n\n"

    if summary:
        text += f"<b>Breakdown:</b>\n{summary}\n\n"

    text += f"<b>Sample errors (first {min(10, len(error_list))}):</b>\n"
    for i, err in enumerate(error_list[:10]):
        path_short = err.get("path", "?")
        if len(path_short) > 60:
            path_short = "..." + path_short[-57:]
        err_msg = err.get("error", "?")[:120]
        text += f"<code>{i+1}. {path_short}</code>\n  → {err_msg}\n"

    if len(error_list) > 10:
        text += f"\n<i>... and {len(error_list) - 10} more</i>"

    # Telegram has a 4096 char limit – truncate if needed
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>… truncated</i>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks (re-auth yes/no)."""
    if not _authorized(update):
        await update.callback_query.answer("Not authorized")
        return

    query = update.callback_query
    await query.answer()

    if query.data == "reauth_yes":
        await query.edit_message_text("🔄 Starting re-authentication...")

        future = await start_reauth_in_thread()
        await poll_for_2fa_prompt(context.application, str(query.message.chat_id), future)

        try:
            success, error = await asyncio.wait_for(future, timeout=360)
        except asyncio.TimeoutError:
            success, error = False, "Timeout"

        if success:
            await query.message.reply_text("✅ Authentication renewed!")
            await send_backup_result(query.message.chat_id, context.application)
        else:
            await query.message.reply_text(
                f"❌ Authentication failed: {error}\nTry /reauth again."
            )

    elif query.data == "reauth_no":
        await query.edit_message_text("👌 OK, I'll remind you later.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages – used for 2FA code input."""
    if not _authorized(update):
        return
    if not state.pending_2fa:
        await update.message.reply_text(
            "ℹ️ Send /start for available commands."
        )
        return

    text = update.message.text.strip()

    if text.lower() == "sms":
        code = "sms"
    elif re.match(r"^\d{6}$", text):
        code = text
    else:
        await update.message.reply_text(
            "⚠️ Send a 6-digit code or 'sms'."
        )
        return

    success, message = await feed_2fa_code(code)
    await update.message.reply_text(f"{'✅' if success else '❌'} {message}")
