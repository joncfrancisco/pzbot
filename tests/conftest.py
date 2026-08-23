"""Fakes for the two things the bot cannot have in a test: AWS and a game server."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from pzbot.aws import Backup, CommandResult, Cost, Instance
from pzbot.config import Config, Runtime
from pzbot.rcon import RconUnreachable
from pzbot.server import GameServer


class FakeAws:
    """Records every call, so tests can assert on what the bot *did*, not just returned."""

    region = "us-east-1"

    def __init__(self, state: str = "stopped") -> None:
        self.state = state
        self.calls: list[str] = []
        self.shell: list[list[str]] = []
        self.python: list[str] = []
        self.shell_result = CommandResult("Success", "done", "")
        self.python_result = CommandResult(
            "Success", json.dumps({"setting": "?", "was": "1", "now": "2"}), ""
        )
        self.backups: list[Backup] = []
        self.parameters: dict[str, str] = {}
        self.instance_id = "i-0test"

    async def describe(self, instance_id: str) -> Instance:
        self.calls.append(f"describe:{instance_id}")
        return Instance(
            instance_id=instance_id,
            state=self.state,
            private_ip="10.20.1.171",
            public_ip="34.233.59.251",
            launch_time=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30),
            instance_type="m7i.xlarge",
        )

    async def private_ip(self, instance_id: str) -> str:
        return "10.20.1.171"

    async def find_instance(self, *, stack: str, role: str) -> str:
        self.calls.append(f"find:{stack}/{role}")
        return self.instance_id

    async def start_instance(self, instance_id: str) -> str:
        self.calls.append(f"start:{instance_id}")
        self.state = "running"
        return "pending"

    async def stop_instance(self, instance_id: str) -> str:
        self.calls.append(f"stop:{instance_id}")
        self.state = "stopped"
        return "stopping"

    async def run_shell(self, instance_id, commands, *, timeout=600, comment="") -> CommandResult:
        self.calls.append(f"shell:{comment}")
        self.shell.append(commands)
        # `run_python` ships a module as base64 and runs it. Those calls answer with JSON
        # on stdout, so they get their own canned result -- the argv is still visible at
        # the end of the command string for tests to assert on.
        if "base64 -d" in commands[0]:
            self.python.append(commands[0])
            return self.python_result
        return self.shell_result

    async def list_backups(self, bucket, stack, limit=200) -> list[Backup]:
        self.calls.append(f"list_backups:{bucket}")
        return self.backups[:limit]

    async def get_parameters_by_path(self, prefix: str) -> dict[str, str]:
        self.calls.append(f"params:{prefix}")
        return {k: v for k, v in self.parameters.items() if k.startswith(prefix)}

    async def month_to_date(self, stack: str, instance_type: str = "") -> Cost:
        self.calls.append("cost")
        return Cost(stack_usd=12.34, account_usd=56.78, game_hours=41.5)


class FakeRcon:
    """Answers `players`; anything else echoes. Set `.fail` to simulate a dead server."""

    def __init__(self, players: list[str] | None = None) -> None:
        self.host = "10.20.1.171"
        self.port = 27015
        self.players = players or []
        self.fail: Exception | None = None
        self.commands: list[str] = []

    async def execute(self, command: str, *, timeout: float | None = None) -> str:
        self.commands.append(command)
        if self.fail is not None:
            raise self.fail
        if command == "players":
            names = "".join(f"\n-{p}" for p in self.players)
            return f"Players connected ({len(self.players)}):{names}"
        return f"ok: {command}"


@pytest.fixture
def cfg() -> Config:
    return Config(
        region="us-east-1",
        stack="prod",
        ssm_prefix="/pz/prod",
        game_instance_id="i-0test",
        rcon_host="10.20.1.171",
        rcon_port=27015,
        rcon_password="hunter2",
        connect_host="pz.joncfrancis.co",
        connect_port=16261,
        server_name="pzprod",
        backup_bucket="pz-prod-backups-000000000000",
        alert_topic_arn="arn:aws:sns:us-east-1:000000000000:pz-prod-alerts",
        metric_namespace="PZ",
        token="not-a-real-token",
        guild_id=111,
        role_admin=222,
        role_player=333,
        channels_allowed=(444, 555),
        channel_audit=555,
        runtime=Runtime(),
        # Tests must not spend real seconds waiting for a poll loop.
        poll_seconds=0,
        stop_grace_seconds=0,
    )


@pytest.fixture
def aws() -> FakeAws:
    return FakeAws()


@pytest.fixture
def server(aws: FakeAws, cfg: Config) -> GameServer:
    game = GameServer(aws, cfg)
    game.rcon = FakeRcon()
    return game


@pytest.fixture
def unreachable() -> RconUnreachable:
    return RconUnreachable("cannot reach 10.20.1.171:27015")


def backup(name: str, size: int = 1024, minutes_ago: int = 5) -> Backup:
    return Backup(
        key=f"backups/prod/{name}",
        size=size,
        modified=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
    )


async def noop_progress(_: str) -> None:
    return None
