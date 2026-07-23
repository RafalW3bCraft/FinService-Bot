"""Build the Telegram bot application for polling mode."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from finservice_bot.bot.handlers import BotHandlers
from finservice_bot.config import ServiceCatalog
from finservice_bot.settings import Settings


LOGGER = logging.getLogger(__name__)

_PUBLIC_COMMANDS = [
    BotCommand("start",      "Open or restart the bot"),
    BotCommand("help",       "Show available commands"),
    BotCommand("language",   "Choose your preferred language"),
    BotCommand("privacy",    "Data handling & privacy"),
    BotCommand("delete_me",  "Erase your account data"),
]

_ADMIN_COMMANDS = _PUBLIC_COMMANDS + [
    BotCommand("add_offer",       "Add a new offer (guided)"),
    BotCommand("template",        "Download the CSV bulk-import template"),
    BotCommand("setup_channels",  "Link a service to a Telegram channel"),
    BotCommand("list_services",   "Service catalogue & channel status"),
    BotCommand("stats",           "Publication counts by status"),
    BotCommand("audit",           "Last 20 admin actions"),
    BotCommand("prune",           "Clean up expired records"),
    BotCommand("block",           "Restrict a user by Telegram ID"),
    BotCommand("unblock",         "Restore a user by Telegram ID"),
    BotCommand("cancel",          "Stop the current workflow"),
]


def build_polling_application(
    settings: Settings,
    repository: Any,
    catalog: ServiceCatalog,
    post_init: Any | None = None,
    post_shutdown: Any | None = None,
    publisher: Any | None = None,
) -> Application[Any, Any, Any, Any, Any, Any]:
    callbacks = BotHandlers(settings, repository, catalog)

    async def combined_post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await _set_commands(app, settings)
        if post_init is not None:
            await post_init(app)
        if publisher is not None and app.job_queue is not None:
            async def _publish_job(context: ContextTypes.DEFAULT_TYPE) -> None:
                now = datetime.now(UTC)
                try:
                    report = await publisher.publish_due(bot=context.bot, now=now)
                    if report.claimed:
                        LOGGER.info(
                            "Publisher: claimed=%d published=%d requeued=%d "
                            "failed=%d review=%d",
                            report.claimed,
                            report.published,
                            report.requeued,
                            report.failed,
                            report.review,
                        )
                except Exception:
                    LOGGER.exception("Publisher job raised an unhandled error")

            interval = timedelta(seconds=publisher.interval_seconds)
            app.job_queue.run_repeating(_publish_job, interval=interval, first=interval)

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(False)
        .post_init(combined_post_init)
    )
    if post_shutdown is not None:
        builder.post_shutdown(post_shutdown)

    application = builder.build()
    _register_handlers(application, callbacks)
    return application


def _register_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    callbacks: BotHandlers,
) -> None:
    commands = {
        "start":          callbacks.start,
        "help":           callbacks.help,
        "language":       callbacks.language,
        "privacy":        callbacks.privacy,
        "delete_me":      callbacks.delete_me,
        "add_offer":      callbacks.add_offer,
        "template":       callbacks.template,
        "setup_channels": callbacks.setup_channels,
        "list_services":  callbacks.list_services,
        "stats":          callbacks.stats,
        "audit":          callbacks.audit,
        "prune":          callbacks.prune,
        "block":          callbacks.block,
        "unblock":        callbacks.unblock,
        "cancel":         callbacks.cancel,
    }
    for command, callback in commands.items():
        application.add_handler(CommandHandler(command, callback))

    application.add_handler(
        MessageHandler(filters.Document.FileExtension("csv"), callbacks.upload_csv)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, callbacks.text_input)
    )
    application.add_handler(CallbackQueryHandler(callbacks.handle_callback_query))
    application.add_error_handler(_handle_error)


async def _set_commands(
    application: Application[Any, Any, Any, Any, Any, Any],
    settings: Settings,
) -> None:
    # Public command list visible to all users in private chats
    await application.bot.set_my_commands(
        _PUBLIC_COMMANDS,
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Extended admin list, set per admin chat so they see all commands
    for admin_id in settings.admin_ids:
        try:
            await application.bot.set_my_commands(
                _ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:
            # Admin may not have opened the bot yet; log rather than swallow
            LOGGER.warning("Could not set admin commands for %d: %s", admin_id, exc)


async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error(
        "Unhandled error: %s", type(context.error).__name__, exc_info=context.error
    )
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong. Please try again, or use /cancel if you're "
            "in the middle of a workflow."
        )
