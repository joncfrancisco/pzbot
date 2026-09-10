"""The state machine and the operations, with AWS and RCON faked out.

The tests that matter most here are the negative ones. A bot that starts a server
correctly but fails to clean up after a *failed* start leaves an m7i.xlarge billing at
$0.20/hour with nobody watching, and that is the failure this whole design exists to
prevent.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from botocore.exceptions import ClientError

from conftest import FakeRcon, backup, noop_progress
from pzbot.aws import CommandResult
from pzbot.rcon import RconAuthError, RconUnreachable
from pzbot.server import OperationError, Stage


def fast_forward(monkeypatch):
    """Make every `asyncio.sleep` return immediately, so a 120s grace period costs 0s."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))


# --- Probing ---------------------------------------------------------------------------


async def test_stopped_instance_is_stopped_and_costs_no_rcon_call(server, aws):
    aws.state = "stopped"
    snap = await server.probe()
    assert snap.stage is Stage.STOPPED
    assert server.rcon.commands == []


async def test_running_and_answering_is_ready(server, aws):
    aws.state = "running"
    server.rcon.players = ["Bob", "Alice"]
    snap = await server.probe()
    assert snap.stage is Stage.READY
    assert snap.players == ["Bob", "Alice"]
    assert snap.player_count == 2


async def test_running_but_silent_is_booting_not_ready(server, aws):
    # The single most important distinction in the bot: EC2 says running, PZ is still
    # loading the world, and nobody should be told to connect yet.
    aws.state = "running"
    server.rcon.fail = RconUnreachable("connection refused")
    snap = await server.probe()
    assert snap.stage is Stage.BOOTING
    assert snap.rcon_error


async def test_a_rejected_password_is_not_reported_as_still_loading(server, aws):
    # Reporting an auth failure as "loading" would hide a stale password behind a
    # ten-minute timeout, and then stop a perfectly healthy server.
    aws.state = "running"
    server.rcon.fail = RconAuthError("RCON authentication failed")
    snap = await server.probe()
    assert snap.stage is Stage.UNKNOWN
    assert "authentication" in snap.rcon_error


async def test_a_well_formed_but_wrong_response_is_not_ready(server, aws):
    aws.state = "running"

    class Odd(FakeRcon):
        async def execute(self, command, *, timeout=None):
            return "Unknown command"

    server.rcon = Odd()
    assert (await server.probe()).stage is Stage.BOOTING


async def test_probe_follows_a_moved_private_ip(server, aws):
    aws.state = "running"
    server.rcon.host = "10.20.1.9"  # stale, e.g. after the instance was rebuilt
    await server.probe()
    assert server.rcon.host == "10.20.1.171"


# --- A rebuilt game server ---------------------------------------------------------------
#
# The instance id is discovered by tag at startup and was then pinned for the life of the
# process, so a `terraform apply` that replaced the game server left every command aimed
# at the corpse of the old one -- with nothing to detect it, because `systemctl is-active`
# and the BotAlive heartbeat both stay green while the bot cannot see its own server.


async def test_a_healthy_instance_is_never_re_resolved(server, aws):
    # Re-resolving is the recovery path, not the normal one: an extra DescribeInstances
    # on every probe would double the cost of the loop that runs sixty times an hour.
    aws.state = "running"
    await server.probe()
    assert not [c for c in aws.calls if c.startswith("find:")]


async def test_a_vanished_instance_is_followed_to_its_replacement(server, aws, cfg):
    aws.gone.add("i-0test")  # EC2 no longer knows the id the bot booted with
    aws.instance_id = "i-0rebuilt"  # ...but the tag resolves to the new one
    aws.state = "stopped"

    snap = await server.probe()

    assert snap.instance.instance_id == "i-0rebuilt"
    assert cfg.game_instance_id == "i-0rebuilt", "the id must stick, not be re-derived"


async def test_a_terminated_instance_is_followed_before_it_ages_out(server, aws, cfg):
    # For about an hour after a rebuild the old instance still answers DescribeInstances,
    # as `terminated`. That renders as Stage.UNKNOWN -- indistinguishable, to whoever
    # typed `/pz status`, from a bot that is simply broken.
    aws.states["i-0test"] = "terminated"
    aws.instance_id = "i-0rebuilt"
    aws.state = "running"

    snap = await server.probe()

    assert cfg.game_instance_id == "i-0rebuilt"
    assert snap.stage is Stage.READY


