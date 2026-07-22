from types import SimpleNamespace

from telegram.ext import CommandHandler, MessageHandler

from finservice_bot.bot.application import build_polling_application
from finservice_bot.config import ServiceCatalog
from finservice_bot.settings import Settings


def test_application_registers_supported_commands_and_has_updater():
    settings = Settings.from_mapping(
        {
            "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN",
            "ADMIN_IDS": "42",
        }
    )
    application = build_polling_application(
        settings,
        SimpleNamespace(),
        ServiceCatalog.load("config/services.yaml"),
    )

    commands = {
        command
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    message_handlers = [
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, MessageHandler)
    ]

    assert application.updater is not None
    assert {
        "start",
        "help",
        "privacy",
        "delete_me",
        "add_offer",
        "template",
        "setup_channels",
        "list_services",
        "stats",
        "audit",
        "prune",
        "block",
        "unblock",
        "cancel",
    } <= commands
    assert len(message_handlers) == 2
