"""Configuration assembly: what is required, what is discovered, what is refused."""

from __future__ import annotations

import pytest

from pzbot import config as config_mod
from pzbot.config import ConfigError, _ids

PARAMS = {
    "/pz/prod/config/server_name": "pzprod",
    "/pz/prod/config/backup_bucket": "pz-prod-backups-020949219706",
    "/pz/prod/config/alert_topic_arn": "arn:aws:sns:us-east-1:020949219706:pz-prod-alerts",
    "/pz/prod/config/idle_warn_min": "25",
    "/pz/prod/config/idle_timeout_min": "30",
    "/pz/prod/config/session_cap_hours": "12",
    "/pz/prod/config/xmx": "11g",
    "/pz/prod/rcon_password": "hunter2",
    "/pz/prod/discord/token": "a.b.c",
    "/pz/prod/discord/guild_id": "111",
    "/pz/prod/discord/role_admin": "222",
    "/pz/prod/discord/role_player": "333",
    "/pz/prod/discord/channel_main": "444",
    "/pz/prod/discord/channel_audit": "555",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(dict(**{k: v for k, v in __import__("os").environ.items()})):
        if key.startswith("PZBOT_"):
            monkeypatch.delenv(key, raising=False)
    # Never let a stray /etc/pzbot/bot_contract.json on a developer's machine leak in.
    monkeypatch.setenv("PZBOT_CONTRACT", "/nonexistent/bot_contract.json")
    monkeypatch.setenv("PZBOT_CONNECT_HOST", "pz.joncfrancis.co")


def test_ids_parses_one_or_many():
    assert _ids("444") == (444,)
    assert _ids("444, 555") == (444, 555)
    assert _ids("") == ()
    assert _ids(None) == ()
    with pytest.raises(ConfigError):
        _ids("not-a-snowflake")


async def test_load_assembles_a_usable_config(aws):
    aws.parameters = dict(PARAMS)
    cfg = await config_mod.load(aws)

    assert cfg.stack == "prod"
    assert cfg.game_instance_id == "i-0test"  # discovered by tag, not configured
    assert cfg.rcon_host == "10.20.1.171"
    assert cfg.rcon_port == 27015
    assert cfg.connect_string == "pz.joncfrancis.co:16261"
    assert cfg.guild_id == 111
    assert cfg.channels_allowed == (444, 555)  # the audit channel is implicitly allowed
    assert cfg.runtime.idle_timeout_min == 30


async def test_a_missing_discord_parameter_names_itself_and_the_fix(aws):
    aws.parameters = {k: v for k, v in PARAMS.items() if k != "/pz/prod/discord/channel_audit"}
    with pytest.raises(ConfigError) as caught:
        await config_mod.load(aws)
    assert "/pz/prod/discord/channel_audit" in str(caught.value)
    assert "aws ssm put-parameter" in str(caught.value)


async def test_a_missing_rcon_password_refuses_to_start(aws):
    # Starting without it would mean every readiness probe fails and the bot reports a
    # healthy server as permanently unreachable -- worse than not starting.
    aws.parameters = {k: v for k, v in PARAMS.items() if k != "/pz/prod/rcon_password"}
    with pytest.raises(ConfigError, match="rcon_password"):
        await config_mod.load(aws)


async def test_the_tag_wins_over_a_stale_pinned_instance_id(aws, monkeypatch, caplog):
    # The IAM policy is scoped by tag, so following anything else would point the bot at
    # an instance its own credentials cannot touch.
    aws.parameters = dict(PARAMS)
    monkeypatch.setenv("PZBOT_GAME_INSTANCE_ID", "i-0stale")
    cfg = await config_mod.load(aws)
    assert cfg.game_instance_id == "i-0test"


async def test_no_tagged_instance_is_a_clear_failure(aws):
    aws.parameters = dict(PARAMS)
    aws.instance_id = ""
    with pytest.raises(ConfigError, match="pz:role=gameserver"):
        await config_mod.load(aws)


async def test_refresh_runtime_picks_up_a_changed_knob(aws, cfg):
    aws.parameters = {
        "/pz/prod/config/idle_timeout_min": "45",
        "/pz/prod/config/idle_warn_min": "40",
    }
    runtime = await config_mod.refresh_runtime(aws, cfg)
    assert runtime.idle_timeout_min == 45
    assert runtime.idle_warn_min == 40


async def test_refresh_runtime_survives_parameter_store_being_down(cfg):
    class Broken:
        async def get_parameters_by_path(self, prefix):
            raise RuntimeError("ssm is having a day")

    # A stale idle number in an embed is cosmetic. Failing /pz status over it is not.
    runtime = await config_mod.refresh_runtime(Broken(), cfg)
    assert runtime.idle_timeout_min == cfg.runtime.idle_timeout_min