async def test_a_vanished_instance_with_no_replacement_says_so(server, aws):
    # Mid-apply, or a stack that was destroyed. The failure has to name itself: this used
    # to surface as a bare botocore AccessDenied-shaped embed with no hint that the id
    # the bot was holding had simply stopped existing.
    aws.gone.add("i-0test")
    aws.instance_id = ""

    with pytest.raises(OperationError, match="no longer exists"):
        await server.probe()


async def test_a_terminated_instance_with_no_replacement_is_reported_not_hidden(server, aws):
    # Nothing has taken its place yet, so there is nothing to follow -- report the state
    # EC2 gave rather than inventing an error.
    aws.states["i-0test"] = "terminated"
    aws.instance_id = ""

    assert (await server.probe()).stage is Stage.UNKNOWN


async def test_an_ordinary_aws_failure_is_not_treated_as_a_rebuild(server, aws):
    # Only InvalidInstanceID.NotFound means "re-discover me". A throttle or an
    # AccessDenied must propagate to the one error handler, not silently repoint the bot.
    boom = ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}}, "D")

    async def describe(instance_id):
        raise boom

    aws.describe = describe
    with pytest.raises(ClientError):
        await server.probe()
    assert not [c for c in aws.calls if c.startswith("find:")]


# --- Start -----------------------------------------------------------------------------


async def test_start_from_stopped_reaches_ready(server, aws):
    aws.state = "stopped"
    snap = await server.start(noop_progress)
    assert snap.stage is Stage.READY
    assert f"start:{server.cfg.game_instance_id}" in aws.calls


async def test_start_when_already_ready_does_not_start_again(server, aws):
    aws.state = "running"
    await server.start(noop_progress)
    assert not [c for c in aws.calls if c.startswith("start:")]


async def test_start_during_a_shutdown_is_refused(server, aws):
    # Starting on top of a stop races the save that is in progress.
    aws.state = "stopping"
    with pytest.raises(OperationError, match="shutting down"):
        await server.start(noop_progress)
    assert not [c for c in aws.calls if c.startswith("start:")]


async def test_a_world_that_never_loads_stops_the_instance(server, aws):
    aws.state = "stopped"
    server.cfg.start_ready_timeout = -1  # the world is already "late" on the first poll
    server.rcon.fail = RconUnreachable("connection refused")

    with pytest.raises(OperationError, match="never answered RCON"):
        await server.start(noop_progress)
    assert f"stop:{server.cfg.game_instance_id}" in aws.calls


async def test_an_instance_that_never_boots_stops_itself(server, aws):
    class NeverRuns(type(aws)):
        async def start_instance(self, instance_id):
            self.calls.append(f"start:{instance_id}")
            return "pending"  # and the state stays "stopped" forever

    stuck = NeverRuns("stopped")
    server.aws = stuck
    server.cfg.start_running_timeout = -1

    with pytest.raises(OperationError, match="still `stopped`"):
        await server.start(noop_progress)
    assert f"stop:{server.cfg.game_instance_id}" in stuck.calls


async def test_a_rejected_password_during_start_stops_the_instance(server, aws):
    aws.state = "stopped"
    server.rcon.fail = RconAuthError("RCON authentication failed")
    with pytest.raises(OperationError, match="refused the bot's password"):
        await server.start(noop_progress)
    assert f"stop:{server.cfg.game_instance_id}" in aws.calls


# --- Stop ------------------------------------------------------------------------------


async def test_stop_backs_up_before_stopping(server, aws):
    aws.state = "running"
    await server.stop(noop_progress)
    assert aws.commands == [("pz-prod-backup", {"mode": "prestop", "label": ""})]
    assert aws.calls.index("cmd:pzbot /pz stop") < aws.calls.index("stop:i-0test")


async def test_stop_still_stops_when_the_backup_fails(server, aws):
    # ExecStop saves the world on the way down regardless (pzserver DESIGN G5). Refusing
    # to stop here would leave an expensive instance up because of a broken script.
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.command_result = CommandResult("Failed", "", "tar: no space left on device")
    await server.stop(noop_progress)
    assert f"stop:{server.cfg.game_instance_id}" in aws.calls


