"""Permission gates. These are the real ones -- Discord's own are cosmetic (see guards.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from pzbot import guards
from pzbot.aws import Cost
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


# --- The budget kill-switch (pzserver DESIGN section 12, third layer) ------------------


def spend(stack_usd: float, account_usd: float | None = None):
    return Cost(
        stack_usd=stack_usd,
        account_usd=account_usd if account_usd is not None else stack_usd + 5.0,
        game_hours=41.5,
    )


def test_under_budget_is_allowed(cfg):
    cfg.runtime.monthly_budget_usd = 45.0
    guards.budget(cfg, spend(44.99), is_admin_user=False, override=False)


def test_at_and_over_budget_refuses_the_player_tier(cfg):
    cfg.runtime.monthly_budget_usd = 45.0
    # At exactly 100%, not merely past it -- DESIGN says "at 100%".
    with pytest.raises(Denied, match="100%"):
        guards.budget(cfg, spend(45.0), is_admin_user=False, override=False)
    with pytest.raises(Denied, match=r"\$60\.00"):
        guards.budget(cfg, spend(60.0), is_admin_user=False, override=False)


def test_the_refusal_says_how_to_override_and_where_the_budget_lives(cfg):
    cfg.runtime.monthly_budget_usd = 45.0
    with pytest.raises(Denied) as exc:
        guards.budget(cfg, spend(60.0), is_admin_user=False, override=False)
    assert "override:true" in str(exc.value)
    assert "prod.tfvars" in str(exc.value)


def test_an_admin_may_override(cfg):
    cfg.runtime.monthly_budget_usd = 45.0
    guards.budget(cfg, spend(60.0), is_admin_user=True, override=True)


def test_a_player_may_not_override(cfg):
    # The override flag exists on the command for everyone -- Discord cannot hide a
    # parameter by role -- so the check has to be here, not in the picker.
    cfg.runtime.monthly_budget_usd = 45.0
    with pytest.raises(Denied, match="admin-only"):
        guards.budget(cfg, spend(60.0), is_admin_user=False, override=True)


def test_no_configured_budget_means_no_gate(cfg):
    # An unconfigured ceiling is not a ceiling of zero. Refusing every start because
    # nobody published the parameter would be a self-inflicted outage.
    cfg.runtime.monthly_budget_usd = 0.0
    guards.budget(cfg, spend(9999.0), is_admin_user=False, override=False)


def test_unreadable_cost_explorer_does_not_block_play(cfg):
    # A Cost Explorer outage must not become "nobody can play". The money guarantee is
    # pz-watchdog.sh on the box, which needs neither Discord nor Cost Explorer.
    cfg.runtime.monthly_budget_usd = 45.0
    guards.budget(cfg, None, is_admin_user=False, override=False)


def test_an_unactivated_cost_allocation_tag_does_not_block_play(cfg):
    # stack_usd == 0 while the account has spent something is the signature of the
    # pz:stack tag not being activated (pzserver DEPLOY.md step 1). The tagged figure is
    # then fictional, and gating on it would refuse starts for entirely the wrong reason.
    cfg.runtime.monthly_budget_usd = 45.0
    guards.budget(cfg, spend(0.0, account_usd=250.0), is_admin_user=False, override=False)


def test_a_genuine_zero_spend_is_not_mistaken_for_an_unactivated_tag(cfg):
    # Both figures zero is a real new month, not a broken tag -- and it is under budget
    # anyway, so it must pass for the ordinary reason rather than the escape hatch.
    cfg.runtime.monthly_budget_usd = 45.0
    guards.budget(cfg, spend(0.0, account_usd=0.0), is_admin_user=False, override=False)
