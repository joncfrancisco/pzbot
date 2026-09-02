"""The command surface itself, and the one rendering invariant that matters."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from conftest import FakeRcon, backup
from pzbot import render, sandbox
from pzbot.audit import Audit
from pzbot.aws import Cost, Instance
from pzbot.commands.base import Ctx
from pzbot.commands.core import PzGroup
from pzbot.config import Runtime
from pzbot.server import GameServer, Snapshot, Stage
from pzbot.singleflight import SingleFlight

# The command table in pzserver DESIGN section 10, plus the version and mod management
# that DESIGN section 13 deferred out of v1 and has since landed. If this list changes,
# the design doc and this test should change together -- that is the point of asserting
# on it.
EXPECTED = {
    "pz status",
    "pz start",
    "pz stop",
    "pz who",
    "pz restart",
    "pz save",
    "pz backup now",
    "pz backup list",
    "pz restore",
    "pz config get",
    "pz config set",
    "pz sandbox get",
    "pz sandbox set",
    "pz idle",
    "pz cost",
    "pz version status",
    "pz version hold",
    "pz version unhold",
    "pz version update",
    "pz version branch",
    "pz mods list",
    "pz mods add",
    "pz mods remove",
    "pz mods scan",
    "pz mods check",
}


@pytest.fixture
def group(cfg, aws) -> PzGroup:
    server = GameServer(aws, cfg)
    server.rcon = FakeRcon()
    ctx = Ctx(
        cfg=cfg,
        aws=aws,
        server=server,
        lock=SingleFlight(),
        audit=Audit(MagicMock(spec=discord.Client), cfg.channel_audit),
    )
    return PzGroup(ctx)


def walk(group) -> set[str]:
    names = set()
    for command in group.commands:
        if isinstance(command, discord.app_commands.Group):
            names |= walk(command)
        else:
            names.add(command.qualified_name)
    return names


def test_the_command_surface_matches_the_design(group):
    assert walk(group) == EXPECTED


def test_stop_takes_a_force_flag(group):
    stop = next(c for c in group.commands if c.name == "stop")
    assert "force" in {p.name for p in stop.parameters}


def test_restore_offers_autocomplete_rather_than_a_free_text_key(group):
    restore = next(c for c in group.commands if c.name == "restore")
    assert restore._params["backup"].autocomplete is not None


def test_removing_a_mod_offers_the_ones_installed(group):
    # Nobody knows a Workshop id by heart, and typing the wrong one removes a mod the
    # world is using. The picker is what stops that being a free-text field.
    mods = next(c for c in group.commands if c.name == "mods")
    remove = next(c for c in mods.commands if c.name == "remove")
    assert remove._params["workshop_id"].autocomplete is not None


def test_the_group_is_guild_only(group):
    # Belt and braces with the guild check in guards.py: /pz should not exist in DMs.
    assert group.guild_only


# --- Rendering -------------------------------------------------------------------------


def snapshot(stage: Stage, players: list[str] | None = None) -> Snapshot:
    players = players or []
    state = {
        Stage.STOPPED: "stopped",
        Stage.PENDING: "pending",
        Stage.STOPPING: "stopping",
    }.get(stage, "running")
    instance = Instance(
        instance_id="i-0test",
        state=state,
        private_ip="10.20.1.171",
        public_ip="34.233.59.251",
        launch_time=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        instance_type="m7i.xlarge",
    )
    return Snapshot(instance, stage, players, len(players))


@pytest.mark.parametrize("stage", list(Stage))
def test_the_connect_string_is_only_shown_when_the_server_is_ready(cfg, stage):
    # Handing someone an address for a server that is still loading produces a failed
    # connection and a bot nobody trusts. Stage.BOOTING exists to prevent exactly this.
    embed = render.status(cfg, snapshot(stage), Runtime())
    body = (embed.description or "") + "".join(f.value or "" for f in embed.fields)
    assert (cfg.connect_string in body) == (stage is Stage.READY)


@pytest.mark.parametrize("stage", list(Stage))
def test_every_stage_renders(cfg, stage):
    embed = render.status(
        cfg,
        snapshot(stage, ["Bob"] if stage is Stage.READY else []),
        Runtime(),
        last_backup=backup("2026-08-22T19-24-30Z__scheduled.tar.zst"),
        spend=Cost(12.34, 56.78, 41.5),
    )
    assert embed.title
    assert len(embed) <= 6000  # Discord's hard limit on total embed length


def test_a_zero_stack_cost_next_to_a_real_account_cost_is_flagged(cfg):
    # Silently reporting $0.00 would read as "we spent nothing" rather than "the cost
    # allocation tag was never activated".
    embed = render.cost(cfg, Cost(stack_usd=0.0, account_usd=56.78, game_hours=None))
    assert "cost allocation tag" in (embed.description or "")


def test_backups_embed_says_so_when_there_are_none(cfg):
    embed = render.backups(cfg, [])
    assert "No backups" in (embed.description or "")
    assert embed.colour == render.BAD


def test_sizes_and_durations_read_like_english():
    assert render.size(900) == "900 B"
    assert render.size(1536) == "1.5 KiB"
    assert render.size(5 * 1024**3) == "5.0 GiB"
    assert render.duration(dt.timedelta(seconds=90)) == "1m 30s"
    assert render.duration(dt.timedelta(hours=3, minutes=5)) == "3h 05m"
    assert render.duration(None) == "—"


# --- Sandbox -------------------------------------------------------------------------------


@pytest.fixture
def sandbox_group(group):
    return next(c for c in group.commands if c.name == "sandbox")


def test_both_halves_of_the_sandbox_picker_are_autocompleted(sandbox_group):
    # Nobody should have to know that "saliva only" is ZombieLore.Transmission = 2, so
    # both the setting and its value come from the picker.
    get = next(c for c in sandbox_group.commands if c.name == "get")
    set_ = next(c for c in sandbox_group.commands if c.name == "set")
    assert get._params["setting"].autocomplete is not None
    assert set_._params["setting"].autocomplete is not None
    assert set_._params["value"].autocomplete is not None


async def test_setting_autocomplete_searches_help_text_as_well_as_names(sandbox_group):
    interaction = MagicMock(spec=discord.Interaction)
    choices = await sandbox_group._setting_autocomplete(interaction, "infection")
    values = [c.value for c in choices]
    assert "ZombieLore.Transmission" in values
    assert all(len(c.name) <= 100 for c in choices)


async def test_value_autocomplete_offers_the_options_for_the_chosen_setting(sandbox_group):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.namespace = SimpleNamespace(setting="ZombieLore.Transmission")

    choices = await sandbox_group._value_autocomplete(interaction, "")
    assert [c.value for c in choices] == ["Blood + Saliva", "Saliva only", "Everyone's infected"]

    narrowed = await sandbox_group._value_autocomplete(interaction, "saliva")
    assert [c.value for c in narrowed] == ["Blood + Saliva", "Saliva only"]


async def test_value_autocomplete_suggests_numbers_for_numeric_settings(sandbox_group):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.namespace = SimpleNamespace(setting="ZombieConfig.PopulationMultiplier")
    choices = await sandbox_group._value_autocomplete(interaction, "")
    assert "1.0" in [c.value for c in choices]


async def test_value_autocomplete_is_empty_until_a_setting_is_picked(sandbox_group):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.namespace = SimpleNamespace(setting="")
    assert await sandbox_group._value_autocomplete(interaction, "") == []


def test_the_sandbox_embed_shows_every_group_and_labels_the_enums(cfg):
    values = {path: "1" for path in sandbox.SETTINGS}
    values["ZombieLore.Transmission"] = "2"
    embed = render.sandbox(cfg, values)

    assert [f.name for f in embed.fields] == list(sandbox.GROUPS)
    body = "".join(f.value for f in embed.fields)
    assert "2 (Saliva only)" in body
    assert len(embed) <= 6000


def test_the_sandbox_embed_says_when_a_setting_is_not_in_this_build(cfg):
    embed = render.sandbox(cfg, {})  # an empty file, or a build without these options
    assert "not in this build" in "".join(f.value for f in embed.fields)


def test_a_single_setting_embed_marks_the_live_option(cfg):
    embed = render.sandbox_setting(
        "ZombieLore.Transmission", sandbox.SETTINGS["ZombieLore.Transmission"], "2"
    )
    options = next(f for f in embed.fields if f.name == "Options").value
    assert "**▸** `2` Saliva only" in options
    assert "**▸** `1`" not in options


def test_a_world_gen_only_setting_says_so_before_someone_wastes_a_restart(cfg):
    embed = render.sandbox_setting("StartMonth", sandbox.SETTINGS["StartMonth"], "7")
    assert any("new world" in f.name for f in embed.fields)
