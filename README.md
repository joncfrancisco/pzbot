# pzbot

The Discord control plane for the Project Zomboid server that
[pzserver](https://github.com/joncfrancisco/pzserver) provisions. It exists so the game
server can be **stopped by default** — someone types `/pz start`, the world comes up in a
few minutes, and it shuts itself down when the last person leaves. Compute cost tracks
play time instead of wall-clock time.

Runs as `pzbot.service` on the always-on `t4g.nano` bot host, which is a separate box for
one reason: `/pz start` has to work while the game server is powered off. A one-page map
of the whole AWS account is in [joninfra](https://github.com/joncfrancisco/joninfra).

```
Discord ──▶ pzbot (t4g.nano, always on)
              ├── ec2:StartInstances / StopInstances ──▶ game server (stopped by default)
              ├── RCON tcp/27015 (private, sg-bot only) ── "is it ready? who is on?"
              ├── ssm:SendCommand ──▶ /opt/pz/bin/{pz-backup,pz-restore}.sh
              └── SSM Parameter Store ── token, RCON password, role and channel ids
```

## Commands

| Command | Who | What it does |
|---|---|---|
| `/pz status` | player | State, players, uptime, idle timer, last backup, spend. Live, never cached. |
| `/pz start` | player | Starts the instance and follows it to *ready*, editing one progress message. |
| `/pz stop` | player | Warns players, backs up, stops. The world is saved either way. |
| `/pz stop force:true` | **admin** | Same, minus the warning period. Still saves. |
| `/pz who` | player | Who is online. |
| `/pz restart` | **admin** | Bounces the game without stopping the instance. |
| `/pz save` | **admin** | Forces a save now. |
| `/pz backup now [label]` | **admin** | Labelled backup without stopping. |
| `/pz backup list` | player | What is in the bucket. |
| `/pz restore <backup>` | **admin** | Two-step confirm, autocompleted from S3. Destructive. |
| `/pz config get\|set` | **admin** | An allowlist of `.ini` keys, then `reloadoptions`. Live. |
| `/pz sandbox get [setting]` | player | The world's rules: time, zombies, infection, loot. |
| `/pz sandbox set` | **admin** | Change one of them. Both halves autocompleted; restarts to apply. |
| `/pz idle <minutes\|off>` | **admin** | Retunes the idle shutdown for this session. |
| `/pz cost` | player | Month-to-date spend and running hours. |

### Two config commands, because there are two files

`/pz config` edits `<server>.ini` — how the *server* runs (slots, PVP, chat). RCON
`reloadoptions` picks it up immediately.

`/pz sandbox` edits `<server>_SandboxVars.lua` — how the *world* works (day length,
zombie population, how infection is transmitted). The game only reads that file when it
starts, so changing one **restarts the server**, in the order stop → edit → start: a
running server owns the file and rewrites it wholesale if an admin changes options
in-game, which would silently undo an edit made underneath it.

Neither takes free text. `/pz sandbox set` autocompletes both the setting and its value,
so "saliva only" is a thing you pick rather than something you have to know is
`ZombieLore.Transmission = 2`:

```
/pz sandbox set setting:Infection · Transmission — How the infection is caught
                value:Saliva only
                apply:True
```

## The three rules it is built around

- **Nothing is cached.** Every command re-reads `DescribeInstances` and probes RCON live.
  The game server can be stopped from four places — Discord, the AWS console, the idle
  watchdog, a budget alarm — and three of them will never tell this process about it.
- **"Running" is not "ready."** PZ opens its ports two to five minutes before the world
  has finished loading. Readiness is RCON answering `players` with a well-formed
  response, and the connect string is only ever shown next to that state.
- **One operation at a time, rejected rather than queued.** Two people typing `/pz start`
  in the same second produce one start and one "hang tight". A `/pz stop` arriving during
  a start is refused, because queuing it would shut down on top of the six people who
  just connected.

## Status

**Deployed and running.** `pzbot.service` is `active (running)` and `enabled` on the bot
host `i-09158ffe716ee3c5e`, serving guild `189561197673054208` against the live `prod`
stack. 127 tests pass. All three setup steps — the Discord application, the
`/pz/prod/discord/*` parameters, and `deploy/install.sh` — are done; they are kept in
[DEPLOY.md](DEPLOY.md) for rebuilds and rotations.

To see which build is actually on the box:

```bash
aws ssm start-session --target i-09158ffe716ee3c5e
git -C /opt/pzbot/src log --oneline -1
```

`/pz start` is the normal way to bring the world up. The CLI equivalents
(`aws ec2 start-instances` / `stop-instances`) still work and are the fallback for when
Discord itself is the broken part — see pzserver's
[DEPLOY.md § Running it by hand](https://github.com/joncfrancisco/pzserver/blob/main/DEPLOY.md#running-it-by-hand-before-the-bot-exists).

## Layout

| | |
|---|---|
| [`src/pzbot/server.py`](src/pzbot/server.py) | The state machine and every operation. No Discord in it. |
| [`src/pzbot/rcon.py`](src/pzbot/rcon.py) | Vendored async Source RCON. |
| [`src/pzbot/sandbox.py`](src/pzbot/sandbox.py) | The sandbox settings table and the Lua reader/writer. Runs here *and* on the game server. |
| [`src/pzbot/aws.py`](src/pzbot/aws.py) | EC2, SSM, S3, CloudWatch, Cost Explorer — one method per IAM statement. |
| [`src/pzbot/commands/`](src/pzbot/commands) | The Discord surface: thin bodies over `server.py`. |
| [`src/pzbot/guards.py`](src/pzbot/guards.py) | The permission gates that are actually load-bearing. |
| [`deploy/`](deploy) | systemd unit, installer, env template. |
| [DEPLOY.md](DEPLOY.md) | Discord setup, install, rotation, troubleshooting. |

```bash
pip install -e '.[dev]'
ruff check src tests && ruff format --check src tests
pytest -q
```
