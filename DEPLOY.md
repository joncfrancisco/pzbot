# Deploy & operations runbook

Everything here happens once, in this order. Steps 1 and 2 are in Discord and AWS; step 3
is the only one that touches the box.

There is no SSH to the bot host and no key pair. The way in is:

```bash
aws ssm start-session --target "$(terraform -chdir=infra output -raw bot_instance_id)"
# ...or, from here, without a terraform binary:
aws ssm start-session --target i-09158ffe716ee3c5e
```

---

## 1. The Discord application

The application already exists. Its **id** is `1540914537023279196` and its public key is
`2dbdf1de…` — neither is a secret (the id is in every invite URL, and the public key only
matters for HTTP interaction endpoints, which this bot does not use: it holds a gateway
connection instead).

In the [developer portal](https://discord.com/developers/applications), under **Bot**:

- **Reset Token**, and keep the value for step 2. It is shown exactly once.
- Leave **all three privileged intents OFF.** The bot asks for `Intents.none()` — every
  interaction it handles is signed and delivered by Discord, so it never needs to read
  what anyone types. If Message Content is ever switched on, it is switched on for
  nothing.
- **Public Bot**: off, unless you want other people adding it to their servers.

Invite it with exactly the permissions it uses — view channels, send messages, embed
links (`19456`):

```
https://discord.com/oauth2/authorize?client_id=1540914537023279196&scope=bot+applications.commands&permissions=19456
```

Commands are registered **to one guild**, so they appear the moment the bot connects
rather than propagating for an hour — and they exist nowhere else.

### The ids you need

Turn on **Settings → Advanced → Developer Mode** in Discord, then right-click → *Copy ID*:

| Value | Where from |
|---|---|
| `guild_id` | Right-click the server name |
| `role_admin` | Server Settings → Roles → right-click the admin role |
| `role_player` | The role that may start and stop the server. Optional — leave it unset and every member is a player. |
| `channel_main` | The channel(s) `/pz` works in. Comma-separated for more than one. |
| `channel_audit` | Where "who did what" goes. A private channel is the point. |

---

## 2. Parameter Store

Six values, all under `/pz/prod/discord/`. The bot reads them at startup through its
instance role; nothing lands on disk.

```bash
aws ssm put-parameter --name /pz/prod/discord/token         --type SecureString --value 'MTU0…'
aws ssm put-parameter --name /pz/prod/discord/guild_id      --type SecureString --value '…'
aws ssm put-parameter --name /pz/prod/discord/role_admin    --type SecureString --value '…'
aws ssm put-parameter --name /pz/prod/discord/role_player   --type SecureString --value '…'
aws ssm put-parameter --name /pz/prod/discord/channel_main  --type SecureString --value '…'
aws ssm put-parameter --name /pz/prod/discord/channel_audit --type SecureString --value '…'
```

Only the token is genuinely secret; the ids are `SecureString` anyway because they are
the allowlist, and an attacker who knows which channel and role to target has done half
the work. `role_player` is the one you may omit.

Check what is there:

```bash
aws ssm get-parameters-by-path --path /pz/prod --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sort
```

The bot refuses to start if any of `token`, `guild_id`, `role_admin`, `channel_main`,
`channel_audit` or `rcon_password` is missing, and names the ones that are.

---

## 3. Install on the bot host

```bash
aws ssm start-session --target i-09158ffe716ee3c5e
sudo -i
dnf install -y git
git clone https://github.com/joncfrancisco/pzbot /opt/pzbot/src
/opt/pzbot/src/deploy/install.sh --connect-host pz.joncfrancis.co
```

`install.sh` is idempotent — it is also the upgrade path. It reads the region and the
`pz:stack` tag from the host's own metadata, builds the venv, writes `/etc/pzbot/env`,
installs the unit and restarts it.

### Verify

```bash
systemctl status pzbot
journalctl -u pzbot -n 50 --no-pager
```

A healthy start logs one line with the resolved configuration:

```
INFO pzbot: stack=prod region=us-east-1 game=i-0c319547d110e4179 rcon=10.20.1.171:27015 connect=pz.joncfrancis.co:16261
INFO pzbot.bot: synced 13 commands to guild …
```

Then, in Discord: `/pz status` should answer with `⚫ Stopped` and the fixed-floor note.
`/pz start` is the real test — three to seven minutes, one message that edits itself.

---

## Upgrading

```bash
sudo -i
git -C /opt/pzbot/src pull
/opt/pzbot/src/deploy/install.sh
```

Nothing is cached across a restart, so an upgrade mid-session is safe: the bot re-reads
the world's actual state on the next command. The only thing lost is an in-flight
progress message, and the operation behind it (an EC2 start, an SSM command) carries on
regardless — the state machine picks it up again on the next `/pz status`.

**`/opt/pzbot/src` is the one checkout.** Nothing enforces that, and it is easy to clone a
second one somewhere else during a hurried deploy and end up upgrading the copy that is
not installed. If in doubt, `git -C /opt/pzbot/src log --oneline -1` should match
`origin/main`, and there should be no other checkout under `/opt`.

**Verify the deployed code, not just the service state.** `systemctl is-active` says
nothing about *which build* is running — a stale install looks identical. Grep the
installed package:

```bash
P=/opt/pzbot/venv/lib64/python3.12/site-packages/pzbot
grep -c "def budget" $P/guards.py       # PZ-08 budget kill-switch
grep -c "put_heartbeat" $P/aws.py       # PZ-04 heartbeat
/opt/pzbot/venv/bin/pip list --format=freeze | grep -E '^(discord.py|boto3)='
```

And confirm the heartbeat is actually reaching CloudWatch, which is the only check that
proves the event loop is alive end to end:

```bash
aws cloudwatch list-metrics --namespace PZ --metric-name BotAlive
```

## Changing the world's rules

Two files, two commands, and the difference is which one needs a restart.

| | `/pz config` | `/pz sandbox` |
|---|---|---|
| File | `pzprod.ini` | `pzprod_SandboxVars.lua` |
| Holds | slots, PVP, chat, the server browser entry | day length, zombie population, infection, loot |
| Applied | immediately, by RCON `reloadoptions` | only when the server starts |

So `/pz sandbox set` restarts the server by default (`apply:True`), and does it in the
order **stop → edit → start**. That ordering is deliberate: a running server owns
`SandboxVars.lua`, and an admin changing options from inside the game rewrites the whole
file — which would silently undo an edit made underneath it. Pass `apply:False` to batch
several changes and restart once with `/pz restart`.

```
/pz sandbox get                        → everything, grouped as the in-game screen groups it
/pz sandbox get setting:Transmission   → one setting, its options, and which one is live
/pz sandbox set setting:… value:…      → both halves come from the picker
```

The three most commonly asked for:

| Ask | Setting | Value |
|---|---|---|
| Longer days | `DayLength` | `2 hours`, `3 hours`, … |
| Fewer zombies | `ZombieConfig.PopulationMultiplier` | `0.5` (and the `Zombies` preset for the in-game screen) |
| Infection by bite only | `ZombieLore.Transmission` | `Saliva only` |

Settings marked **only applies to a new world** (start date, starting population) are read
when the map is generated. The command says so before you spend a restart on one.

The option *labels* in the picker are this repo's, taken from the in-game sandbox screen
for Build 41/42; the numbers are the game's. `/pz sandbox get` shows both (`2 (Saliva
only)`), so if a game update ever reorders an option list the mismatch is visible — and a
number outside the known list renders as `(not a known option)` rather than being
mislabelled. **Worth eyeballing once against the in-game sandbox screen after the first
deploy.**

Every change keeps the previous file on the box as `*_SandboxVars.lua.pzbot.bak`, and
`Server/` is in every backup — so `/pz restore` also rolls sandbox settings back.

## Rotating the Discord token

```bash
aws ssm put-parameter --name /pz/prod/discord/token --type SecureString --overwrite --value '…'
aws ssm send-command --instance-ids i-09158ffe716ee3c5e \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl restart pzbot"]'
```

Two commands, because tokens leak and the fix should not require thinking. If the token
is rejected, the bot exits **78** and systemd deliberately does *not* restart it — the
useful log line stays at the end of `journalctl -u pzbot` instead of scrolling past a
thousand identical retries.

## Rotating the RCON password

pzserver's runbook covers the server side. On this side there is nothing to do but
restart: the bot reads `/pz/prod/rcon_password` at startup.

```bash
aws ssm send-command --instance-ids i-09158ffe716ee3c5e \
  --document-name AWS-RunShellScript --parameters 'commands=["systemctl restart pzbot"]'