async def test_stop_warns_players_before_shutting_down(server, aws, monkeypatch):
    fast_forward(monkeypatch)
    aws.state = "running"
    server.cfg.stop_grace_seconds = 120
    server.rcon.players = ["Bob"]

    await server.stop(noop_progress)
    warnings = [c for c in server.rcon.commands if c.startswith("servermsg")]
    assert len(warnings) == 2  # one at the start of the grace period, one near the end


async def test_force_stop_skips_the_warning_but_not_the_backup(server, aws, monkeypatch):
    fast_forward(monkeypatch)
    aws.state = "running"
    server.cfg.stop_grace_seconds = 120
    server.rcon.players = ["Bob"]

    await server.stop(noop_progress, force=True)
    assert not [c for c in server.rcon.commands if c.startswith("servermsg")]
    assert aws.commands == [("pz-prod-backup", {"mode": "prestop", "label": ""})]


async def test_stopping_an_already_stopped_server_is_refused(server, aws):
    aws.state = "stopped"
    with pytest.raises(OperationError, match="already stopped"):
        await server.stop(noop_progress)


# --- Restore ---------------------------------------------------------------------------


GOOD = "2026-08-22T19-24-30Z__scheduled.tar.zst"


async def test_restore_refuses_a_name_that_is_not_a_backup_name(server, aws):
    # The value reaching this function comes from a Discord text box. It must never be
    # able to become a second shell command.
    aws.state = "running"
    for hostile in (
        "; rm -rf /opt/pz/data",
        "$(curl evil.example)",
        "../../etc/passwd",
        f"{GOOD} && reboot",
    ):
        with pytest.raises(OperationError, match="not a backup name"):
            await server.restore(hostile, noop_progress)
    assert aws.commands == []


async def test_restore_refuses_a_name_that_is_not_in_the_bucket(server, aws):
    aws.state = "running"
    aws.backups = [backup("2026-08-01T00-00-00Z__manual.tar.zst")]
    with pytest.raises(OperationError, match="No backup named"):
        await server.restore(GOOD, noop_progress)
    assert aws.commands == []


async def test_restore_refuses_while_players_are_online(server, aws):
    aws.state = "running"
    aws.backups = [backup(GOOD)]
    server.rcon.players = ["Bob"]
    with pytest.raises(OperationError, match="player"):
        await server.restore(GOOD, noop_progress)
    assert aws.commands == []


async def test_restore_refuses_while_the_instance_is_stopped(server, aws):
    aws.state = "stopped"
    aws.backups = [backup(GOOD)]
    with pytest.raises(OperationError, match="must be running"):
        await server.restore(GOOD, noop_progress)


async def test_restore_stops_the_game_restores_and_starts_it_again(server, aws):
    aws.state = "running"
    aws.backups = [backup(GOOD)]
    await server.restore(GOOD, noop_progress)
    assert aws.commands == [
        ("pz-prod-lifecycle", {"action": "stop"}),
        ("pz-prod-restore", {"backupName": GOOD}),
        ("pz-prod-lifecycle", {"action": "start"}),
    ]


# --- Backups ---------------------------------------------------------------------------


async def test_backup_label_must_be_boring(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="letters, numbers"):
        await server.backup_now("no; rm -rf /")
    assert aws.commands == []


async def test_backup_label_is_passed_through_when_sane(server, aws):
    aws.state = "running"
    await server.backup_now("before-b42")
    assert aws.commands == [("pz-prod-backup", {"mode": "manual", "label": "before-b42"})]


# --- Download links --------------------------------------------------------------------


async def test_download_defaults_to_the_newest_backup(server, aws):
    aws.backups = [
        backup(GOOD, minutes_ago=5),
        backup("2026-08-01T00-00-00Z__manual.tar.zst", minutes_ago=900),
    ]
    item, url, ttl = await server.download_url()
    assert item.name == GOOD
    assert ttl == 15 * 60
    assert item.key in url


async def test_download_signs_the_backup_that_was_asked_for(server, aws):
    older = "2026-08-01T00-00-00Z__manual.tar.zst"
    aws.backups = [backup(GOOD, minutes_ago=5), backup(older, minutes_ago=900)]
    item, url, _ = await server.download_url(older)
    assert item.name == older
    assert older in url


