"""Permission gates. These are the real ones -- Discord's own are cosmetic (see guards.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from pzbot import guards
from pzbot.guards import Denied, Tier

PLAYER_ROLE = 333
ADMIN_ROLE = 222


def member(*role_ids: int):
    fake = MagicMock(spec=discord.Member)
    fake.id = 42
    fake.roles = [SimpleNamespace(id=r) for r in role_ids]
    return fake


def interaction(user, *, guild_id: int = 111, channel_id: int = 444):
    fake = MagicMock(spec=discord.Interaction)
    fake.guild_id = guild_id
    fake.channel_id = channel_id
    fake.user = user
    fake.command = SimpleNamespace(qualified_name="pz stop")
    return fake


def test_a_player_may_run_player_commands(cfg):
    guards.check(interaction(member(PLAYER_ROLE)), cfg, Tier.PLAYER)


def test_a_player_may_not_run_admin_commands(cfg):
    with pytest.raises(Denied, match="admin-only"):
        guards.check(interaction(member(PLAYER_ROLE)), cfg, Tier.ADMIN)


def test_an_admin_is_also_a_player(cfg):
    # Without this, an account that holds only the admin role cannot run /pz start --
    # which is exactly the account most likely to be doing it at 3am.
    guards.check(interaction(member(ADMIN_ROLE)), cfg, Tier.PLAYER)
    guards.check(interaction(member(ADMIN_ROLE)), cfg, Tier.ADMIN)


def test_someone_with_no_roles_is_refused(cfg):
    with pytest.raises(Denied):
        guards.check(interaction(member()), cfg, Tier.PLAYER)


def test_every_member_is_a_player_when_no_player_role_is_configured(cfg):
    cfg.role_player = 0
    guards.check(interaction(member()), cfg, Tier.PLAYER)
    with pytest.raises(Denied):
        guards.check(interaction(member()), cfg, Tier.ADMIN)


def test_another_guild_is_ignored_entirely(cfg):
    # The leaked-token case: the bot gets invited somewhere else and must do nothing.
    with pytest.raises(Denied, match="not configured"):
        guards.check(interaction(member(ADMIN_ROLE), guild_id=999), cfg, Tier.PLAYER)


def test_the_wrong_channel_is_refused(cfg):
    with pytest.raises(Denied, match="only works in"):
        guards.check(interaction(member(ADMIN_ROLE), channel_id=99), cfg, Tier.PLAYER)


def test_dms_are_refused(cfg):
    user = MagicMock(spec=discord.User)  # a User, not a Member: no roles to check
    with pytest.raises(Denied, match="DMs"):
        guards.check(interaction(user), cfg, Tier.PLAYER)


def test_is_admin(cfg):
    assert guards.is_admin(member(ADMIN_ROLE), cfg)
    assert not guards.is_admin(member(PLAYER_ROLE), cfg)
    assert not guards.is_admin(MagicMock(spec=discord.User), cfg)
