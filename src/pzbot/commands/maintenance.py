"""`/pz version` and `/pz mods` — the two things that change what CODE the world runs.

Split from `/pz config` and `/pz sandbox`, which change what the world is LIKE. The
distinction is not tidiness; it is blast radius. A wrong sandbox value makes a world you
would rather not play. A wrong Steam branch or a mod that will not load makes a world that
does not open at all, and the save it will not open is the one everybody's characters are
in — which is why pzserver DESIGN section 13 deferred both out of v1 until the pre-change
backup path existed to hang them on.

So every command here that changes anything:

* takes a labelled backup FIRST, and refuses the change if that backup fails;
* stops the game around the edit rather than editing underneath it;
* asks, in a message that says what is about to happen, before doing any of it.

`server.py` owns all three. What is here is the asking.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .. import guards, render
from ..guards import Tier
from ..server import STEAM_BRANCHES
from ..singleflight import Busy
from .base import Ctx, Live, busy_embed, confirmed

log = logging.getLogger(__name__)


class VersionGroup(
    app_commands.Group, name="version", description="Which build of the game this server runs"
):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx

    @app_commands.command(description="Which build is installed, and will it update itself?")
    async def status(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()
        await interaction.followup.send(
            embed=render.version(self.ctx.cfg, await self.ctx.server.version_status())
        )

    @app_commands.command(description="Pin the build that is installed now. Stops auto-updates.")
    async def hold(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        status = await self.ctx.server.version_hold(True)

        embed = render.version(self.ctx.cfg, status)
        embed.title = "⏸️  Updates held"
        embed.description = (
            "This world stays on the build it has. Every start would otherwise pull the "
            "latest one, which is the right default and the wrong one on the day a PZ "
            "patch breaks something.\n\n"
            "The hold lives on the **data volume**, so it survives a reboot and an "
            "instance rebuild — it is not a setting for this session only."
        )
        await interaction.followup.send(embed=embed)
        await self.ctx.audit.record(
            interaction, "version hold", detail=f"build={status.get('installed_build')}"
        )

    @app_commands.command(description="Resume automatic updates on the next start")
    async def unhold(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        status = await self.ctx.server.version_hold(False)

        embed = render.version(self.ctx.cfg, status)
        embed.title = "▶️  Updates resumed"
        embed.description = (
            "The next `/pz start` updates to the latest build on this branch before the "
            "world loads. Nothing is downloaded right now — `/pz version update` does "
            "that, with a backup and a restart around it."
        )
        await interaction.followup.send(embed=embed)
        await self.ctx.audit.record(interaction, "version unhold")

    @app_commands.command(description="Update the game now. Backs up, stops, updates, restarts.")
    @app_commands.describe(
        validate="Re-checksum every file against Steam. Slow — for a corrupt install, not a patch."
    )
    async def update(self, interaction: discord.Interaction, validate: bool = False) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        snap = await self.ctx.server.probe()

        embed = discord.Embed(
            title="⚠️  Update the game?",
            colour=render.BAD,
            description=(
                (
                    "This **re-checksums every file** of a several-gigabyte install "
                    "against Steam and re-downloads whatever does not match. It is for a "
                    "corrupt install, not for a patch, and it takes a long time.\n\n"
                    if validate
                    else "This pulls the latest build on the branch this server tracks.\n\n"
                )
                + "In order:\n"
                "1. a labelled backup, and the update is abandoned if it fails;\n"
                "2. the game stops — which saves the world;\n"
                "3. SteamCMD runs;\n"
                "4. the game comes back up on the new build.\n\n"
                "**A PZ update can make an existing save unloadable.** The backup taken in "
                "step 1 is the way back, and `/pz version hold` is how you stay put."
            ),
        )
        embed.add_field(name="Server is", value=f"{snap.stage.emoji} `{snap.stage}`")
        embed.add_field(name="Players online", value=str(snap.player_count))

        if not await confirmed(
            interaction,
            embed,
            label="Validate the install" if validate else "Update the game",
            title="Update cancelled",
            cancelled="Nothing was downloaded and the server was not touched.",
        ):
            return

        live = Live(interaction, "Validating the install" if validate else "Updating the game")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")
        try:
            # Under the same lock as start/stop: this stops and starts the game, and a
            # `/pz stop` landing in the middle would leave SteamCMD writing into an
            # install nothing is going to start.
            async with self.ctx.lock.hold("version update", interaction.user.display_name):
                status = await self.ctx.server.version_update(live, validate=validate)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        await live.finish(embed=_updated_embed(self.ctx, status, validate=validate))
        await self.ctx.audit.record(
            interaction,
            "version validate" if validate else "version update",
            detail=f"build={status.get('installed_build')} branch={status.get('installed_branch')}",
        )

    @app_commands.command(description="Move this server to another Steam branch. Save-breaking.")
    @app_commands.describe(name="The Steam branch to track, e.g. public or unstable")
    async def branch(self, interaction: discord.Interaction, name: str) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        snap = await self.ctx.server.probe()
        current = await self.ctx.server.version_status()

        embed = discord.Embed(
            title="⚠️  Switch Steam branch?",
            colour=render.BAD,
            description=(
                f"From `{current.get('installed_branch') or 'public'}` to **`{name}`**.\n\n"
                "This is the most save-breaking thing this bot can do that is not a "
                "restore. Branches are different *versions of the game* — a Build 41 save "
                "opened by Build 42 is converted and cannot go back, and a Build 42 save "
                "will not open on 41 at all.\n\n"
                "In order: a `before-branch-switch` backup, the game stops, the branch is "
                "pinned, SteamCMD downloads it, the game comes back up.\n\n"
                "**If this goes wrong, the way back is `/pz version branch` to the old "
                "branch and `/pz restore` of that backup — in that order.**"
            ),
        )
        embed.add_field(name="Server is", value=f"{snap.stage.emoji} `{snap.stage}`")
        embed.add_field(name="Players online", value=str(snap.player_count))

        if not await confirmed(
            interaction,
            embed,
            label=f"Switch to {name}"[:80],
            title="Branch switch cancelled",
            cancelled="The server is still on the branch it was on.",
        ):
            return

        live = Live(interaction, f"Switching to {name}")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")
        try:
            async with self.ctx.lock.hold("version branch", interaction.user.display_name):
                status = await self.ctx.server.version_update(live, branch=name)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        await live.finish(embed=_updated_embed(self.ctx, status, branch=name))
        await self.ctx.audit.record(
            interaction,
            "version branch",
            detail=(
                f"{current.get('installed_branch')} -> {name} build={status.get('installed_build')}"
            ),
        )

    @branch.autocomplete("name")
    async def _branch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggestions, not a allowlist — see STEAM_BRANCHES for why it cannot be one."""
        wanted = current.casefold()
        return [
            app_commands.Choice(name=f"{branch} — {what}"[:100], value=branch)
            for branch, what in STEAM_BRANCHES.items()
            if wanted in f"{branch} {what}".casefold()
        ][:25]