async def test_download_refuses_a_name_that_is_not_a_backup_name(server, aws):
    # Same rule as restore: this value comes out of a Discord text box, and a key is a
    # path. `../` here would sign a URL for something outside backups/<stack>/, which is
    # the one place the bot's own IAM policy lets it read.
    aws.backups = [backup(GOOD)]
    for hostile in ("../../ops/secrets.env", "$(curl evil.example)", f"{GOOD} && reboot"):
        with pytest.raises(OperationError, match="not a backup name"):
            await server.download_url(hostile)
    assert not any(c.startswith("presign:") for c in aws.calls)


async def test_download_refuses_a_name_that_is_not_in_the_bucket(server, aws):
    aws.backups = [backup("2026-08-01T00-00-00Z__manual.tar.zst")]
    with pytest.raises(OperationError, match="No backup named"):
        await server.download_url(GOOD)
    assert not any(c.startswith("presign:") for c in aws.calls)


async def test_download_says_so_when_the_bucket_is_empty(server, aws):
    aws.backups = []
    with pytest.raises(OperationError, match="nothing in"):
        await server.download_url()


async def test_download_never_touches_the_game_server(server, aws):
    # The whole point: the instance is stopped by default, and the archive is in S3
    # either way. Starting an m7i.xlarge to hand someone a copy of the world would be
    # absurd -- and the moment you most want a copy is when the box is broken.
    aws.state = "stopped"
    aws.backups = [backup(GOOD)]
    await server.download_url()
    assert aws.commands == []
    assert not any(c.startswith(("start:", "describe:")) for c in aws.calls)


@pytest.mark.parametrize(("asked", "signed"), [(1, 5), (15, 15), (999, 60)])
async def test_the_link_lifetime_is_clamped(server, aws, asked, signed):
    # `app_commands.Range` bounds what Discord will send, but nothing stops another
    # caller -- or a later command -- passing whatever it likes. The ceiling is a
    # security property of a credential that cannot be revoked once it is handed out.
    aws.backups = [backup(GOOD)]
    _, _, ttl = await server.download_url(minutes=asked)
    assert ttl == signed * 60


# --- Config allowlist ------------------------------------------------------------------


async def test_config_rejects_a_key_that_is_not_on_the_allowlist(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="not an editable key"):
        await server.ini_write("RCONPassword", "hunter2")
    assert aws.commands == []


async def test_config_validates_values(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="between 1 and 64"):
        await server.ini_write("MaxPlayers", "9000")
    with pytest.raises(OperationError, match="not true or false"):
        await server.ini_write("PVP", "maybe")
    with pytest.raises(OperationError, match="control characters"):
        await server.ini_write("PublicName", "a=b")


async def test_config_writes_and_reloads(server, aws):
    aws.state = "running"
    message = await server.ini_write("PVP", "yes")
    assert "reloadoptions" in server.rcon.commands
    assert "`true`" in message


# --- Idle ------------------------------------------------------------------------------


async def test_idle_timeout_bounds_are_enforced(server, aws):
    for bad in (0, 4, 1441):
        with pytest.raises(OperationError, match="between 5 and 1440"):
            await server.set_idle(bad, bad - 5)
    assert aws.commands == []


async def test_idle_never_writes_a_zero_timeout(server, aws):
    # PZ's watchdog compares `idle >= IDLE_TIMEOUT`, so a zero would shut the server
    # down on the first tick, sixty seconds after someone asked for "no idle timeout".
    await server.set_idle(30, -5)
    assert aws.commands == [("pz-prod-idle-retune", {"timeoutMin": "30", "warnMin": "1"})]


def test_the_backup_name_pattern_matches_what_the_live_stack_actually_produces():
    # Copied verbatim from `aws s3 ls` against pz-prod-backups-020949219706. The pattern
    # is a security control on the restore path, so it has to match reality exactly --
    # too strict and `/pz restore` refuses every real backup.
    from pzbot.server import BACKUP_NAME

    for name in (
        "2026-08-23T00-22-27Z__prestop__idle-for-30m.tar.zst",
        "2026-08-23T00-00-36Z__scheduled.tar.zst",
        "2026-08-22T23-47-22Z__manual__first-verification.tar.zst",
    ):
        assert BACKUP_NAME.fullmatch(name), name


# --- Sandbox options ---------------------------------------------------------------------


