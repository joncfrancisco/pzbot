"""Embeds. All the formatting in one place so the command bodies stay about behaviour.

Two conventions worth keeping:

*   Times are rendered as Discord timestamps (`<t:...:R>`), so everyone reads them in
    their own timezone. Half the point of a Discord control plane is that the group is
    scattered across timezones.
*   The connect string is only ever shown next to a `ready` state. Handing someone an
    address for a server that is still loading produces a connection failure and a "the
    bot lied to me" -- which is the exact confusion `Stage.BOOTING` exists to prevent.
"""

from __future__ import annotations

import datetime as dt

import discord

from . import sandbox as sandbox_mod
from .aws import Backup, Cost
from .config import Config, Runtime
from .sandbox import Setting as SandboxSetting
from .server import Snapshot, Stage

READY = discord.Colour(0x3BA55D)
IDLE = discord.Colour(0x5865F2)
BUSY = discord.Colour(0xE67E22)
OFF = discord.Colour(0x4F545C)
BAD = discord.Colour(0xED4245)

STAGE_COLOUR = {
    Stage.READY: READY,
    Stage.STOPPED: OFF,
    Stage.PENDING: BUSY,
    Stage.BOOTING: BUSY,
    Stage.STOPPING: BUSY,
    Stage.UNKNOWN: BAD,
}

STAGE_TEXT = {
    Stage.READY: "Ready",
    Stage.STOPPED: "Stopped",
    Stage.PENDING: "Starting",
    Stage.BOOTING: "Loading the world",
    Stage.STOPPING: "Shutting down",
    Stage.UNKNOWN: "Unknown",
}


def ts(when: dt.datetime, style: str = "R") -> str:
    return f"<t:{int(when.timestamp())}:{style}>"


def duration(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "—"
    seconds = max(int(delta.total_seconds()), 0)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds % 60:02d}s"


def size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _players_field(snap: Snapshot) -> str:
    if snap.stage is not Stage.READY:
        return "—"
    if not snap.player_count:
        return "nobody"
    if snap.players:
        return f"**{snap.player_count}**: " + ", ".join(f"`{p}`" for p in snap.players)
    return f"**{snap.player_count}**"


def status(
    cfg: Config,
    snap: Snapshot,
    runtime: Runtime,
    *,
    last_backup: Backup | None = None,
    spend: Cost | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{snap.stage.emoji}  {STAGE_TEXT[snap.stage]}",
        colour=STAGE_COLOUR[snap.stage],
        timestamp=dt.datetime.now(dt.UTC),
    )

    if snap.stage is Stage.READY:
        embed.description = f"Connect: **`{cfg.connect_string}`**"
    elif snap.stage is Stage.BOOTING:
        embed.description = (
            "The instance is up but the world is still loading. "
            "This is normal for 2–5 minutes after a start."
        )
    elif snap.stage is Stage.STOPPED:
        embed.description = "Nothing is billing except the fixed floor. `/pz start` to play."
    elif snap.stage is Stage.UNKNOWN and snap.rcon_error:
        embed.description = f"⚠️ {snap.rcon_error}"

    embed.add_field(name="Players", value=_players_field(snap))
    embed.add_field(name="Uptime", value=duration(snap.uptime))
    embed.add_field(
        name="Instance",
        value=f"`{snap.instance.state}` · {snap.instance.instance_type or '—'}",
    )

    if snap.stage is Stage.READY:
        embed.add_field(
            name="Idle shutdown",
            value=(
                f"{runtime.idle_timeout_min}m with nobody on "
                f"(warning at {runtime.idle_warn_min}m) · "
                f"session cap {runtime.session_cap_hours}h"
            ),
            inline=False,
        )

    if last_backup:
        when, how_big = ts(last_backup.modified), size(last_backup.size)
        embed.add_field(
            name="Last backup",
            value=f"{when} · {how_big} · `{last_backup.trigger}`",
            inline=False,
        )

    if spend:
        hours = f" · {spend.game_hours:.1f} game hours" if spend.game_hours else ""
        embed.add_field(
            name="Spend this month",
            value=f"**${spend.stack_usd:.2f}** tagged `pz`{hours}",
            inline=False,
        )

    embed.set_footer(text=f"{cfg.stack} · {cfg.game_instance_id} · live, never cached")
    return embed


def who(cfg: Config, snap: Snapshot) -> discord.Embed:
    if snap.stage is not Stage.READY:
        return discord.Embed(
            title=f"{snap.stage.emoji}  {STAGE_TEXT[snap.stage]}",
            description="Nobody is online — the server is not accepting connections yet.",
            colour=STAGE_COLOUR[snap.stage],
        )
    if not snap.player_count:
        return discord.Embed(
            title="🟢  Nobody online",
            description=(
                f"The server is up and empty. It shuts itself down after "
                f"{cfg.runtime.idle_timeout_min} idle minutes."
            ),
            colour=IDLE,
        )
    names = "\n".join(f"• `{p}`" for p in snap.players) or "(names unavailable)"
    return discord.Embed(
        title=f"🟢  {snap.player_count} online",
        description=names,
        colour=READY,
    )


