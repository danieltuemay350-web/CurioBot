"""Server-side Telegram administrator checks."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

from app.config import Settings


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_ids


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        settings: Settings) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    private_chat = chat is not None and chat.type == "private"
    if user is None or not private_chat or not is_admin(user.id, settings):
        if update.callback_query:
            await update.callback_query.answer("Administrator access required", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Administrator access required.")
        return False
    return True
