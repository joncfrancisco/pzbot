"""The error handler: the one place every command's failure funnels through.

A bug here is worse than a bug anywhere else in the bot -- it does not just break one
command, it hides the real error behind a second, unrelated crash. This is not
hypothetical: `case AwsError():` did exactly that in production (`AwsError` is a tuple
for `except` purposes, and `match`/`case` class patterns reject tuples with
`TypeError: called match pattern must be a class`), which meant every AWS failure -- the
IAM AccessDenied included -- surfaced as an opaque "something broke" with no useful
detail in Discord, and only the real cause in the journal.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from botocore.exceptions import ClientError

from pzbot.bot import PzBot
from pzbot.guards import Denied
from pzbot.server import OperationError


@pytest.fixture
def bot(cfg, aws) -> PzBot:
    instance = PzBot.__new__(PzBot)  # skip discord.Client.__init__ and the gateway
    instance.cfg = cfg
    instance.ctx = SimpleNamespace(audit=SimpleNamespace(record=AsyncMock()))
    return instance


def fake_interaction() -> discord.Interaction:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.command = SimpleNamespace(qualified_name="pz config get")
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    return interaction


def client_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "SendCommand",
    )


@pytest.mark.parametrize(
    "original",
    [
        Denied("not allowed"),
        OperationError("the instance is stopped"),
        client_error(),
        RuntimeError("something else entirely"),
    ],
)
async def test_every_kind_of_failure_produces_one_reply(bot, original):
    # The regression: this used to raise TypeError out of the handler itself for the
    # ClientError case, before a reply was ever sent.
    interaction = fake_interaction()
    error = SimpleNamespace(original=original)

    await bot.on_command_error(interaction, error)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"] is not None


async def test_every_failure_is_audited_with_an_outcome(bot):
    interaction = fake_interaction()
    await bot.on_command_error(interaction, SimpleNamespace(original=client_error()))

    bot.ctx.audit.record.assert_awaited_once()
    _, kwargs = bot.ctx.audit.record.call_args
    assert kwargs["outcome"] == "failed"


async def test_a_denial_is_audited_as_denied_not_failed(bot):
    interaction = fake_interaction()
    await bot.on_command_error(interaction, SimpleNamespace(original=Denied("nope")))

    _, kwargs = bot.ctx.audit.record.call_args
    assert kwargs["outcome"] == "denied"


# --- The heartbeat (pzserver PZ-04) ---------------------------------------------------


@pytest.fixture
def heartbeat_bot(cfg, aws) -> PzBot:
    """A bot whose presence update is stubbed, so the tests are about the heartbeat."""
    instance = PzBot.__new__(PzBot)
    instance.cfg = cfg
    instance.ctx = SimpleNamespace(aws=aws)
    instance._update_presence = AsyncMock()
    return instance


async def test_the_presence_loop_publishes_a_heartbeat(heartbeat_bot, aws):
    # `.presence` is a discord.ext Loop object; call the wrapped coroutine directly.
    await PzBot.presence.coro(heartbeat_bot)
    assert "heartbeat" in aws.calls


async def test_the_heartbeat_is_published_even_when_the_probe_fails(heartbeat_bot, aws):
    # A transient DescribeInstances error means AWS is unhappy, not that this process is
    # dead. Suppressing the heartbeat here would page someone about a bot running fine.
    # AwsError is a tuple of exception classes for `except`, not something you raise --
    # so this uses a real ClientError, the same one the error-handler tests use.
    heartbeat_bot._update_presence.side_effect = client_error()
    await PzBot.presence.coro(heartbeat_bot)
    assert "heartbeat" in aws.calls


@pytest.mark.parametrize(
    "escaping",
    [
        client_error(),
        OperationError("the game server no longer exists"),
        RuntimeError("a `players` response the parser did not like"),
    ],
)
async def test_a_failed_probe_does_not_kill_the_presence_loop(heartbeat_bot, aws, escaping):
    # `tasks.loop` stops permanently on an unhandled exception and nothing restarts it,
    # so anything escaping the probe is worse than the thing that escaped: presence
    # freezes on a stale reading and the heartbeat stops for good -- which then trips the
    # alarm built to catch "healthy host, dead bot" because its own publisher was killed.
    heartbeat_bot._update_presence.side_effect = escaping
    await PzBot.presence.coro(heartbeat_bot)  # must not raise
    assert "heartbeat" in aws.calls


async def test_a_failed_heartbeat_does_not_kill_the_presence_loop(heartbeat_bot, aws):
    # tasks.loop stops on an unhandled exception. A PutMetricData throttle taking the
    # presence loop down would turn a cosmetic failure into a real outage -- and would
    # then trip the very alarm this metric feeds.
    aws.heartbeat_fails = RuntimeError("PutMetricData throttled")
    await PzBot.presence.coro(heartbeat_bot)  # must not raise
    heartbeat_bot._update_presence.assert_awaited_once()
