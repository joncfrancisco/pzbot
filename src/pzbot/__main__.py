"""Entry point. `python -m pzbot`, or the `pzbot` console script under systemd.

Exit codes matter here, because systemd is what reads them:

*   **78** (`EX_CONFIG`) means a human has to do something -- a missing parameter, a token
    Discord rejected. `pzbot.service` sets `RestartPreventExitStatus=78`, so the unit
    stops instead of restarting into the same wall every five seconds and burying the
    one useful log line under a thousand identical ones.
*   Anything else is treated as transient and restarted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord

from . import config as config_mod
from .aws import Aws, AwsError
from .bot import PzBot

EX_CONFIG = 78

log = logging.getLogger("pzbot")


def _setup_logging() -> None:
    # No timestamps: journald stamps every line already, and two timestamps per line is
    # the sort of thing you only notice when you are trying to read logs at 2am.
    logging.basicConfig(
        level=os.environ.get("PZBOT_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


async def _run() -> int:
    region = os.environ.get("PZBOT_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    aws = Aws(region)

    try:
        cfg = await config_mod.load(aws)
    except config_mod.ConfigError as exc:
        log.error("cannot start:\n%s", exc)
        return EX_CONFIG
    except AwsError as exc:
        # Deliberately NOT EX_CONFIG: an AccessDenied or a throttle on the way in is
        # usually a stack mid-`terraform apply` or IMDS not up yet on a fresh boot, and
        # the right answer is the ten-second retry systemd already does. What this adds
        # is the log line -- without it the botocore exception escapes `asyncio.run` as a
        # bare traceback, and which call failed for which principal is buried in it.
        log.error("cannot read this stack's configuration from AWS: %s", exc)
        return 1

    log.info(
        "stack=%s region=%s game=%s rcon=%s:%s connect=%s",
        cfg.stack,
        cfg.region,
        cfg.game_instance_id,
        cfg.rcon_host,
        cfg.rcon_port,
        cfg.connect_string,
    )

    bot = PzBot(cfg, aws)
    try:
        await bot.start(cfg.token)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the token in %s. Rotate it in the developer portal, put the "
            "new one in Parameter Store, and restart -- see DEPLOY.md.",
            cfg.ssm("discord/token"),
        )
        return EX_CONFIG
    except discord.PrivilegedIntentsRequired:
        # Should be impossible -- this bot asks for Intents.none() -- but the failure is
        # otherwise a bare traceback in journald at 2am.
        log.error("Discord asked for privileged intents this bot does not use.")
        return EX_CONFIG
    finally:
        await bot.close()
    return 0


def main() -> int:
    _setup_logging()
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
