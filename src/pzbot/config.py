"""Where the bot's configuration comes from, and why it comes from there.

Three sources, in precedence order:

1.  **Environment** (`/etc/pzbot/env`, written once by `deploy/install.sh`). Only the
    handful of values that cannot be discovered: which stack, which region, the DNS name
    players connect to.
2.  **`terraform output -json bot_contract`**, if a copy is on disk. Optional; it exists
    so a deploy can be exact rather than inferred.
3.  **Parameter Store**, under `/pz/<stack>/`. Everything else -- secrets, and the
    runtime knobs `pzserver` already publishes there for the game host.

The game server's *instance id* is not configuration at all: it is discovered by tag
(`pz:stack` + `pz:role=gameserver`), the same condition the bot's IAM policy is scoped
by. If the instance is ever rebuilt, the bot follows it without a redeploy, and it can
never be pointed at a box its own credentials cannot touch.

Nothing here is re-read at runtime *except* the mutable knobs -- see `refresh_runtime`.
Secrets and identifiers are read once at startup so a Parameter Store outage cannot take
the bot down mid-session.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Everything under /pz/<stack>/discord/ that the bot cannot start without.
REQUIRED_DISCORD = ("token", "guild_id", "role_admin", "channel_main", "channel_audit")


class ConfigError(RuntimeError):
    pass


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _ids(value: str | None) -> tuple[int, ...]:
    """Parse a comma-separated list of Discord snowflakes.

    Comma-separated because `channel_main` is genuinely plural in practice -- a general
    channel and a #pz channel -- and because a single value is the same syntax.
    """
    if not value:
        return ()
    out = []
    for part in str(value).replace(" ", "").split(","):
        if part:
            try:
                out.append(int(part))
            except ValueError as exc:
                raise ConfigError(f"{part!r} is not a Discord id") from exc
    return tuple(out)


@dataclass
class Runtime:
    """The knobs that may change while the bot is running.

    These live in Parameter Store because the *game host* reads them there on every boot
    (pzserver DESIGN C6). `/pz idle` writes one of them, and re-reads rather than caches,
    so a change made with `aws ssm put-parameter` is picked up without a restart.
    """

    idle_warn_min: int = 25
    idle_timeout_min: int = 30
    session_cap_hours: int = 12
    xmx: str = ""

    # The ceiling `/pz start` refuses past, for the player tier. Runtime rather than
    # startup config on purpose: this is the one knob whose whole job is to be retuned
    # when it turns out to be set wrong, and needing a redeploy to raise a budget in the
    # middle of the month you have blown it is exactly the wrong ergonomics.
    #
    # 0.0 means no gate. That is the honest default for an unconfigured bot -- see
    # guards.budget for why this whole layer fails OPEN rather than closed.
    monthly_budget_usd: float = 0.0


@dataclass
class Config:
    region: str
    stack: str
    ssm_prefix: str

    game_instance_id: str
    rcon_host: str
    rcon_port: int
    rcon_password: str

    connect_host: str
    connect_port: int
    server_name: str
    backup_bucket: str
    alert_topic_arn: str
    metric_namespace: str

    token: str = field(repr=False)
    guild_id: int
    role_admin: int
    role_player: int  # 0 == every member of the guild is a player
    channels_allowed: tuple[int, ...]
    channel_audit: int

    runtime: Runtime

    # Timings. Named here rather than buried in the command modules because they are the
    # numbers most likely to want tuning after a few real sessions.
    start_running_timeout: int = 300  # DESIGN: Pending -> Failed after 5 minutes
    start_ready_timeout: int = 600  # DESIGN: Booting -> Failed after 10 minutes
    stop_grace_seconds: int = 120
    poll_seconds: int = 10

    @property
    def connect_string(self) -> str:
        return f"{self.connect_host}:{self.connect_port}"

    def ssm(self, suffix: str) -> str:
        return f"{self.ssm_prefix}/{suffix}"

    def document(self, suffix: str) -> str:
        """Name of one of pzserver's scoped SSM documents (issue #29).

        Mirrors pzserver's own `local.name_prefix = "pz-${var.stack}"` -- the two
        repos must agree on this without either publishing it to the other, the same
        way `ssm_prefix` already does above.
        """
        return f"pz-{self.stack}-{suffix}"


def _contract_from_disk() -> dict:
    path = os.environ.get("PZBOT_CONTRACT", "/etc/pzbot/bot_contract.json")
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring unreadable contract at %s: %s", path, exc)
        return {}
    # `terraform output -json` wraps values in {"value": ..., "type": ...}.
    if isinstance(data, dict) and "value" in data and isinstance(data["value"], dict):
        data = data["value"]
    return data if isinstance(data, dict) else {}


async def load(aws) -> Config:
    """Assemble the configuration, failing loudly and specifically if it cannot."""
    contract = _contract_from_disk()

    def pick(env_key: str, contract_key: str, default: str = "") -> str:
        return os.environ.get(env_key) or str(contract.get(contract_key) or "") or default

    stack = pick("PZBOT_STACK", "stack", "prod")
    region = pick("PZBOT_REGION", "region") or aws.region
    ssm_prefix = pick("PZBOT_SSM_PREFIX", "ssm_prefix", f"/pz/{stack}")

    params = await aws.get_parameters_by_path(ssm_prefix)

    def param(suffix: str, default: str = "") -> str:
        return params.get(f"{ssm_prefix}/{suffix}", default)

    missing = [k for k in REQUIRED_DISCORD if not param(f"discord/{k}")]
    if missing:
        raise ConfigError(
            "Parameter Store is missing "
            + ", ".join(f"{ssm_prefix}/discord/{k}" for k in missing)
            + ".\nPut them with:\n"
            + "\n".join(
                f"  aws ssm put-parameter --name {ssm_prefix}/discord/{k} "
                f"--type SecureString --value '…'"
                for k in missing
            )
        )
    if not param("rcon_password"):
        raise ConfigError(
            f"{ssm_prefix}/rcon_password is missing or the bot cannot decrypt it. "
            "Without it every readiness check fails and the bot would report a healthy "
            "server as unreachable; refusing to start."
        )

    # Discovered, not configured -- see the module docstring.
    instance_id = os.environ.get("PZBOT_GAME_INSTANCE_ID") or str(
        contract.get("game_instance_id") or ""
    )
    discovered = await aws.find_instance(stack=stack, role="gameserver")
    if discovered and instance_id and discovered != instance_id:
        log.warning(
            "configured game instance %s but the pz:stack=%s pz:role=gameserver tag is on "
            "%s; following the tag, because that is what the IAM policy is scoped to",
            instance_id,
            stack,
            discovered,
        )
    instance_id = discovered or instance_id
    if not instance_id:
        raise ConfigError(
            f"no instance tagged pz:stack={stack} pz:role=gameserver in {region}. "
            "Either the stack is not applied or this host's role cannot see it."
        )

    rcon_host = (
        os.environ.get("PZBOT_RCON_HOST")
        or str(contract.get("game_private_ip") or "")
        or await aws.private_ip(instance_id)
        or ""
    )

    role_player = _ids(param("discord/role_player"))
    channels = _ids(param("discord/channel_main"))
    audit = _ids(param("discord/channel_audit"))

    return Config(
        region=region,
        stack=stack,
        ssm_prefix=ssm_prefix,
        game_instance_id=instance_id,
        rcon_host=rcon_host,
        rcon_port=_int(pick("PZBOT_RCON_PORT", "rcon_port"), 27015),
        rcon_password=param("rcon_password"),
        connect_host=pick("PZBOT_CONNECT_HOST", "connect_host"),
        connect_port=_int(os.environ.get("PZBOT_CONNECT_PORT"), 16261),
        server_name=param("config/server_name", "pzprod"),
        backup_bucket=param("config/backup_bucket") or str(contract.get("backup_bucket") or ""),
        alert_topic_arn=param("config/alert_topic_arn")
        or str(contract.get("alert_topic_arn") or ""),
        metric_namespace=str(contract.get("metric_namespace") or "PZ"),
        token=param("discord/token"),
        guild_id=_ids(param("discord/guild_id"))[0],
        role_admin=_ids(param("discord/role_admin"))[0],
        role_player=role_player[0] if role_player else 0,
        channels_allowed=tuple(dict.fromkeys(channels + audit)),
        channel_audit=audit[0] if audit else 0,
        runtime=Runtime(
            idle_warn_min=_int(param("config/idle_warn_min"), 25),
            idle_timeout_min=_int(param("config/idle_timeout_min"), 30),
            session_cap_hours=_int(param("config/session_cap_hours"), 12),
            xmx=param("config/xmx"),
            monthly_budget_usd=_float(
                param("config/monthly_budget_usd"),
                # bot_contract is the fallback for a fresh install whose Parameter Store
                # tree predates pzserver publishing this value.
                _float(str(contract.get("monthly_budget_usd") or ""), 0.0),
            ),
        ),
    )


async def refresh_runtime(aws, cfg: Config) -> Runtime:
    """Re-read the mutable knobs. Cheap, and keeps `/pz status` honest.

    A failure here is deliberately non-fatal: stale idle numbers in an embed are a
    cosmetic problem, and a Parameter Store hiccup must not fail a status command.
    """
    try:
        params = await aws.get_parameters_by_path(f"{cfg.ssm_prefix}/config")
    except Exception as exc:  # noqa: BLE001 -- see docstring
        log.warning("could not refresh runtime config: %s", exc)
        return cfg.runtime

    def param(suffix: str, default: str = "") -> str:
        return params.get(f"{cfg.ssm_prefix}/config/{suffix}", default)

    cfg.runtime = Runtime(
        idle_warn_min=_int(param("idle_warn_min"), cfg.runtime.idle_warn_min),
        idle_timeout_min=_int(param("idle_timeout_min"), cfg.runtime.idle_timeout_min),
        session_cap_hours=_int(param("session_cap_hours"), cfg.runtime.session_cap_hours),
        xmx=param("xmx", cfg.runtime.xmx),
        monthly_budget_usd=_float(param("monthly_budget_usd"), cfg.runtime.monthly_budget_usd),
    )
    return cfg.runtime