```

Until it is restarted, `/pz status` reports **Unknown** with an authentication error
rather than "still loading" — that distinction is deliberate, and it is the symptom to
recognise here.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Unit stops immediately, exit 78 | Missing Parameter Store value, or a rejected token. The log says which. |
| `/pz` does not appear in Discord | The bot is not in the guild, or `guild_id` is wrong. Check the sync line in the log. |
| Every command says "not configured for this server" | `guild_id` does not match the guild the command came from. |
| `/pz` works nowhere | `channel_main` does not include the channel you are in. |
| Status says **Unknown**, "authentication failed" | The bot's RCON password is stale. Restart it (above). |
| Status sits on **Loading the world** for 10+ minutes | PZ itself. `journalctl -u pzserver` on the *game* server. The bot stops the instance rather than let it bill. |
| `/pz cost` shows `$0.00` next to a real account total | The `pz:stack` cost allocation tag was never activated — pzserver `DEPLOY.md` step 1. |
| Everything is slow, memory climbs | `MemoryMax=320M` in the unit will restart it. The box has 512 MB total. |

### Reading the state without Discord

If Discord itself is down, the bot is irrelevant — the manual commands in pzserver's
runbook are the fallback, and the idle watchdog runs on the game server precisely so the
cost guarantee does not depend on any of this being healthy.

```bash
aws ec2 start-instances --instance-ids i-0c319547d110e4179
aws ec2 stop-instances  --instance-ids i-0c319547d110e4179
```

## Uninstalling

```bash
systemctl disable --now pzbot
rm -rf /opt/pzbot /etc/pzbot /etc/systemd/system/pzbot.service
systemctl daemon-reload
```

The host itself belongs to pzserver's Terraform; leave it alone.