async def test_setting_a_sandbox_option_stops_the_game_edits_then_starts_it(server, aws):
    # Order matters and is the whole reason this is not "edit, then restart": a running
    # server owns SandboxVars.lua and rewrites it wholesale when an admin changes an
    # option in game, which would silently undo an edit made underneath it.
    aws.state = "running"
    changed = await server.sandbox_set("ZombieLore.Transmission", "Saliva only", noop_progress)

    assert [c for c in aws.calls if c.startswith("cmd:")] == [
        "cmd:pzbot /pz sandbox set (stop)",
        "cmd:pzbot /pz sandbox set",
        "cmd:pzbot /pz sandbox set (start)",
    ]
    assert aws.commands[0] == ("pz-prod-lifecycle", {"action": "stop"})
    assert aws.commands[2] == ("pz-prod-lifecycle", {"action": "start"})
    assert changed["applied"] == "yes"


async def test_the_label_is_translated_to_the_number_the_game_reads(server, aws):
    aws.state = "running"
    await server.sandbox_set("ZombieLore.Transmission", "Saliva only", noop_progress)
    # [0] is the stop, [1] is the sandbox document call itself.
    assert aws.commands[1] == (
        "pz-prod-sandbox",
        {
            "action": "set",
            "path": "/opt/pz/data/Zomboid/Server/pzprod_SandboxVars.lua",
            "key": "ZombieLore.Transmission",
            "value": "2",
        },
    )


async def test_apply_off_leaves_the_server_alone(server, aws):
    # The batching workflow: change several settings, restart once.
    aws.state = "running"
    changed = await server.sandbox_set("DayLength", "2 hours", noop_progress, apply=False)
    assert [c for c in aws.calls if c.startswith("cmd:")] == ["cmd:pzbot /pz sandbox set"]
    assert changed["applied"] == "next start"


async def test_a_failed_edit_puts_the_server_back_up(server, aws):
    # Otherwise the instance bills at $0.20/hour with no game on it, which is the worst
    # of both worlds.
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.sandbox_result = CommandResult(
        "Failed", "", "sandbox: DayLength is not in this world's file"
    )
    with pytest.raises(OperationError, match="Could not change the sandbox options"):
        await server.sandbox_set("DayLength", "2 hours", noop_progress)
    assert aws.commands[-1] == ("pz-prod-lifecycle", {"action": "start"})


async def test_a_bad_value_never_touches_the_server(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="Blood"):
        await server.sandbox_set("ZombieLore.Transmission", "by sneezing", noop_progress)
    with pytest.raises(OperationError, match="between 0 and 4"):
        await server.sandbox_set("ZombieConfig.PopulationMultiplier", "99", noop_progress)
    with pytest.raises(OperationError, match="not a setting"):
        await server.sandbox_set("ZombieLore.RCONPassword", "1", noop_progress)
    assert aws.commands == []


async def test_sandbox_needs_the_instance_running(server, aws):
    aws.state = "stopped"
    with pytest.raises(OperationError, match="instance is stopped"):
        await server.sandbox_set("DayLength", "2 hours", noop_progress)
    assert aws.commands == []


async def test_sandbox_read_parses_the_json_the_box_returns(server, aws):
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.sandbox_result = CommandResult("Success", '{"DayLength": "3"}', "")
    assert await server.sandbox_read() == {"DayLength": "3"}


@pytest.mark.parametrize(
    "operation",
    [
        lambda s: s.sandbox_read(),
        lambda s: s.ini_read(),
        lambda s: s.sandbox_set("DayLength", "2 hours", noop_progress),
        lambda s: s.ini_write("PVP", "true"),
    ],
)
async def test_reaching_the_box_while_it_is_off_explains_itself(server, aws, operation):
    # SSM's own answer to this is "InvalidInstanceId", which tells a player nothing.
    aws.state = "stopped"
    with pytest.raises(OperationError, match="instance is stopped"):
        await operation(server)
    assert aws.commands == []


# --- The game build ------------------------------------------------------------------------


def cmds(aws) -> list[str]:
    return [c.removeprefix("cmd:pzbot ") for c in aws.calls if c.startswith("cmd:")]


async def test_version_status_reports_what_steam_says_not_what_was_asked_for(server, aws):
    # `configured_branch` is the pin; `installed_branch` is the manifest. They can differ,
    # and the difference is the whole point of showing both.
    aws.state = "running"
    aws.version_result = CommandResult(
        "Success",
        json.dumps({"installed_build": "18102025", "installed_branch": "unstable", "hold": True}),
        "",
    )
    status = await server.version_status()
    assert status["installed_branch"] == "unstable"
    assert status["hold"] is True
    assert aws.commands == [("pz-prod-version", {"action": "status", "branch": ""})]


