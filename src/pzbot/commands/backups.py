"""`/pz backup now`, `list` and `download`, plus the autocomplete `/pz restore` leans on."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands

from .. import guards, render
from ..aws import AwsError
from ..guards import Tier
from ..server import (
    DOWNLOAD_TTL_DEFAULT,
    DOWNLOAD_TTL_MAX,
    DOWNLOAD_TTL_MIN,
    OperationError,
    Stage,
)
from .base import Ctx

log = logging.getLogger(__name__)


async def backup_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Names for `/pz restore` and `/pz backup download`, so nobody retypes a 60-character key.

    This is also the first line of defence for the restore path: the value that reaches
    the command is one the bot itself produced. `GameServer.restore` and
    `GameServer.download_url` both re-check it against the live listing anyway, because
    an autocomplete value can be typed by hand.
    """
    ctx: Ctx = interaction.client.ctx  # type: ignore[attr-defined]
    try:
        items = await ctx.backups.get(
            lambda: ctx.aws.list_backups(ctx.cfg.backup_bucket, ctx.cfg.stack)
        )
    except Exception:  # noqa: BLE001 -- an autocomplete may not raise into the picker
        return []

    current = current.lower()
    choices = []
    for b in items:
        if current and current not in b.name.lower():
            continue
        label = (
            f"{b.stamp} · {b.trigger}{' · ' + b.label if b.label else ''} · {render.size(b.size)}"
        )
        choices.append(app_commands.Choice(name=label[:100], value=b.name))
        if len(choices) == 25:
            break
    return choices


class BackupGroup(app_commands.Group, name="backup", description="Snapshots of the world"):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx

    @app_commands.command(description="Take a labelled backup now, without stopping the server")
    @app_commands.describe(label="Optional tag for the archive, e.g. `before-mod-update`")
    async def now(self, interaction: discord.Interaction, label: str = "") -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()

        # No single-flight lock: `pz-backup.sh` forces its own RCON save and is safe to
        # run alongside the scheduled timer. Holding the lifecycle lock here would mean a
        # 20-minute archive of a large world blocks `/pz stop`, which is worse.
        result = await self.ctx.server.backup_now(label)
        self.ctx.backups.invalidate()

        embed = discord.Embed(
            title="Backup taken" if result.ok else "Backup failed",
            colour=render.READY if result.ok else render.BAD,
            description=f"```\n{result.output[-1200:] or result.status}\n```",
        )
        await interaction.followup.send(embed=embed)
        await self.ctx.audit.record(
            interaction,
            "backup now",
            outcome="ok" if result.ok else "failed",
            detail=label or "(unlabelled)",
        )

    @app_commands.command(name="list", description="What backups exist?")
    @app_commands.describe(count="How many to show (default 10)")
    async def list_(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 25] = 10
    ) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()
        items = await self.ctx.aws.list_backups(self.ctx.cfg.backup_bucket, self.ctx.cfg.stack)
        await interaction.followup.send(embed=render.backups(self.ctx.cfg, items, limit=count))

    @app_commands.command(description="Private, time-limited link to download a world save")
    @app_commands.describe(
        backup="Which archive. Leave this blank for the most recent one.",
        minutes=(
            f"How long the link stays valid "
            f"({DOWNLOAD_TTL_MIN}\N{EN DASH}{DOWNLOAD_TTL_MAX}, default {DOWNLOAD_TTL_DEFAULT})."
        ),
    )
    @app_commands.autocomplete(backup=backup_autocomplete)
    async def download(
        self,
        interaction: discord.Interaction,
        backup: str = "",
        minutes: app_commands.Range[int, DOWNLOAD_TTL_MIN, DOWNLOAD_TTL_MAX] = DOWNLOAD_TTL_DEFAULT,
    ) -> None:
        """A presigned S3 GET, sent only to the admin who asked for it.

        Admin rather than player for the same reason `/pz restore` is: the archive
        carries `db/`, which is where PZ keeps player accounts. `/pz backup list` can
        stay open to everyone, because knowing an archive exists is not the same as
        being able to hold it.

        Ephemeral, and no single-flight lock. Signing changes nothing on the game server
        and does not even talk to it, so queueing this behind a `/pz start` would only
        mean an admin cannot get a copy of the world during the exact ten minutes
        something is going wrong with it.
        """
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer(ephemeral=True)

        try:
            stage = (await self.ctx.server.probe()).stage
        except (*AwsError, OperationError):
            # Decoration on the embed, never a gate. Whether the archive is the *current*
            # world depends on what the server is doing, which is worth saying -- but
            # "EC2 is unreachable", or "the instance was replaced and nothing carries the
            # tag yet", are among the moments an admin most wants a copy of the save.
            # Refusing them the link over a missing footnote would be absurd, and
            # `_describe_game` raises OperationError rather than AwsError for the second
            # of those, so catching only AwsError here would do exactly that.
            log.warning("could not probe the game server for /pz backup download", exc_info=True)
            stage = Stage.UNKNOWN

        item, url, ttl = await self.ctx.server.download_url(backup.strip(), minutes=minutes)
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)

        await interaction.followup.send(
            embed=render.download(self.ctx.cfg, item, url, expires_at, stage=stage),
            ephemeral=True,
        )
        # The name and the window, never the URL. The audit channel is not ephemeral, and
        # a link posted there would outlive the message that was careful to hide it.
        await self.ctx.audit.record(
            interaction,
            "backup download",
            detail=f"{item.name} ({render.size(item.size)}), link valid {ttl // 60}m",
        )
