# pzbot — Context for Claude Code

## Purpose
The Discord control plane for the Project Zomboid stack in [pzserver](../pzserver): slash
commands that start, stop, inspect, back up and restore a game server that is **stopped by
default**. Runs as `pzbot.service` on the always-on `t4g.nano` bot host in account
`020949219706`. The design of record is pzserver's `DESIGN.md` §10 — that table is the
contract this repo implements, and `tests/test_commands.py` asserts it.

**This is live.** [pzserver](../pzserver) was applied on 2026-08-22 and `pzbot.service`
has been running on the bot host `i-09158ffe716ee3c5e` since 2026-08-23 — the Discord
application, the `/pz/prod/discord/*` parameters and `deploy/install.sh` are all done.
Changes merged here do **not** reach the box on their own; deploying is a `git pull` plus
`deploy/install.sh` over SSM. Check what is actually running with
`git -C /opt/pzbot/src log --oneline -1` before assuming main and prod agree. See
[DEPLOY.md](DEPLOY.md).

## Stack & Commands
Python 3.12 · `discord.py` 2.x (app commands, gateway) · `boto3` · vendored async RCON ·
systemd on AL2023 arm64. No web framework, no database, no state on disk.

```bash
pip install -e '.[dev]'
ruff check src tests && ruff format --check src tests
pytest -q                      # 138 tests, no network, ~2s
```

There is no Python 3.12 on this Mac (system Python is 3.9, and there is no Homebrew).
To run the tests, download a standalone build into a scratch dir and make a venv from it
— the same trick pzserver's CLAUDE.md describes for Terraform:

```bash
curl -sSL -o /tmp/py.tar.gz https://github.com/astral-sh/python-build-standalone/releases/latest/download/…-aarch64-apple-darwin-install_only_stripped.tar.gz
```

The AWS credentials on this machine (`jon-claude-local`) can read the live stack.
`src/pzbot/config.py` and the read-only probe path have been verified against it; treat
anything that starts or stops the game server as an action that costs money and needs the
user's go-ahead.

## Code Map
```
src/pzbot/
  server.py      ← THE state machine + every operation. No Discord types in it.
  rcon.py        ← vendored async Source RCON; typed failures
  sandbox.py     ← SandboxVars.lua: the settings table and the Lua reader/writer. A
                   second copy of this file's remote half lives in pzserver as
                   ops/bin/pz-sandbox-tool.py, invoked by the `pz-<stack>-sandbox` SSM
                   document -- kept in sync by hand until that duplication is resolved.
  aws.py         ← one method per statement in pz-bot-role
  config.py      ← env > bot_contract.json > Parameter Store > tag discovery
  guards.py      ← guild / channel / role gates (the real permission model)
  singleflight.py← one operation at a time, rejected not queued
  audit.py       ← who did what, to a channel and to journald
  render.py      ← every embed
  bot.py         ← client, guild-scoped command sync, presence, one error handler
  commands/
    base.py      ← Ctx, the self-editing progress message, the TTL cache
    core.py      ← /pz status start stop who restart save idle cost restore, /pz config
    backups.py   ← /pz backup now|list and the restore autocomplete
    world.py     ← /pz sandbox get|set, with both halves of the picker autocompleted
deploy/          ← pzbot.service, install.sh (idempotent; also the upgrade path), env template
requirements.in  ← the direct deps and their intended ranges; humans edit THIS
requirements.txt ← pip-compile lockfile, fully pinned + hashed; install.sh --require-hashes
tests/           ← fakes for AWS and RCON; the socket tests speak real RCON over loopback
```

## Architecture
`commands/` is thin: permission check, take the single-flight lock, call `server.py`,
render, audit. All errors — `Denied`, `OperationError`, `Busy`, botocore's — propagate to
one handler in `bot.py`, which produces one ephemeral embed and one audit line. A command
that catches its own exception is a command that has stopped auditing.

`server.py` is written as if for a CLI. That is what makes it testable, and it is where
the two rules from DESIGN §10 are enforced so that no command can forget them.

## Conventions & Gotchas

- **Never cache server state.** `probe()` always does a live `DescribeInstances` plus a
  live RCON call. The box can be stopped from four places and three of them will never
  tell this process. `commands/base.Cached` exists only for Cost Explorer (which bills a
  cent a request) and the backup listing behind autocomplete — never for state.
- **`running` is not `ready`.** `Stage.BOOTING` is the whole point: PZ opens its ports
  minutes before the world has loaded. `test_the_connect_string_is_only_shown_when_the_server_is_ready`
  is the guard on this, parameterised over every stage.
- **An RCON auth failure is not a readiness failure.** A stale password must surface as
  `Stage.UNKNOWN` with the error, not as "still loading" — otherwise it hides behind a
  ten-minute timeout and then stops a healthy server.
- **Every exit from `start()` either reaches Ready or stops the instance.** A start that
  dies halfway leaves an `m7i.xlarge` billing at $0.20/hour with nobody watching.
