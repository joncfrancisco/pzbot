"""The gateway client: wiring, command registration, and one error handler for everything.

Commands are registered **to one guild**, not globally. Two reasons, and the second is
the important one: guild commands appear instantly instead of propagating for an hour,
and a bot whose commands only exist in the guild it was configured for is a bot that does
nothing useful anywhere else. That is a real mitigation for the leaked-token case, and it
pairs with the guild check in `guards.py` rather than replacing it.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from . import guards, render
from .audit import Audit
from .aws import Aws, AwsError
from .commands.base import Ctx
from .commands.core import PzGroup
from .config import Config
from .server import GameServer, OperationError, Stage
from .singleflight import SingleFlight

log = logging.getLogger(__name__)


class PzBot(discord.Client):
    def __init__(self, cfg: Config, aws: Aws) -> None:
        # No privileged intents (Members, Presences, Message Content) and no message
        # content: every command here happens through interactions, so the bot never
        # needs to read what anyone types. GUILDS is not privileged, though, and
        # discord.py wants it regardless -- without it there is no guild/channel cache,
        # so `audit.py`'s channel lookup falls back to an HTTP fetch on every single
        # audit line instead of using it.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.ctx = Ctx(
            cfg=cfg,
            aws=aws,
            server=GameServer(aws, cfg),
            lock=SingleFlight(),
            audit=Audit(self, cfg.channel_audit),
        )
        self.guild = discord.Object(id=cfg.guild_id)
        self.tree.add_command(PzGroup(self.ctx), guild=self.guild)
        self.tree.on_error = self.on_command_error

    async def setup_hook(self) -> None:
        synced = await self.tree.sync(guild=self.guild)
        log.info("synced %d commands to guild %s", len(synced), self.cfg.guild_id)
        self.presence.start()

    async def on_ready(self) -> None:
        log.info(
            "connected as %s, watching %s (%s) in %s",
            self.user,
            self.cfg.game_instance_id,
            self.cfg.connect_string,
            self.cfg.region,
        )

    # --- Presence --------------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def presence(self) -> None:
        """Put the server's state in the member list, and prove this process is alive.

        Most `/pz status` calls are really the question "is it up?", and this answers that
        without anyone typing anything. It is also a live probe of the whole path --
        DescribeInstances plus RCON -- so a broken RCON password shows up in the sidebar
        rather than the first time someone tries to stop the server.
        """
        try:
            await self._update_presence()
        except Exception:  # noqa: BLE001 -- see below; this loop may never die
            # `tasks.loop` stops permanently on an unhandled exception, and nothing
            # restarts it. That makes any escape from here strictly worse than the thing
            # that escaped: presence freezes on a stale reading AND the heartbeat below
            # stops forever, so the one alarm built to catch "healthy host, dead bot"
            # fires because its own publisher was killed by a `players` response the RCON
            # parser did not like. `_heartbeat` is already written this way and says so;
            # the probe is the larger surface and needs the same rule.
            log.exception("presence update failed")
        finally:
            # In a `finally`, and therefore published even when the probe above failed.
            # The heartbeat's claim is "this event loop completed a cycle", not "AWS is
            # healthy" -- a transient DescribeInstances error must not page someone about
            # a bot that is in fact running fine. It is published AFTER the probe rather
            # than before so that it still requires a full cycle of real work, which is
            # what stops it degenerating into a liveness check on the timer itself.
            await self._heartbeat()

    async def _heartbeat(self) -> None:
        """PZ/BotAlive=1. Never allowed to raise -- see the alarm's own comment.

        pzserver's EC2 status-check alarms cover a dead host. This covers the failure they
        structurally cannot see: healthy instance, running process, wedged event loop.
        """
        try:
            await self.ctx.aws.put_heartbeat(self.cfg.metric_namespace, self.cfg.stack)
        except Exception:  # noqa: BLE001 -- a failed heartbeat must not kill the loop
            # Deliberately not fatal, and deliberately not silent: the alarm will notice
            # the absence on its own, and this line is what explains it afterwards.
            log.warning("could not publish the BotAlive heartbeat", exc_info=True)

    async def _update_presence(self) -> None:
        try:
            snap = await self.ctx.server.probe(rcon_timeout=4.0)
        except (*AwsError, OperationError):
            # Unpacked, not `(AwsError, OperationError)`. `AwsError` is a *tuple* of
            # classes, and a nested tuple in an `except` raises TypeError("catching
            # classes that do not inherit from BaseException") when the clause is
            # evaluated -- so this handler did nothing except turn every AWS failure into
            # a different exception. The loop survived on `presence`'s outer catch-all,
            # which is why it never showed up as anything but a misleading log line.
            # Same trap as `case AwsError():` further down; see the comment there.
            #
            # A stopped stack, a mid-apply AccessDenied, a game server that no longer
            # exists: all of them are things to say in the log and re-check in sixty
            # seconds, not reasons to stop watching.
            log.warning("presence probe failed", exc_info=True)
            return

        match snap.stage:
            case Stage.READY if snap.player_count:
                text = f"{snap.player_count} online · {self.cfg.connect_string}"
            case Stage.READY:
                text = f"up and empty · {self.cfg.connect_string}"
            case Stage.BOOTING | Stage.PENDING:
                text = "loading the world…"
            case Stage.STOPPING:
                text = "shutting down…"
            case Stage.STOPPED:
                text = "stopped · /pz start"
            case _:
                text = "state unknown"

        try:
            # "Watching …" rather than a custom status: custom statuses are a user
            # feature that bots have only patchy support for, and a presence that
            # silently fails every 60 seconds is worse than a slightly plainer one.
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name=text[:128]),
                status=discord.Status.online if snap.stage is Stage.READY else discord.Status.idle,
            )
        except discord.HTTPException as exc:
            log.warning("could not update presence: %s", exc)

    @presence.before_loop
    async def _before_presence(self) -> None:
        await self.wait_until_ready()

    # --- One error handler -----------------------------------------------------------

    async def on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Turn every failure into one embed and one audit line.

        Errors are ephemeral: a refusal or a stack trace is between the bot and the person
        who typed the command, and putting either in the channel just adds noise to a
        channel that exists for coordinating a game.
        """
        original = getattr(error, "original", error)
        action = getattr(interaction.command, "qualified_name", "?").removeprefix("pz ")

        match original:
            case guards.Denied():
                embed = render.error(str(original), title="Not allowed")
                outcome = "denied"
            case OperationError():
                embed = render.error(str(original))
                outcome = "failed"
            case _ if isinstance(original, AwsError):
                # AwsError is `(ClientError, BotoCoreError)` -- a tuple, which `except`
                # accepts but a `case` class pattern does not. `case AwsError():` raises
                # `TypeError: called match pattern must be a class` at match time, which
                # took down the error handler itself and hid the real failure (an IAM
                # AccessDenied) behind a second, unrelated crash.
                log.exception("AWS call failed during /pz %s", action)
                embed = render.error(
                    "AWS refused or timed out on that. The stack itself is probably fine "
                    f"— check `journalctl -u pzbot` on the bot host.\n```\n{original}\n```"[:1800],
                    title="AWS said no",
                )
                outcome = "failed"
            case _:
                log.exception("unhandled error during /pz %s", action)
                embed = render.error(
                    "Something unexpected broke. The details are in `journalctl -u pzbot` "
                    "on the bot host.\n"
                    f"```\n{type(original).__name__}: {original}\n```"[:1800],
                    title="Unexpected error",
                )
                outcome = "failed"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.exception("could not deliver the error message for /pz %s", action)

        await self.ctx.audit.record(
            interaction, action, outcome=outcome, detail=str(original)[:900]
        )
