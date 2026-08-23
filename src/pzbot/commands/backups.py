"""`/pz backup now` and `/pz backup list`, plus the autocomplete `/pz restore` leans on."""

from __future__ import annotations

import discord
from discord import app_commands

from .. import guards, render
from ..guards import Tier
from .base import Ctx


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


async def backup_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Names for `/pz restore`, so nobody has to retype a 60-character S3 key.

    This is also the first line of defence for the restore path: the value that reaches
    the command is one the bot itself produced. `GameServer.restore` re-checks it against
    the live listing anyway, because an autocomplete value can be typed by hand.
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