- **Nothing user-supplied reaches a shell.** `/pz restore` validates the name against a
  regex *and* against the live S3 listing; `/pz config set` looks the key up in
  `INI_KEYS` and validates the value by that entry's own rule. `Aws.send_command` never
  builds a shell string at all — every call names one of pzserver's scoped SSM documents
  (issue #29) and passes typed parameters, so the document's own `allowedPattern` /
  `allowedValues` re-check everything at the AWS layer, not just here.
- **The budget gate fails OPEN, in three places, on purpose.** `guards.budget` lets
  `/pz start` through when no budget is configured, when Cost Explorer is unreadable, and
  when `stack_usd` is $0.00 while the account has spent something (the signature of the
  `pz:stack` cost allocation tag not being activated — the figure is fictional, not zero).
  The money guarantee is `pz-watchdog.sh` on the game server, which needs neither Discord
  nor Cost Explorer; this layer is the polite early stop, not the backstop. Turning any of
  these into a refusal converts an AWS hiccup into "nobody can play".
- **The bot has no `ssm:PutParameter`**, deliberately (pzserver DESIGN §9). That is why
  `/pz idle` edits `/etc/pz/env` on the game server and lasts only until the next boot —
  it is not a shortcut, it is the only door. Permanent means `prod.tfvars` and an apply.
- **`/pz idle off` is not off.** It sets the timeout to the session cap. The watchdog is
  the cost guarantee; a real "off" would be a way to leave an expensive instance running
  overnight from a chat message. And the timeout may never be written as `0` — the
  watchdog compares `idle >= timeout` and would stop the box on the first tick.
- **The game instance id is discovered by tag, never configured.** The IAM policy is
  scoped by `pz:stack` + `pz:role=gameserver`; following anything else would point the
  bot at an instance its own credentials cannot touch.
- **Commands are registered to one guild.** Instant sync, and they exist nowhere else —
  which is a real mitigation if the token ever leaks, alongside the guild check in
  `guards.py`.
- **Discord's own permissions are top-level only.** `default_member_permissions` applies
  to `/pz`, not to `/pz restore`, so admin subcommands *cannot* be hidden from players in
  the picker. The role check in `guards.py` is the entire gate. Do not "fix" this by
  splitting the group into top-level commands.
- **Two config files, two commands, two behaviours.** `/pz config` → `<server>.ini` →
  `reloadoptions`, live. `/pz sandbox` → `<server>_SandboxVars.lua` → read only at
  startup. Conflating them is the easiest mistake here; the table at the top of
  `sandbox.py` is the reference.
- **Sandbox edits go stop → edit → start, never edit → restart.** A running server owns
  `SandboxVars.lua` and rewrites it wholesale when an admin changes options from inside
  the game, so an edit made underneath a live server can be silently undone. The failure
  path matters too: if the edit fails after the game is stopped, `sandbox_set` starts it
  back up on the old settings rather than leaving an instance billing with no server.
- **The sandbox enum *labels* are ours; the numbers are the game's.** They match the
  in-game sandbox screen for B41/B42. `/pz sandbox get` prints the raw number next to the
  label (`2 (Saliva only)`) and prints `(not a known option)` for a number outside our
  table, so a build that reorders an option list shows up as a visible mismatch rather
  than a mislabelled setting. If one ever looks wrong, the in-game screen is the truth.
- **Document names are a convention, not published config.** `Config.document()` builds
  `pz-<stack>-<suffix>` to match pzserver's own `local.name_prefix`, the same way
  `ssm_prefix` mirrors pzserver's parameter tree. Renaming a document on either side
  without the other is a silent break — SSM's error for an unknown document name does
  not mention this convention at all.
- **`deploy/install.sh` must stay idempotent** — it is the upgrade path, and it runs as
  root on the only host that can reach the game server.
- **Never hand-edit `requirements.txt`.** It is a `pip-compile` lockfile and the installer
  uses `--require-hashes`, which fails closed if any package lacks a hash — so a hand edit
  surfaces as a broken deploy on the box with no SSH. Edit `requirements.in` and run
  `pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in`.
  CI checks the two are in sync.
- **Files must stay LF** (`.gitattributes`), same rule as pzserver: `deploy/` lands on
  Linux and is read by shebang and systemd's parser.

## Key Files
- [src/pzbot/server.py](src/pzbot/server.py) — the state machine, the start/stop
  choreography, the restore guards
- [src/pzbot/sandbox.py](src/pzbot/sandbox.py) — the world's rules, and the one module
  that runs in both places
- [src/pzbot/guards.py](src/pzbot/guards.py) — why gate 2 is the only real one
- [src/pzbot/config.py](src/pzbot/config.py) — precedence, and what the bot refuses to
  start without
- [deploy/pzbot.service](deploy/pzbot.service) — `RestartPreventExitStatus=78` and the
  memory ceiling on a 512 MB box
- [DEPLOY.md](DEPLOY.md) — Discord setup, the six parameters, rotation, troubleshooting
- [../pzserver/DESIGN.md](../pzserver/DESIGN.md) §10 — the design of record
