"""Who may run what, and where.

pzserver DESIGN section 10 specifies two gates. Only the second one is real:

1.  Discord's own `default_member_permissions`, which decides whether `/pz` appears in
    someone's command picker. It is worth setting, but it is **top-level only** --
    Discord applies command permissions to `/pz`, not to `/pz restore` -- so it cannot
    hide the admin subcommands from a player. Cosmetic, and not load-bearing.
2.  A role-id check in this file, against ids read from Parameter Store. Interaction
    payloads are signed by Discord and carry the member's role list, so those ids are
    trustworthy -- but the check has to happen here, not be assumed from gate 1.

Plus two allowlists that exist for the leaked-token case: a guild allowlist (the bot
ignores every interaction from a guild it was not configured for, so an attacker who
invites it elsewhere gets nothing) and a channel allowlist.
"""

from __future__ import annotations

import enum

import discord

from .config import Config


class Tier(enum.StrEnum):
    PLAYER = "player"
    ADMIN = "admin"


class Denied(Exception):
    """Refusal, with a message that says what to do about it."""


def check(interaction: discord.Interaction, cfg: Config, tier: Tier) -> None:
    if interaction.guild_id != cfg.guild_id:
        # Deliberately vague to the caller, specific in the log: a bot in a guild it does
        # not serve should not confirm which guild it does serve.
        raise Denied("This bot is not configured for this server.")

    if cfg.channels_allowed and interaction.channel_id not in cfg.channels_allowed:
        channels = ", ".join(f"<#{c}>" for c in cfg.channels_allowed)
        raise Denied(f"`/pz` only works in {channels}.")

    member = interaction.user
    if not isinstance(member, discord.Member):
        raise Denied("`/pz` only works inside the server, not in DMs.")

    role_ids = {r.id for r in member.roles}

    if tier is Tier.ADMIN:
        if cfg.role_admin not in role_ids:
            raise Denied(f"`/pz {_name(interaction)}` is admin-only (<@&{cfg.role_admin}>).")
        return

    # Admins are players too. Without this, an admin-only account cannot run `/pz start`.
    if cfg.role_player and not {cfg.role_player, cfg.role_admin} & role_ids:
        raise Denied(f"You need <@&{cfg.role_player}> to use `/pz`.")


def _name(interaction: discord.Interaction) -> str:
    command = interaction.command
    return getattr(command, "qualified_name", "").removeprefix("pz ") or "that"


def is_admin(member: discord.abc.User | discord.Member, cfg: Config) -> bool:
    return isinstance(member, discord.Member) and cfg.role_admin in {r.id for r in member.roles}
