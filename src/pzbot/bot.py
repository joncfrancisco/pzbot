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
        # No privileged intents, and no message content: everything this bot does happens
        # through interactions, so it never needs to read what anyone types.
        super().__init__(intents=discord.Intents.none())
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
        """Put the server's state in the member list, where nobody has to ask for it.

        Most `/pz status` calls are really the question "is it up?", and this answers that
        without anyone typing anything. It is also a live probe of the whole path --
        DescribeInstances plus RCON -- so a broken RCON password shows up in the sidebar
        rather than the first time someone tries to stop the server.
        """
        try:
            snap = await self.ctx.server.probe(rcon_timeout=4.0)
        except AwsError:
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
            case AwsError():
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
