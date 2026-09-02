"""Fakes for the two things the bot cannot have in a test: AWS and a game server."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from botocore.exceptions import ClientError

from pzbot.aws import INSTANCE_GONE, Backup, CommandResult, Cost, Instance
from pzbot.config import Config, Runtime
from pzbot.rcon import RconUnreachable
from pzbot.server import GameServer


class FakeAws:
    """Records every call, so tests can assert on what the bot *did*, not just returned."""

    region = "us-east-1"

    def __init__(self, state: str = "stopped") -> None:
        self.state = state
        self.calls: list[str] = []
        # Each entry is (document_name, parameters) for one send_command call.
        self.commands: list[tuple[str, dict[str, str]]] = []
        self.command_result = CommandResult("Success", "done", "")
        self.sandbox_result = CommandResult(
            "Success", json.dumps({"setting": "?", "was": "1", "now": "2"}), ""
        )
        # pz-version.sh and pz-mod-tool.py both answer in JSON, and the bot insists on
        # it, so the fakes have to as well -- a bare "done" here would make every test
        # of those paths fail for the wrong reason.
        self.version_result = CommandResult(
            "Success",
            json.dumps(
                {
                    "installed_build": "18102025",
                    "installed_branch": "public",
                    "configured_branch": "",
                    "last_updated": "2026-08-30T09:00:00Z",
                    "hold": False,
                    "hold_reason": "",
                    "server_running": False,
                    "note": "",
                }
            ),
            "",
        )
        self.mods_result = CommandResult(
            "Success",
            json.dumps(
                {
                    "workshop_items": ["2169435993"],
                    "mods": ["Authentic_Z"],
                    "entries": [
                        {
                            "workshop_id": "2169435993",
                            "mods": ["Authentic_Z"],
                            "added": "2026-09-01T12:00:00Z",
                            "tracked": True,
                            "pending": False,
                        }
                    ],
                    "unattributed_mods": [],
                }
            ),
            "",
        )
        # Per-document overrides, keyed by the document-name suffix ("-backup", "-mods").
        # Each entry is a list consumed in order and the LAST one sticks, so a
        # one-element list is a plain override and a two-element list is "fail the first
        # call, succeed after" -- which is how the two-restart mod path gets tested.
        self.results: dict[str, list[CommandResult]] = {}
        self.backups: list[Backup] = []
        self.parameters: dict[str, str] = {}
        # What `find_instance` resolves the pz:role=gameserver tag to. "" means nothing
        # in the account carries the tag.
        self.instance_id = "i-0test"
        # Instance ids EC2 no longer knows about -- a game server that was rebuilt and
        # has since aged out of DescribeInstances.
        self.gone: set[str] = set()
        # Per-id state, for the window where a rebuilt instance still answers as
        # `terminated`. Falls back to `self.state`.
        self.states: dict[str, str] = {}
        # Set to an exception to simulate PutMetricData failing.
        self.heartbeat_fails: Exception | None = None

    async def describe(self, instance_id: str) -> Instance:
        self.calls.append(f"describe:{instance_id}")
        if instance_id in self.gone:
            raise ClientError(
                {"Error": {"Code": INSTANCE_GONE, "Message": "does not exist"}},
                "DescribeInstances",
            )
        return Instance(
            instance_id=instance_id,
            state=self.states.get(instance_id, self.state),
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

    async def send_command(
        self, instance_id, document, parameters, *, timeout=600, comment=""
    ) -> CommandResult:
        self.calls.append(f"cmd:{comment}")
        self.commands.append((document, dict(parameters)))
        for suffix, queued in self.results.items():
            if document.endswith(suffix):
                return queued.pop(0) if len(queued) > 1 else queued[0]
        if document.endswith("-sandbox"):
            return self.sandbox_result
        if document.endswith("-version"):
            return self.version_result
        if document.endswith("-mods"):
            return self.mods_result
        return self.command_result

    async def list_backups(self, bucket, stack, limit=200) -> list[Backup]:
        self.calls.append(f"list_backups:{bucket}")
        return self.backups[:limit]

    async def get_parameters_by_path(self, prefix: str) -> dict[str, str]:
        self.calls.append(f"params:{prefix}")
        return {k: v for k, v in self.parameters.items() if k.startswith(prefix)}

    async def month_to_date(self, stack: str, instance_type: str = "") -> Cost:
        self.calls.append("cost")
        return Cost(stack_usd=12.34, account_usd=56.78, game_hours=41.5)

    async def put_heartbeat(self, namespace: str, stack: str) -> None:
        self.calls.append("heartbeat")
        if self.heartbeat_fails is not None:
            raise self.heartbeat_fails


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
