"""`/pz sandbox` — the world's rules: passage of time, zombies, infection, loot.

Split from `/pz config` because the two behave differently in the way that matters most
to whoever is typing them:

* `/pz config` edits the server `.ini` and takes effect immediately (`reloadoptions`).
* `/pz sandbox` edits `SandboxVars.lua`, which the game only reads at startup — so
  changing one restarts the server, and this command says so before it does.

Both halves of the picker are autocompleted, which is the point: nobody should have to
know that "saliva only" is `ZombieLore.Transmission = 2`.
"""

from __future__ import annotations

import discord
from discord import app_commands

from .. import guards, render
from ..guards import Tier
from ..sandbox import GROUPS, SETTINGS, describe
from ..server import OperationError
from ..singleflight import Busy
from .base import Ctx, Live, busy_embed


class SandboxGroup(app_commands.Group, name="sandbox", description="How the world works"):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx

    @app_commands.command(description="Show the world's settings, or one of them in detail")
    @app_commands.describe(setting="Leave empty for all of them")
    async def get(self, interaction: discord.Interaction, setting: str = "") -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()

        values = await self.ctx.server.sandbox_read()
        if not setting:
            await interaction.followup.send(embed=render.sandbox(self.ctx.cfg, values))
            return

        spec = SETTINGS.get(setting)
        if spec is None:
            raise OperationError(
                f"`{setting}` is not a setting this bot can show. "
                "Start typing and pick one from the list."
            )
        await interaction.followup.send(
            embed=render.sandbox_setting(setting, spec, values.get(setting))
        )

    @app_commands.command(description="Change one of the world's settings")
    @app_commands.describe(
        setting="Which rule to change",
        value="The new value — pick from the list, or type a number",
        apply="Restart the server so it takes effect now. Off means: on the next start.",
    )
    async def set(
        self,
        interaction: discord.Interaction,
        setting: str,
        value: str,
        apply: bool = True,
    ) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)

        spec = SETTINGS.get(setting)
        if spec is None:
            raise OperationError(
                f"`{setting}` is not a setting this bot can change. "
                "Start typing and pick one from the list."
            )

        live = Live(interaction, f"Setting {setting.rpartition('.')[2]}")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")

        try:
            # Under the same lock as start/stop/restart: this path stops and starts the
            # game, and a `/pz stop` landing in the middle of it would leave the world
            # half-configured with the server down.
            async with self.ctx.lock.hold("sandbox set", interaction.user.display_name):
                changed = await self.ctx.server.sandbox_set(setting, value, live, apply=apply)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        embed = discord.Embed(
            title=f"{setting.rpartition('.')[2]} changed",
            colour=render.READY,
            description=(
                f"`{setting}`\n"
                f"was **{describe(setting, changed['was'])}** → "
                f"now **{describe(setting, changed['now'])}**"
            ),
        )

        if changed["applied"] == "yes":
            embed.add_field(
                name="Live",
                value="The server was restarted and is running on the new settings.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Not live yet",
                value=(
                    "The game only reads this file when it starts, so this takes effect "
                    "on the next `/pz start` or `/pz restart`.\n"
                    "⚠️ If the server is running and an admin changes options from inside "
                    "the game, it rewrites this file and will undo the change."
                ),
                inline=False,
            )

        if spec.world_gen_only:
            embed.add_field(
                name="⚠️ Only applies to a new world",
                value=(
                    "The game reads this when it generates the map, so the world you "
                    "already have will not change."
                ),
                inline=False,
            )

        embed.set_footer(text="Previous file kept on the box as *_SandboxVars.lua.pzbot.bak")
        await live.finish(embed=embed)
        await self.ctx.audit.record(
            interaction,
            "sandbox set",
            detail=f"{setting}: {changed['was']} → {changed['now']} (applied={changed['applied']})",
        )

    # --- Autocomplete ------------------------------------------------------------------

    @get.autocomplete("setting")
    @set.autocomplete("setting")
    async def _setting_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        wanted = current.casefold()
        choices = []
        for group, paths in GROUPS.items():
            for path in paths:
                leaf = path.rpartition(".")[2]
                spec = SETTINGS[path]
                if wanted and wanted not in f"{path} {spec.help}".casefold():
                    continue
                choices.append(
                    app_commands.Choice(name=f"{group} · {leaf} — {spec.help}"[:100], value=path)
                )
                if len(choices) == 25:
                    return choices
        return choices

    @set.autocomplete("value")
    async def _value_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer the options for whichever setting is already picked.

        `interaction.namespace` carries the other half-filled parameters, so by the time
        someone tabs into `value` the picker can offer "Saliva only" rather than a blank
        text box and a wiki tab.
        """
        spec = SETTINGS.get(getattr(interaction.namespace, "setting", "") or "")
        if spec is None:
            return []

        if spec.kind == "enum":
            candidates = [f"{label}" for label in spec.options.values()]
        elif spec.kind == "bool":
            candidates = ["true", "false"]
        else:
            candidates = list(spec.suggest) or [f"{spec.low:g}", f"{spec.high:g}"]

        wanted = current.casefold()
        return [
            app_commands.Choice(name=candidate[:100], value=candidate[:100])
            for candidate in candidates
            if wanted in candidate.casefold()
        ][:25]
