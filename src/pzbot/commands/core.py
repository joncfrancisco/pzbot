"""`/pz` — the commands people actually type.

The bodies here are thin on purpose: permission check, take the single-flight lock if the
command changes state, call into `server.py`, render, audit. Anything that needed a
comment about *how the server works* belongs in `server.py`, not here.

One thing worth knowing before reading: nothing in this file catches its own errors.
`OperationError`, `Denied`, `Busy` and AWS errors all propagate to the tree-level handler
in `bot.py`, which turns them into one consistent embed and one audit line. A command
that swallowed its own exception would be the one that silently stopped auditing.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .. import guards, render
from ..aws import AwsError
from ..config import refresh_runtime
from ..guards import Tier
from ..server import INI_KEYS, OperationError
from ..singleflight import Busy
from .backups import BackupGroup, backup_autocomplete
from .base import Ctx, Live, busy_embed
from .world import SandboxGroup

log = logging.getLogger(__name__)


class PzGroup(app_commands.Group, name="pz", description="Project Zomboid server control"):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__(guild_only=True)
        self.ctx = ctx
        self.add_command(BackupGroup(ctx))
        self.add_command(ConfigGroup(ctx))
        self.add_command(SandboxGroup(ctx))

    # --- Read-only -------------------------------------------------------------------

    @app_commands.command(description="Is the server up, who is on, and what has it cost?")
    async def status(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()

        snap = await self.ctx.server.probe()
        runtime = await refresh_runtime(self.ctx.aws, self.ctx.cfg)

        # Both of these are decoration on a status embed. Neither is allowed to turn a
        # working `/pz status` into an error -- knowing the server is up matters more
        # than knowing what it cost.
        last_backup = None
        try:
            backups = await self.ctx.aws.list_backups(
                self.ctx.cfg.backup_bucket, self.ctx.cfg.stack, limit=1
            )
            last_backup = backups[0] if backups else None
        except AwsError:
            log.warning("could not list backups for /pz status", exc_info=True)

        spend = None
        try:
            spend = await self.ctx.spend.get(
                lambda: self.ctx.aws.month_to_date(self.ctx.cfg.stack, snap.instance.instance_type)
            )
        except AwsError:
            log.warning("could not read Cost Explorer for /pz status", exc_info=True)

        await interaction.followup.send(
            embed=render.status(self.ctx.cfg, snap, runtime, last_backup=last_backup, spend=spend)
        )

    @app_commands.command(description="Who is online right now?")
    async def who(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()
        snap = await self.ctx.server.probe()
        await interaction.followup.send(embed=render.who(self.ctx.cfg, snap))

    @app_commands.command(description="What has the server cost this month?")
    async def cost(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()
        instance = await self.ctx.aws.describe(self.ctx.cfg.game_instance_id)
        spend = await self.ctx.spend.get(
            lambda: self.ctx.aws.month_to_date(self.ctx.cfg.stack, instance.instance_type)
        )
        await interaction.followup.send(embed=render.cost(self.ctx.cfg, spend))

    # --- Lifecycle -------------------------------------------------------------------

    @app_commands.command(description="Start the server and wait until it is ready to play")
    @app_commands.describe(
        override="Start anyway despite the month's budget being spent. Admin only, and logged."
    )
    async def start(self, interaction: discord.Interaction, override: bool = False) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)

        # DESIGN section 12's third cost-control layer. The number is already fetched and
        # cached for /pz status and /pz cost; this is the gate that was missing.
        #
        # Deliberately BEFORE the single-flight lock and before Live opens: a refusal
        # should be a cheap ephemeral no-op, not a progress message that then fails, and
        # it must not hold the lock while it decides.
        spend = None
        if self.ctx.cfg.runtime.monthly_budget_usd > 0:
            await refresh_runtime(self.ctx.aws, self.ctx.cfg)
            try:
                instance = await self.ctx.aws.describe(self.ctx.cfg.game_instance_id)
                spend = await self.ctx.spend.get(
                    lambda: self.ctx.aws.month_to_date(self.ctx.cfg.stack, instance.instance_type)
                )
            except AwsError:
                # See guards.budget: a Cost Explorer outage must not become "nobody can
                # play". The watchdog on the box is the actual money guarantee.
                log.warning("could not read Cost Explorer to gate /pz start", exc_info=True)

        guards.budget(
            self.ctx.cfg,
            spend,
            is_admin_user=guards.is_admin(interaction.user, self.ctx.cfg),
            override=override,
        )

        if override and spend is not None:
            # Loudly logged, per DESIGN. Recorded BEFORE the start, so an override that
            # then fails halfway is still on the record -- the audit trail is about who
            # decided to spend the money, not about whether it worked.
            await self.ctx.audit.record(
                interaction,
                "start override",
                detail=(
                    f"budget override: ${spend.stack_usd:.2f} spent against "
                    f"${self.ctx.cfg.runtime.monthly_budget_usd:.2f}"
                ),
            )
            log.warning(
                "BUDGET OVERRIDE by %s (%s): $%.2f spent against $%.2f",
                interaction.user,
                interaction.user.id,
                spend.stack_usd,
                self.ctx.cfg.runtime.monthly_budget_usd,
            )

        live = Live(interaction, "Starting the server")
        await live.open("Checking what the server is actually doing\N{HORIZONTAL ELLIPSIS}")

        try:
            async with self.ctx.lock.hold("start", interaction.user.display_name):
                snap = await self.ctx.server.start(live)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        runtime = self.ctx.cfg.runtime
        await live.finish(
            content=(
                f"{interaction.user.mention} the server is up \N{EM DASH} "
                f"**`{self.ctx.cfg.connect_string}`**"
            ),
            embed=render.status(self.ctx.cfg, snap, runtime),
        )
        await self.ctx.audit.record(interaction, "start", detail=f"ready as {snap.stage}")

    @app_commands.command(description="Save, back up and shut the server down")
    @app_commands.describe(
        force="Skip the warning period. Admin only. It still saves and backs up first."
    )
    async def stop(self, interaction: discord.Interaction, force: bool = False) -> None:
        # `force` is what raises the bar, exactly as in DESIGN's command table: any
        # player may stop the server, but only an admin may do it out from under the
        # people currently on it.
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN if force else Tier.PLAYER)

        live = Live(interaction, "Stopping the server")
        await live.open("Checking who is online\N{HORIZONTAL ELLIPSIS}")

        try:
            async with self.ctx.lock.hold("stop", interaction.user.display_name):
                await self.ctx.server.stop(live, force=force)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        snap = await self.ctx.server.probe()
        await live.finish(
            embed=render.status(self.ctx.cfg, snap, self.ctx.cfg.runtime),
            content="Saved, backed up, and stopped. Nothing is billing but the fixed floor.",
        )
        await self.ctx.audit.record(interaction, "stop", detail=f"force={force}")

    @app_commands.command(description="Restart the game without stopping the instance")
    async def restart(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        live = Live(interaction, "Restarting the server")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")

        try:
            async with self.ctx.lock.hold("restart", interaction.user.display_name):
                snap = await self.ctx.server.restart(live)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        await live.finish(
            embed=render.status(self.ctx.cfg, snap, self.ctx.cfg.runtime),
            content=f"Back up \N{EM DASH} **`{self.ctx.cfg.connect_string}`**",
        )
        await self.ctx.audit.record(interaction, "restart")

    @app_commands.command(description="Force a save right now, without restarting")
    async def save(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        output = await self.ctx.server.save()
        embed = discord.Embed(
            title="\N{FLOPPY DISK}  Saved",
            description=f"```\n{output[:800] or 'save'}\n```",
            colour=render.READY,
        )
        await interaction.followup.send(embed=embed)
        await self.ctx.audit.record(interaction, "save")

    # --- Knobs -----------------------------------------------------------------------

    @app_commands.command(description="Retune the idle shutdown for this session")
    @app_commands.describe(
        minutes="Minutes with nobody online before the server stops itself (5-1440), or `off`."
    )
    async def idle(self, interaction: discord.Interaction, minutes: str) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()

        cap_minutes = self.ctx.cfg.runtime.session_cap_hours * 60
        if minutes.strip().lower() in ("off", "never", "disable"):
            # There is no true "off". The watchdog's timeout is the thing keeping an
            # m7i.xlarge from billing all night, so `off` means "as late as the session
            # cap allows" -- which is a real backstop rather than an imaginary one.
            timeout = cap_minutes
            note = (
                f"Idle shutdown is now as late as it can be: **{timeout} minutes**, which is "
                f"the {self.ctx.cfg.runtime.session_cap_hours}h session cap. There is no "
                "true off \N{EM DASH} the watchdog is the cost guarantee."
            )
        else:
            try:
                timeout = int(minutes)
            except ValueError:
                raise OperationError(
                    f"`{minutes}` is not a number of minutes (or `off`)."
                ) from None
            note = f"Idle shutdown is now **{timeout} minutes** with nobody online."

        rendered = await self.ctx.server.set_idle(timeout, timeout - 5)
        embed = discord.Embed(
            title="\N{ALARM CLOCK}  Idle shutdown retuned",
            description=(
                f"{note}\n\nThis lasts until the server next boots \N{EM DASH} the game host "
                "re-reads Parameter Store on every start. To change it permanently, set "
                "`idle_timeout_minutes` in `pzserver`'s `prod.tfvars` and apply."
            ),
            colour=render.IDLE,
        )
        embed.add_field(name="On the box", value=f"```\n{rendered[:400]}\n```", inline=False)
        await interaction.followup.send(embed=embed)
        await self.ctx.audit.record(interaction, "idle", detail=f"timeout={timeout}m")

    # --- The dangerous one -----------------------------------------------------------

    @app_commands.command(description="Roll the world back to a backup. Destructive.")
    @app_commands.describe(backup="Which backup to restore. Start typing to search.")
    @app_commands.autocomplete(backup=backup_autocomplete)
    async def restore(self, interaction: discord.Interaction, backup: str) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)

        snap = await self.ctx.server.probe()
        confirm = ConfirmRestore(interaction.user.id)
        embed = discord.Embed(
            title="\N{WARNING SIGN}  Restore the world?",
            colour=render.BAD,
            description=(
                f"This replaces the live world with **`{backup}`**.\n\n"
                "In order:\n"
                "1. `systemctl stop pzserver` \N{EM DASH} which saves the current world;\n"
                "2. a `prerestore` backup of what is there now, so this is reversible;\n"
                "3. the restore itself, which refuses a partial archive;\n"
                "4. the server comes back up on the restored world.\n\n"
                "**Everything that happened after that backup was taken will be gone.**"
            ),
        )
        embed.add_field(name="Server is", value=f"{snap.stage.emoji} `{snap.stage}`")
        embed.add_field(name="Players online", value=str(snap.player_count))
        embed.set_footer(text="This confirmation expires in 60 seconds.")

        await interaction.response.send_message(embed=embed, view=confirm)
        await confirm.wait()

        if not confirm.value:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Restore cancelled",
                    description="Nothing was changed.",
                    colour=render.OFF,
                ),
                view=None,
            )
            return

        live = Live(interaction, f"Restoring {backup}")
        await live.open("Starting the restore\N{HORIZONTAL ELLIPSIS}")
        try:
            async with self.ctx.lock.hold("restore", interaction.user.display_name):
                result = await self.ctx.server.restore(backup, live)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        done = discord.Embed(
            title="\N{WHITE HEAVY CHECK MARK}  Restored",
            colour=render.READY,
            description=(
                f"The world is now **`{backup}`**, and the server is starting back up. "
                "Give it a few minutes and check `/pz status`.\n\n"
                "The world it replaced is still on the data volume as `*.pre-restore-*` "
                "and in S3 as a `prerestore` backup."
            ),
        )
        done.add_field(name="Output", value=f"```\n{result.output[-900:]}\n```", inline=False)
        await live.finish(embed=done)
        await self.ctx.audit.record(interaction, "restore", detail=backup)


class ConfirmRestore(discord.ui.View):
    """Two-step confirmation, bound to the person who asked.

    The `user_id` check is not paranoia about attackers -- it is about the far more
    likely accident of someone else in the channel clicking the red button on a
    confirmation they did not read.
    """

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.value = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This confirmation belongs to whoever ran the command.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Restore the world", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


class ConfigGroup(app_commands.Group, name="config", description="Read and write server options"):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx

    @app_commands.command(description="Show the server options this bot can change")
    async def get(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        values = await self.ctx.server.ini_read()

        embed = discord.Embed(
            title="Server options",
            colour=render.IDLE,
            description=f"`{self.ctx.server.ini_path}` \N{EM DASH} allowlisted keys only.",
        )
        for key, spec in INI_KEYS.items():
            embed.add_field(
                name=key,
                value=f"`{values.get(key, '(unset)')}`\n{spec.help}",
                inline=True,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(description="Change one server option and reload it")
    @app_commands.describe(key="Which option", value="The new value")
    async def set(self, interaction: discord.Interaction, key: str, value: str) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        message = await self.ctx.server.ini_write(key, value)
        await interaction.followup.send(
            embed=discord.Embed(title="Option changed", description=message, colour=render.READY)
        )
        await self.ctx.audit.record(interaction, "config set", detail=f"{key}={value}")

    @set.autocomplete("key")
    async def _key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=f"{key} — {spec.help}"[:100], value=key)
            for key, spec in INI_KEYS.items()
            if current in key.lower()
        ][:25]