async def test_updating_backs_up_stops_updates_then_starts(server, aws):
    aws.state = "running"
    status = await server.version_update(noop_progress)

    assert cmds(aws) == [
        "before-update",
        "/pz version update (stop)",
        "/pz version update",
        "/pz version update (start)",
    ]
    assert aws.commands[0] == ("pz-prod-backup", {"mode": "manual", "label": "before-update"})
    assert aws.commands[2] == ("pz-prod-version", {"action": "update", "branch": ""})
    assert status["applied"] == "yes"
    assert status["backup_label"] == "before-update"


async def test_a_failed_backup_stops_the_whole_update(server, aws):
    # Unlike /pz stop, which stops anyway: here the backup is the only way back from a
    # build that will not load the save, so no backup means no update.
    aws.state = "running"
    aws.results["-backup"] = [CommandResult("Failed", "", "no space left on device")]

    with pytest.raises(OperationError, match="pre-change backup failed"):
        await server.version_update(noop_progress)
    # Nothing was stopped, so nobody was kicked off for an update that never happened.
    assert [c for c in aws.commands if c[0] == "pz-prod-lifecycle"] == []


async def test_a_failed_update_puts_the_server_back_up(server, aws):
    aws.state = "running"
    aws.results["-version"] = [CommandResult("Failed", "", "steamcmd: no subscription")]

    with pytest.raises(OperationError, match="failed on the game server"):
        await server.version_update(noop_progress)
    assert aws.commands[-1] == ("pz-prod-lifecycle", {"action": "start"})


async def test_switching_branch_pins_it_before_downloading_it(server, aws):
    # Order is load-bearing: pz-update.sh reads the pin out of version.conf, so a pin
    # written after the update would take effect a session late.
    aws.state = "running"
    await server.version_update(noop_progress, branch="b41multiplayer")

    version_calls = [c for c in aws.commands if c[0] == "pz-prod-version"]
    assert version_calls == [
        ("pz-prod-version", {"action": "branch", "branch": "b41multiplayer"}),
        ("pz-prod-version", {"action": "update", "branch": ""}),
    ]
    assert aws.commands[0] == (
        "pz-prod-backup",
        {"mode": "manual", "label": "before-branch-switch"},
    )


async def test_a_branch_name_that_is_not_one_never_reaches_the_box(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="not a Steam branch name"):
        await server.version_update(noop_progress, branch="public; rm -rf /")
    assert aws.commands == []


async def test_validating_asks_for_a_validate_and_says_so_in_the_backup_label(server, aws):
    aws.state = "running"
    status = await server.version_update(noop_progress, validate=True)
    assert ("pz-prod-version", {"action": "validate", "branch": ""}) in aws.commands
    assert status["backup_label"] == "before-validate"


async def test_holding_does_not_need_the_game_stopped(server, aws):
    # It writes a flag pz-update.sh reads at the START of the next session, so it changes
    # nothing about the session it is set in and has no business interrupting one.
    aws.state = "running"
    await server.version_hold(True)
    assert aws.commands == [("pz-prod-version", {"action": "hold", "branch": ""})]

    aws.commands.clear()
    await server.version_hold(False)
    assert aws.commands == [("pz-prod-version", {"action": "unhold", "branch": ""})]


# --- Mods ------------------------------------------------------------------------------------


async def test_mods_list_parses_the_inventory(server, aws):
    aws.state = "running"
    inventory = await server.mods_list()
    assert inventory["workshop_items"] == ["2169435993"]
    assert inventory["entries"][0]["mods"] == ["Authentic_Z"]


@pytest.mark.parametrize(
    "workshop_id",
    [
        "https://steamcommunity.com/sharedfiles/filedetails/?id=2169435993",  # the URL
        "2169435993; systemctl stop pzserver",
        "",
        "12345678901234",  # longer than any Workshop id
    ],
)
async def test_a_workshop_id_that_is_not_one_never_reaches_the_box(server, aws, workshop_id):
    aws.state = "running"
    with pytest.raises(OperationError, match="Workshop id"):
        await server.mods_add(workshop_id, "", noop_progress)
    assert aws.commands == []


async def test_a_mod_id_that_is_not_one_never_reaches_the_box(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="not a mod id"):
        await server.mods_add("2169435993", "Authentic_Z,../../etc/passwd", noop_progress)
    assert aws.commands == []