def backups(cfg: Config, items: list[Backup], limit: int = 10) -> discord.Embed:
    embed = discord.Embed(
        title="Backups",
        colour=IDLE,
        description=f"`s3://{cfg.backup_bucket}/backups/{cfg.stack}/`",
    )
    if not items:
        embed.description += "\n\nNo backups found. That is worth investigating."
        embed.colour = BAD
        return embed

    lines = []
    for b in items[:limit]:
        label = f" · `{b.label}`" if b.label else ""
        lines.append(f"{ts(b.modified)} · {size(b.size)} · `{b.trigger}`{label}\n`{b.name}`")
    embed.add_field(name=f"Most recent {min(limit, len(items))}", value="\n\n".join(lines))
    embed.set_footer(text=f"{len(items)} in the bucket · restore with /pz restore <name>")
    return embed


def cost(cfg: Config, spend: Cost) -> discord.Embed:
    embed = discord.Embed(title="Spend, month to date", colour=IDLE)
    embed.add_field(name=f"`pz:stack={cfg.stack}`", value=f"**${spend.stack_usd:.2f}**")
    embed.add_field(name="Whole account", value=f"${spend.account_usd:.2f}")
    if spend.game_hours is not None:
        embed.add_field(name="Game server hours", value=f"{spend.game_hours:.1f}h")
    if spend.stack_usd == 0 and spend.account_usd > 0:
        # The tag-filtered number is silently $0.00 until the cost allocation tag is
        # activated, which is a footgun pzserver DEPLOY.md calls out by name. Showing the
        # account total next to it is what makes that visible rather than reassuring.
        embed.description = (
            "⚠️ The `pz` figure is $0.00 while the account is not — the "
            "`pz:stack` cost allocation tag is probably not activated yet "
            "(pzserver `DEPLOY.md` step 1)."
        )
        embed.colour = BUSY
    embed.set_footer(text="Cost Explorer lags by up to 24 hours; this is cached for 6.")
    return embed


def error(message: str, *, title: str = "That did not work") -> discord.Embed:
    return discord.Embed(title=f"❌  {title}", description=message, colour=BAD)


def progress(title: str, line: str, started: dt.datetime) -> discord.Embed:
    embed = discord.Embed(title=title, description=line, colour=BUSY)
    embed.set_footer(text="This message updates itself.")
    embed.timestamp = started
    return embed


def sandbox(cfg: Config, values: dict[str, str]) -> discord.Embed:
    """The world's rules, grouped the way the in-game sandbox screen groups them."""
    embed = discord.Embed(
        title="Sandbox options",
        colour=IDLE,
        description=(
            "How the **world** works. Unlike `/pz config`, these are only read when the "
            "server starts — changing one restarts it.\n"
            f"`{cfg.server_name}_SandboxVars.lua`"
        ),
    )
    for group, paths in sandbox_mod.GROUPS.items():
        lines = []
        for path in paths:
            leaf = path.rpartition(".")[2]
            raw = values.get(path)
            if raw is None:
                # The file is the source of truth. A setting we know about but this
                # build does not is worth showing as absent rather than hiding.
                lines.append(f"`{leaf}` — *not in this build*")
                continue
            lines.append(f"`{leaf}` **{sandbox_mod.describe(path, raw)}**")
        embed.add_field(name=group, value="\n".join(lines), inline=False)

    embed.set_footer(text="/pz sandbox set to change one · values in brackets are the raw file")
    return embed


def sandbox_setting(path: str, setting: SandboxSetting, current: str | None) -> discord.Embed:
    """One setting in detail, with its options and which one is live."""
    leaf = path.rpartition(".")[2]
    embed = discord.Embed(title=leaf, description=setting.help, colour=IDLE)

    if setting.kind == "enum":
        lines = []
        for number, label in setting.options.items():
            live = current is not None and current.strip() == str(number)
            lines.append(f"{'**▸**' if live else '　'} `{number}` {label}")
        embed.add_field(name="Options", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Accepts", value=setting.summary)

    embed.add_field(
        name="Currently",
        value=f"**{sandbox_mod.describe(path, current)}**" if current else "*not in this build*",
    )
    if setting.world_gen_only:
        embed.add_field(
            name="⚠️ Only applies to a new world",
            value=(
                "The game reads this when it generates the map. Changing it on a world "
                "that already exists does nothing."
            ),
            inline=False,
        )
    embed.set_footer(text=f"/pz sandbox set setting:{path}")
    return embed
