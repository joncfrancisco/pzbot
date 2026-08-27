"""The AWS surface, and nothing more than the AWS surface.

boto3 is synchronous and the gateway client is not, so every call here is pushed to a
worker thread. That matters more than it looks: a `DescribeInstances` that blocks the
event loop for 400 ms blocks Discord's heartbeat too, and a bot that misses heartbeats
gets disconnected mid-start.

The methods map one-to-one onto the statements in `pz-bot-role` (pzserver DESIGN section
9). If a method here needs a permission that policy does not grant, the policy is the
thing to change -- not this file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

AwsError = (ClientError, BotoCoreError)

# What EC2 answers when an instance id no longer names an instance -- the state a
# rebuilt game server leaves the bot's pinned id in once the terminated one ages out
# of DescribeInstances. Named because `server.probe` has to tell it apart from every
# other AWS failure: this one is recoverable by re-resolving the tag, and nothing else is.
INSTANCE_GONE = "InvalidInstanceID.NotFound"


def is_instance_gone(exc: BaseException) -> bool:
    """True for the one AWS error that means "re-discover me", not "AWS is unhappy"."""
    return (
        isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == INSTANCE_GONE
    )


# Short timeouts with retries, rather than long ones: a wedged API call must surface as
# an error inside an interaction's lifetime, not hang a progress embed for a minute.
_BOTO = BotoConfig(
    retries={"max_attempts": 5, "mode": "standard"},
    connect_timeout=5,
    read_timeout=20,
    user_agent_extra="pzbot/1.0",
)


@dataclass(frozen=True)
class Instance:
    instance_id: str
    state: str  # pending | running | stopping | stopped | shutting-down | terminated
    private_ip: str
    public_ip: str
    launch_time: dt.datetime | None
    instance_type: str

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_stopped(self) -> bool:
        return self.state == "stopped"

    @property
    def in_transition(self) -> bool:
        return self.state in ("pending", "stopping", "shutting-down")


@dataclass(frozen=True)
class Backup:
    key: str
    size: int
    modified: dt.datetime

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def stamp(self) -> str:
        return self.name.split("__", 1)[0]

    @property
    def trigger(self) -> str:
        parts = self.name.removesuffix(".tar.zst").split("__")
        return parts[1] if len(parts) > 1 else "?"

    @property
    def label(self) -> str:
        parts = self.name.removesuffix(".tar.zst").split("__")
        return parts[2] if len(parts) > 2 else ""


@dataclass(frozen=True)
class Cost:
    stack_usd: float
    account_usd: float
    game_hours: float | None


@dataclass(frozen=True)
class CommandResult:
    status: str  # Success | Failed | TimedOut | Cancelled
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == "Success"

    @property
    def output(self) -> str:
        """What to show a human. The ops scripts log to stderr, so it comes first."""
        return "\n".join(p.strip() for p in (self.stderr, self.stdout) if p.strip())


class Aws:
    def __init__(self, region: str) -> None:
        session = boto3.session.Session(region_name=region)
        self.region = region
        self._ec2 = session.client("ec2", config=_BOTO)
        self._ssm = session.client("ssm", config=_BOTO)
        self._s3 = session.client("s3", config=_BOTO)
        self._cw = session.client("cloudwatch", config=_BOTO)
        # Cost Explorer is a us-east-1-only endpoint regardless of where the stack runs.
        self._ce = session.client("ce", region_name="us-east-1", config=_BOTO)

    # --- Parameter Store -------------------------------------------------------------

    async def get_parameters_by_path(self, prefix: str) -> dict[str, str]:
        def call() -> dict[str, str]:
            out: dict[str, str] = {}
            paginator = self._ssm.get_paginator("get_parameters_by_path")
            for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
                for p in page["Parameters"]:
                    out[p["Name"]] = p["Value"]
            return out

        return await asyncio.to_thread(call)

    # --- EC2 -------------------------------------------------------------------------

    async def find_instance(self, *, stack: str, role: str) -> str:
        """The one instance carrying these tags, or "".

        Terminated instances linger in DescribeInstances for an hour after a rebuild, so
        they are filtered out explicitly -- otherwise a fresh stack could be resolved to
        the corpse of the old one.
        """

        def call() -> str:
            resp = self._ec2.describe_instances(
                Filters=[
                    {"Name": "tag:pz:stack", "Values": [stack]},
                    {"Name": "tag:pz:role", "Values": [role]},
                    {
                        "Name": "instance-state-name",
                        "Values": ["pending", "running", "stopping", "stopped"],
                    },
                ]
            )
            ids = [i["InstanceId"] for r in resp["Reservations"] for i in r["Instances"]]
            if len(ids) > 1:
                log.warning("%d instances tagged pz:role=%s in %s: %s", len(ids), role, stack, ids)
            return ids[0] if ids else ""

        return await asyncio.to_thread(call)

    async def describe(self, instance_id: str) -> Instance:
        def call() -> Instance:
            resp = self._ec2.describe_instances(InstanceIds=[instance_id])
            found = [i for r in resp["Reservations"] for i in r["Instances"]]
            if not found:
                # EC2 rejects a genuinely unknown id with InvalidInstanceID.NotFound, but
                # an instance terminated moments ago can come back as an empty result
                # instead. Both mean the same thing, so both raise the same error: an
                # IndexError here is not an `AwsError`, so it would escape the presence
                # loop (killing it) and `/pz start`'s `except AwsError` budget gate
                # (turning a fail-open into a refusal) rather than being handled.
                raise ClientError(
                    {
                        "Error": {
                            "Code": INSTANCE_GONE,
                            "Message": f"The instance ID '{instance_id}' does not exist",
                        }
                    },
                    "DescribeInstances",
                )
            raw = found[0]
            return Instance(
                instance_id=raw["InstanceId"],
                state=raw["State"]["Name"],
                private_ip=raw.get("PrivateIpAddress", ""),
                public_ip=raw.get("PublicIpAddress", ""),
                launch_time=raw.get("LaunchTime"),
                instance_type=raw.get("InstanceType", ""),
            )

        return await asyncio.to_thread(call)

    async def private_ip(self, instance_id: str) -> str:
        return (await self.describe(instance_id)).private_ip

    async def start_instance(self, instance_id: str) -> str:
        def call() -> str:
            resp = self._ec2.start_instances(InstanceIds=[instance_id])
            return resp["StartingInstances"][0]["CurrentState"]["Name"]

        return await asyncio.to_thread(call)

    async def stop_instance(self, instance_id: str) -> str:
        def call() -> str:
            resp = self._ec2.stop_instances(InstanceIds=[instance_id])
            return resp["StoppingInstances"][0]["CurrentState"]["Name"]

        return await asyncio.to_thread(call)

    # --- Run Command -----------------------------------------------------------------

    async def send_command(
        self,
        instance_id: str,
        document: str,
        parameters: dict[str, str],
        *,
        timeout: int = 600,
        comment: str = "pzbot",
    ) -> CommandResult:
        """Run one of pzserver's scoped SSM documents on the game server, and wait for it.

        Callers pass a document name and typed parameters; nothing here builds a shell
        string. The document is the allowlist -- its `allowedPattern`/`allowedValues`
        constrain each parameter at the AWS layer, not by convention in this file
        (pzserver issue #29). `timeout` bounds how long this method polls for a result;
        the document's own step has its own fixed `timeoutSeconds` on the pzserver side.
        """

        def send() -> str:
            resp = self._ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName=document,
                Parameters={k: [v] for k, v in parameters.items()},
                Comment=comment[:100],
            )
            return resp["Command"]["CommandId"]

        command_id = await asyncio.to_thread(send)

        def poll() -> dict:
            return self._ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)

        deadline = asyncio.get_running_loop().time() + timeout + 30
        while True:
            await asyncio.sleep(2)
            try:
                inv = await asyncio.to_thread(poll)
            except ClientError as exc:
                # The invocation is not queryable for a beat after send_command.
                if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                    if asyncio.get_running_loop().time() > deadline:
                        raise
                    continue
                raise
            if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
                return CommandResult(
                    status=inv["Status"],
                    stdout=inv.get("StandardOutputContent", ""),
                    stderr=inv.get("StandardErrorContent", ""),
                )
            if asyncio.get_running_loop().time() > deadline:
                return CommandResult("TimedOut", inv.get("StandardOutputContent", ""), "")

    # --- S3 --------------------------------------------------------------------------

    async def list_backups(self, bucket: str, stack: str, limit: int = 200) -> list[Backup]:
        def call() -> list[Backup]:
            out: list[Backup] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=f"backups/{stack}/"):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".tar.zst"):
                        out.append(Backup(obj["Key"], obj["Size"], obj["LastModified"]))
            out.sort(key=lambda b: b.modified, reverse=True)
            return out[:limit]

        return await asyncio.to_thread(call)

    # --- CloudWatch ------------------------------------------------------------------

    async def latest_metric(
        self, namespace: str, metric: str, stack: str, *, minutes: int = 15
    ) -> float | None:
        def call() -> float | None:
            now = dt.datetime.now(dt.UTC)
            resp = self._cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric,
                Dimensions=[{"Name": "Stack", "Value": stack}],
                StartTime=now - dt.timedelta(minutes=minutes),
                EndTime=now,
                Period=60,
                Statistics=["Maximum"],
            )
            points = sorted(resp["Datapoints"], key=lambda d: d["Timestamp"])
            return points[-1]["Maximum"] if points else None

        return await asyncio.to_thread(call)

    async def put_heartbeat(self, namespace: str, stack: str) -> None:
        """Publish PZ/BotAlive=1.

        The alarm on the other end of this treats missing data as breaching, which is what
        makes it a heartbeat rather than a status check. pzserver's EC2 status-check alarms
        cover a dead host; this covers the failure they structurally cannot see -- instance
        healthy, process running, asyncio event loop wedged or the gateway connection
        silently dead. From the outside that is indistinguishable from a working bot right
        up until someone types `/pz start` and gets nothing.

        The caller is the presence loop, so this is only published once the loop has
        actually completed a cycle of real work. Publishing it from a bare timer would
        make it a liveness check on the timer rather than on the bot.
        """

        def call() -> None:
            self._cw.put_metric_data(
                Namespace=namespace,
                MetricData=[
                    {
                        "MetricName": "BotAlive",
                        "Dimensions": [{"Name": "Stack", "Value": stack}],
                        "Value": 1,
                        "Unit": "None",
                    }
                ],
            )

        await asyncio.to_thread(call)

    # --- Cost Explorer ---------------------------------------------------------------

    async def month_to_date(self, stack: str, instance_type: str = "") -> Cost:
        """Spend and running hours for the current month.

        Three calls, because they answer three different questions:

        *   the stack's spend, filtered by the `pz:stack` cost allocation tag;
        *   the whole account's spend, which is what makes it obvious when the tag has
            not been activated -- without it, the stack figure is silently $0.00 and
            reads as "we spent nothing" rather than "we cannot tell";
        *   running hours for the game instance type, which is the number that actually
            explains the bill.

        Cost Explorer charges $0.01 per request and its data lags by up to a day, so
        callers are expected to cache this. See `commands.base.Cached`.
        """

        def call() -> Cost:
            today = dt.datetime.now(dt.UTC).date()
            window = {
                "TimePeriod": {
                    "Start": today.replace(day=1).isoformat(),
                    "End": (today + dt.timedelta(days=1)).isoformat(),
                },
                "Granularity": "MONTHLY",
            }
            tag_filter = {"Tags": {"Key": "pz:stack", "Values": [stack]}}

            def total(resp: dict) -> float:
                return sum(
                    float(r["Total"]["UnblendedCost"]["Amount"]) for r in resp["ResultsByTime"]
                )

            tagged = self._ce.get_cost_and_usage(
                **window, Metrics=["UnblendedCost"], Filter=tag_filter
            )
            account = self._ce.get_cost_and_usage(**window, Metrics=["UnblendedCost"])

            hours = None
            if instance_type:
                usage = self._ce.get_cost_and_usage(
                    **window,
                    Metrics=["UsageQuantity"],
                    Filter={
                        "And": [
                            tag_filter,
                            {
                                "Dimensions": {
                                    "Key": "USAGE_TYPE_GROUP",
                                    "Values": ["EC2: Running Hours"],
                                }
                            },
                        ]
                    },
                    GroupBy=[{"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}],
                )
                for period in usage["ResultsByTime"]:
                    for group in period.get("Groups", []):
                        if group["Keys"] and group["Keys"][0] == instance_type:
                            hours = (hours or 0.0) + float(
                                group["Metrics"]["UsageQuantity"]["Amount"]
                            )

            return Cost(stack_usd=total(tagged), account_usd=total(account), game_hours=hours)

        return await asyncio.to_thread(call)
