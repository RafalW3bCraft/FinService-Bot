import pytest

from finservice_bot.cli import build_parser


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (["poll"], "poll"),
        (["poll", "--db", "custom.db"], "poll"),
    ],
)
def test_documented_cli_commands_parse(arguments, command):
    parsed = build_parser().parse_args(arguments)

    assert parsed.command == command