def _updated_embed(
    ctx: Ctx, status: dict[str, str], *, validate: bool = False, branch: str = ""
) -> discord.Embed:
    embed = render.version(ctx.cfg, status)
    embed.colour = render.READY
    if branch:
        embed.title = f"✅  Now on `{branch}`"
    elif validate:
        embed.title = "✅  Install validated"
    else:
        embed.title = "✅  Game updated"

    if status.get("applied") == "yes":
        note = "The server was restarted and is loading the world on this build."
    else:
        note = "The game was already stopped; this build loads on the next `/pz start`."
    embed.description = (
        f"{note}\n\nThe world as it was beforehand is in the "
        f"`{status.get('backup_label', 'manual')}` backup — `/pz backup list` to find it."
    )
    return embed


class ModsGroup(app_commands.Group, name="mods", description="Workshop mods this world loads"):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx

    @app_commands.command(name="list", description="Which Workshop items this server loads")
    async def list_(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.PLAYER)
        await interaction.response.defer()
        await interaction.followup.send(
            embed=render.mods(self.ctx.cfg, await self.ctx.server.mods_list())
        )

    @app_commands.command(description="Add a Workshop item. Backs up, stops, edits, restarts.")
    @app_commands.describe(
        workshop_id="The number at the end of the item's Workshop URL (?id=…)",
        mod_ids="The Mod IDs from the item's Workshop page, comma-separated. Empty: work them out.",
        apply="Restart the server so it loads now. Off means: on the next start.",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        workshop_id: str,
        mod_ids: str = "",
        apply: bool = True,
    ) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)

        embed = discord.Embed(
            title="⚠️  Add a mod?",
            colour=render.BAD,
            description=(
                f"Workshop item **`{workshop_id}`**"
                + (f", mods `{mod_ids}`" if mod_ids else "")
                + ".\n\n"
                "A `before-mods` backup is taken first and the change is abandoned if it "
                "fails. **Adding a mod to a world that already exists can make its save "
                "unloadable**, and removing the mod later does not always undo that — "
                "that backup is the way back.\n\n"
                + (
                    "You did not give any Mod IDs, so the server has to download the item "
                    "before this bot can tell what is inside it. That means **two** "
                    "restarts: one to download, one to load.\n\n"
                    if not mod_ids
                    else ""
                )
                + (
                    "The server restarts as part of this."
                    if apply
                    else "`apply` is off, so this only takes effect on the next start."
                )
            ),
        )

        if not await confirmed(
            interaction,
            embed,
            label="Add the mod",
            title="Mod not added",
            cancelled="The mod list was not changed.",
        ):
            return

        live = Live(interaction, f"Adding {workshop_id}")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")
        try:
            async with self.ctx.lock.hold("mods add", interaction.user.display_name):
                report = await self.ctx.server.mods_add(workshop_id, mod_ids, live, apply=apply)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        await live.finish(embed=_mods_embed(self.ctx, report, title=f"Added `{workshop_id}`"))
        await self.ctx.audit.record(
            interaction,
            "mods add",
            detail=f"{workshop_id} mods={report.get('mods_added')} applied={report.get('applied')}",
        )

    @app_commands.command(description="Remove a Workshop item and the mods it brought")
    @app_commands.describe(
        workshop_id="The Workshop item to remove. Start typing to pick one it already has.",
        apply="Restart the server so it stops loading them now.",
    )
    async def remove(
        self, interaction: discord.Interaction, workshop_id: str, apply: bool = True
    ) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)

        embed = discord.Embed(
            title="⚠️  Remove a mod?",
            colour=render.BAD,
            description=(
                f"Workshop item **`{workshop_id}`** and every mod it contributed.\n\n"
                "**Removing a mod from a world that used it can make the save "
                "unloadable** — anything it spawned into the map goes with it. This is "
                "the more dangerous direction, not the safer one.\n\n"
                "A `before-mods` backup is taken first, and the change is abandoned if it "
                "fails."
            ),
        )

        if not await confirmed(
            interaction,
            embed,
            label="Remove the mod",
            title="Mod not removed",
            cancelled="The mod list was not changed.",
        ):
            return

        live = Live(interaction, f"Removing {workshop_id}")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")
        try:
            async with self.ctx.lock.hold("mods remove", interaction.user.display_name):
                report = await self.ctx.server.mods_remove(workshop_id, live, apply=apply)
        except Busy as busy:
            await live.finish(busy_embed(busy))
            return

        await live.finish(embed=_mods_embed(self.ctx, report, title=f"Removed `{workshop_id}`"))
        await self.ctx.audit.record(
            interaction,
            "mods remove",
            detail=(
                f"{workshop_id} mods={report.get('mods_removed')} applied={report.get('applied')}"
            ),
        )

    @remove.autocomplete("workshop_id")
    async def _workshop_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer what is actually installed, so nobody has to go and find the number.

        Deliberately not cached: unlike the S3 backup listing behind `/pz restore`, this
        is server state, and pzserver DESIGN section 10 does not allow caching that. It
        is one SSM round trip per keystroke burst, on an admin-only command.
        """
        try:
            inventory = await self.ctx.server.mods_list()
        except Exception:  # noqa: BLE001 -- an autocomplete may never raise
            # A stopped server, an SSM hiccup, a malformed answer: an autocomplete that
            # raises shows the user nothing at all AND logs a tree error, which is worse
            # than an empty picker. The command itself still explains the real problem.
            log.warning("could not list mods for autocomplete", exc_info=True)
            return []

        wanted = current.casefold()
        choices = []
        for entry in inventory.get("entries", []):
            wid = str(entry.get("workshop_id", ""))
            mods = ", ".join(entry.get("mods") or []) or "no mods listed"
            if wanted and wanted not in f"{wid} {mods}".casefold():
                continue
            choices.append(app_commands.Choice(name=f"{wid} — {mods}"[:100], value=wid))
        return choices[:25]

    @app_commands.command(description="Work out which mods the downloaded Workshop items ship")
    async def scan(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        live = Live(interaction, "Scanning the Workshop items")
        await live.open("Checking the current state\N{HORIZONTAL ELLIPSIS}")
        report = await self.ctx.server.mods_scan(live)

        embed = _mods_embed(self.ctx, report, title="Workshop items scanned")
        if report.get("resolved"):
            embed.insert_field_at(
                0,
                name="⚠️ Restart to load these",
                value=(
                    "They are in `Mods=` now, but PZ read that file when it started. "
                    "`/pz restart` is what actually loads them."
                ),
                inline=False,
            )
        await live.finish(embed=embed)
        await self.ctx.audit.record(
            interaction, "mods scan", detail=f"resolved={report.get('resolved')}"
        )

    @app_commands.command(description="Ask the running server whether its mods have updates")
    async def check(self, interaction: discord.Interaction) -> None:
        guards.check(interaction, self.ctx.cfg, Tier.ADMIN)
        await interaction.response.defer()
        answer = await self.ctx.server.mods_check()

        await interaction.followup.send(
            embed=discord.Embed(
                title="Mod update check",
                colour=render.IDLE,
                description=(
                    "PZ answers this one into its **own log** rather than over RCON, so "
                    "what came back is an acknowledgement, not a verdict — read "
                    "`journalctl -u pzserver` for `CheckModsNeedUpdate`.\n\n"
                    "A Workshop mod updates when the server next starts, so `/pz restart` "
                    "is what picks one up.\n"
                    f"```\n{answer[:900] or '(no reply)'}\n```"
                ),
            )
        )


def _mods_embed(ctx: Ctx, report: dict, *, title: str) -> discord.Embed:
    """The mod list as it now stands, with whatever the change itself needs to say."""
    embed = render.mods(ctx.cfg, report)
    embed.title = f"✅  {title}"
    embed.colour = render.READY

    applied = report.get("applied")
    if applied == "next start":
        embed.insert_field_at(
            0,
            name="Not live yet",
            value=(
                "PZ reads its mod list when it starts, so this takes effect on the next "
                "`/pz start` or `/pz restart`."
            ),
            inline=False,
        )

    if report.get("pending") and not report.get("scanned"):
        embed.insert_field_at(
            0,
            name="⚠️ Listed, but loading nothing",
            value=(
                "The item is in `WorkshopItems=` but nothing is in `Mods=` for it yet — "
                "the server has to download it before this bot can see what is inside. "
                "Start the server, then run `/pz mods scan`."
            ),
            inline=False,
        )

    if report.get("unresolved"):
        embed.insert_field_at(
            0,
            name="⚠️ Its mods may still be loaded",
            value=(
                "This item was added outside this bot and is not on disk, so there was no "
                "way to tell which `Mods=` entries came from it. Check the list below and "
                "clear any leftovers with `/pz config`."
            ),
            inline=False,
        )

    embed.set_footer(text=f"Applied: {applied or 'yes'}")
    return embed
