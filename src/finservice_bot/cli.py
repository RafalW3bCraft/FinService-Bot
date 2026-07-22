"""Command-line interface — polling mode only."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

from finservice_bot.config import ServiceCatalog
from finservice_bot.settings import Settings, SettingsError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finservice-bot",
        description="FinService Bot — Telegram referral offer publisher",
    )
    parser.add_argument("--env-file", type=Path, metavar="FILE", help="dotenv configuration file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    poll = subcommands.add_parser("poll", help="Run the bot in local polling mode")
    poll.add_argument(
        "--db",
        type=Path,
        default=Path("finservice.db"),
        metavar="PATH",
        help="SQLite database path (default: finservice.db)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=arguments.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )

    try:
        if arguments.command == "poll":
            _poll(arguments)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(0)
    except (SettingsError, ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


def _poll(arguments: argparse.Namespace) -> None:
    if arguments.env_file is not None:
        if not arguments.env_file.is_file():
            raise SettingsError(f"Environment file does not exist: {arguments.env_file}")
        for key, value in dotenv_values(arguments.env_file).items():
            if value is not None:
                os.environ.setdefault(key, value)

    settings = Settings.load()
    catalog = ServiceCatalog.load(settings.service_config_path)

    from finservice_bot.storage.sqlite_repo import SqliteRepository

    repository = SqliteRepository(arguments.db, catalog, settings.admin_ids)

    async def post_init(application: object) -> None:
        await repository.connect()

    async def post_shutdown(application: object) -> None:
        await repository.close()

    from datetime import timedelta

    from finservice_bot.bot.application import build_polling_application
    from finservice_bot.services.publisher import Publisher

    publisher = Publisher(
        repository=repository,
        bot=None,          # bot is injected at runtime via the job context
        catalog=catalog,
        claim_ttl=timedelta(seconds=settings.claim_ttl_seconds),
        batch_size=settings.publish_batch_size,
        interval_seconds=settings.publish_interval_seconds,
    )

    application = build_polling_application(
        settings,
        repository,
        catalog,
        post_init=post_init,
        post_shutdown=post_shutdown,
        publisher=publisher,
    )

    print(f"Starting FinService Bot  •  db={arguments.db}", file=sys.stderr)
    application.run_polling(drop_pending_updates=True)