async def test_adding_a_mod_backs_up_stops_edits_and_restarts(server, aws):
    aws.state = "running"
    report = await server.mods_add("2169435993", "Authentic_Z, AuthenticZ_Clothing", noop_progress)

    assert cmds(aws) == [
        "before-mods",
        "/pz mods add (stop)",
        "/pz mods add",
        "/pz mods add (start)",
    ]
    # Whitespace around the comma is what a person types; the box must not see it.
    assert aws.commands[2] == (
        "pz-prod-mods",
        {
            "action": "add",
            "workshopId": "2169435993",
            "modIds": "Authentic_Z,AuthenticZ_Clothing",
        },
    )
    assert report["applied"] == "yes"


async def test_an_item_whose_mods_are_unknown_is_scanned_and_restarted_again(server, aws):
    # The confusing state this exists to prevent: the item is in WorkshopItems=, the
    # server downloaded it on the restart, and Mods= is still empty -- so it is installed
    # and loading nothing. Finding the ids needs the download; loading them needs another
    # restart, because PZ read Mods= before they were written.
    aws.state = "running"
    aws.results["-mods"] = [
        CommandResult("Success", json.dumps({"pending": True, "entries": []}), ""),
        CommandResult(
            "Success",
            json.dumps({"resolved": [{"workshop_id": "2169435993", "mods": ["Zed"]}]}),
            "",
        ),
    ]

    report = await server.mods_add("2169435993", "", noop_progress)

    assert cmds(aws) == [
        "before-mods",
        "/pz mods add (stop)",
        "/pz mods add",
        "/pz mods add (start)",
        "/pz mods scan",
        "/pz mods add (stop for scan)",
        "/pz mods add (start for scan)",
    ]
    assert report["applied"] == "yes, after two restarts"


async def test_a_scan_that_finds_nothing_does_not_restart_a_second_time(server, aws):
    aws.state = "running"
    aws.results["-mods"] = [
        CommandResult("Success", json.dumps({"pending": True}), ""),
        CommandResult("Success", json.dumps({"resolved": [], "still_pending": ["216"]}), ""),
    ]
    report = await server.mods_add("2169435993", "", noop_progress)
    assert cmds(aws).count("/pz mods add (start)") == 1
    assert "stop for scan" not in " ".join(cmds(aws))
    assert report["applied"] == "yes"


async def test_apply_off_edits_the_mod_list_without_touching_the_session(server, aws):
    aws.state = "running"
    report = await server.mods_remove("2169435993", noop_progress, apply=False)
    assert cmds(aws) == ["before-mods", "/pz mods remove"]
    assert report["applied"] == "next start"


async def test_a_failed_mod_edit_puts_the_server_back_up(server, aws):
    aws.state = "running"
    aws.results["-mods"] = [CommandResult("Failed", "", "mods: already in the list")]
    with pytest.raises(OperationError, match="failed on the game server"):
        await server.mods_add("2169435993", "Zed", noop_progress)
    assert aws.commands[-1] == ("pz-prod-lifecycle", {"action": "start"})


async def test_a_failed_backup_stops_a_mod_change(server, aws):
    aws.state = "running"
    aws.results["-backup"] = [CommandResult("Failed", "", "no space left on device")]
    with pytest.raises(OperationError, match="pre-change backup failed"):
        await server.mods_add("2169435993", "Zed", noop_progress)
    assert [c for c in aws.commands if c[0] == "pz-prod-mods"] == []


async def test_prose_where_json_was_expected_is_an_error_not_an_empty_mod_list(server, aws):
    # Reporting "no mods" for an .ini that could not be read would have somebody adding a
    # mod that is already there, or removing one that is not.
    aws.state = "running"
    aws.results["-mods"] = [CommandResult("Success", "mods: 3 items", "")]
    with pytest.raises(OperationError, match="Unreadable `mods` response"):
        await server.mods_list()


async def test_checking_for_mod_updates_needs_a_server_that_is_actually_up(server, aws):
    aws.state = "running"
    server.rcon.fail = RconUnreachable("connection refused")
    with pytest.raises(OperationError, match="only a running server"):
        await server.mods_check()


async def test_checking_for_mod_updates_asks_the_game(server, aws):
    aws.state = "running"
    assert "checkModsNeedUpdate" in await server.mods_check()
    assert "checkModsNeedUpdate" in server.rcon.commands
