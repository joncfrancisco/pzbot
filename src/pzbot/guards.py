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


def budget(cfg: Config, spend, *, is_admin_user: bool, override: bool) -> None:
    """The third cost-control layer from pzserver DESIGN section 12.

    "At 100%, the bot stops the server and refuses `/pz start` from the player tier;
    pz-admin can override with an explicit `/pz start override:true` that is loudly
    logged."

    The bot already fetched and cached this number for `/pz status` and `/pz cost`; it
    simply never gated on it. Combined with the watchdog missing the crashed-unit case and
    the budget alarm only reaching email, all three of DESIGN's cost-control layers had a
    hole in them.

    FAILS OPEN, in three places, and each is deliberate:

      * `monthly_budget_usd` unset (0.0) -- an unconfigured ceiling is not a ceiling of
        zero. Refusing every start because nobody published the parameter would be a
        self-inflicted outage.
      * `spend is None` -- Cost Explorer was unreachable or errored. `/pz status` already
        treats that as decoration rather than failure, and a Cost Explorer outage must not
        become "nobody can play".
      * stack_usd == 0.0 while the account has spent something -- the signature of the
        `pz:stack` cost allocation tag not being activated (pzserver DEPLOY.md step 1),
        which makes the tagged figure a meaningless $0.00 rather than a real zero.
        Gating on a number we know to be fictional would refuse starts for the wrong
        reason, and `render.cost` already surfaces the condition itself.

    Failing open on a *refusal* is the right direction: the money guarantee is enforced on
    the box by pz-watchdog.sh, which needs neither Discord nor Cost Explorer to work. This
    layer is the polite early stop, not the backstop.
    """
    limit = cfg.runtime.monthly_budget_usd
    if limit <= 0 or spend is None:
        return
    if spend.stack_usd <= 0 < spend.account_usd:
        return
    if spend.stack_usd < limit:
        return

    if override:
        if not is_admin_user:
            raise Denied(
                f"Spend this month is **${spend.stack_usd:.2f}** against a "
                f"**${limit:.2f}** budget, and `override` is admin-only.\n"
                f"Ask <@&{cfg.role_admin}>."
            )
        return  # allowed; commands/core.py is responsible for the audit line

    raise Denied(
        f"**${spend.stack_usd:.2f}** spent this month against a **${limit:.2f}** budget "
        f"(100%).\n\n`/pz start` is paused for the player tier until the budget rolls "
        f"over or is raised.\n\nAn admin can start it anyway with "
        f"`/pz start override:true`, which is logged to the audit channel. The budget "
        f"lives in `pzserver`'s `prod.tfvars` as `monthly_budget_usd`."
    )


def _name(interaction: discord.Interaction) -> str:
    command = interaction.command
    return getattr(command, "qualified_name", "").removeprefix("pz ") or "that"


def is_admin(member: discord.abc.User | discord.Member, cfg: Config) -> bool:
    return isinstance(member, discord.Member) and cfg.role_admin in {r.id for r in member.roles}
