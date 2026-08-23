"""Sandbox options: the world's rules, in `<server>_SandboxVars.lua`.

This is a different file, with different semantics, from the `.ini` that `/pz config`
edits, and conflating the two is the easiest mistake to make here:

|  | `<server>.ini` | `<server>_SandboxVars.lua` |
|---|---|---|
| Holds | how the *server* runs -- slots, PVP, chat | how the *world* works -- time, zombies |
| Applied by | RCON `reloadoptions`, live | reading the file at startup |
| Changing it means | nothing, it is instant | **a restart** |

**This module is executed in two places.** The bot imports it for the settings table and
the validation; it is also base64-shipped to the game server and run there as a script
(`python3 - get|set …`), because that is where the file is. That is why it imports
nothing outside the standard library and raises its own exception type: the copy that
parses the Lua is the same code that is unit-tested here, rather than a second
implementation embedded in a shell string.

On the enum labels below: they are the options as the in-game sandbox screen presents
them for Build 41/42. The **numbers** are what the game reads, and they are what `get`
prints alongside each label -- so if a future build reorders an option list, the mismatch
is visible in `/pz sandbox get` rather than silently applying the wrong setting.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field


class SandboxError(Exception):
    """Bad setting, bad value, or a file that is not what we expected."""


@dataclass(frozen=True)
class Setting:
    kind: str  # enum | int | float | bool
    help: str
    options: dict[int, str] = field(default_factory=dict)
    low: float = 0
    high: float = 0
    suggest: tuple[str, ...] = ()
    # True for settings the game only consults when it generates the world. Changing
    # one of these on an existing save does nothing, and saying so is the difference
    # between "the bot is broken" and "that is not how the game works".
    world_gen_only: bool = False

    @property
    def summary(self) -> str:
        if self.kind == "enum":
            return " · ".join(f"{k}={v}" for k, v in self.options.items())
        if self.kind == "bool":
            return "true or false"
        return f"{self.low:g}–{self.high:g}"


def _enum(help: str, *labels: str, world_gen_only: bool = False) -> Setting:
    """Sandbox enums are 1-indexed, in the order the in-game dropdown lists them."""
    return Setting(
        kind="enum",
        help=help,
        options={i: label for i, label in enumerate(labels, start=1)},
        world_gen_only=world_gen_only,
    )


# Loot rarity is one option list used by three settings.
_RARITY = ("None", "Extremely rare", "Rare", "Normal", "Common", "Abundant")

# The curated set. Not the whole sandbox -- there are well over a hundred options, and a
# Discord picker that lists all of them is a picker nobody can use. These are the ones a
# group actually changes between seasons, plus the three that prompted the command:
# passage of time, zombie population, and how infection is transmitted.
SETTINGS: dict[str, Setting] = {
    # --- Time -------------------------------------------------------------------------
    "DayLength": _enum(
        "How long one in-game day takes in real time",
        "15 minutes",
        "30 minutes",
        "1 hour",
        "2 hours",
        "3 hours",
        "4 hours",
        "5 hours",
        "12 hours",
        "Real time",
    ),
    "StartYear": Setting("int", "Year the world starts in", low=1, high=100, world_gen_only=True),
    "StartMonth": _enum(
        "Month the world starts in",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        world_gen_only=True,
    ),
    "StartDay": Setting(
        "int", "Day of the month the world starts on", low=1, high=31, world_gen_only=True
    ),
    "StartTime": _enum(
        "Time of day the world starts at",
        "7 AM",
        "9 AM",
        "12 PM",
        "2 PM",
        "5 PM",
        "9 PM",
        "12 AM",
        "3 AM",
        "5 AM",
        world_gen_only=True,
    ),
    # --- Infection --------------------------------------------------------------------
    "ZombieLore.Transmission": _enum(
        "How the infection is caught",
        "Blood + Saliva",
        "Saliva only",
        "Everyone's infected",
    ),
    "ZombieLore.Mortality": _enum(
        "How long an infected survivor lasts",
        "Instant",
        "0-30 seconds",
        "0-1 minutes",
        "0-12 hours",
        "2-3 days",
        "1-2 weeks",
    ),
    "ZombieLore.Reanimate": _enum(
        "How long a corpse takes to get back up",
        "Instant",
        "0-1 minutes",
        "0-12 hours",
        "2-3 days",
        "1-2 weeks",
    ),
    # --- The zombies themselves --------------------------------------------------------
    "ZombieLore.Speed": _enum("How fast they move", "Sprinters", "Fast shamblers", "Shamblers"),
    "ZombieLore.Strength": _enum("How hard they hit", "Superhuman", "Normal", "Weak"),
    "ZombieLore.Toughness": _enum("How hard they are to kill", "Tough", "Normal", "Fragile"),
    "ZombieLore.Cognition": _enum(
        "How well they navigate", "Navigate + use doors", "Navigate", "Basic navigation"
    ),
    "ZombieLore.Memory": _enum("How long they remember you", "Long", "Normal", "Short", "None"),
    "ZombieLore.Sight": _enum("How well they see", "Eagle", "Normal", "Poor"),
    "ZombieLore.Hearing": _enum("How well they hear", "Pinpoint", "Normal", "Poor"),
    "ZombieLore.ZombiesDragDown": Setting("bool", "They can drag you to the ground"),
    "ZombieLore.ZombiesFenceLunge": Setting("bool", "They can lunge over fences"),
    # --- Population ---------------------------------------------------------------------
    "Zombies": _enum(
        "The population preset shown on the in-game sandbox screen. The multipliers "
        "below are what the population manager actually reads",
        "Insane",
        "High",
        "Normal",
        "Low",
        "None",
    ),
    "ZombieConfig.PopulationMultiplier": Setting(
        "float",
        "Overall population. 1.0 is normal, 0.5 is half as many",
        low=0.0,
        high=4.0,
        suggest=("0.0", "0.35", "0.5", "1.0", "1.5", "2.0", "3.0"),
    ),
    "ZombieConfig.PopulationStartMultiplier": Setting(
        "float",
        "Population on day one",
        low=0.0,
        high=4.0,
        suggest=("0.5", "1.0", "1.5"),
        world_gen_only=True,
    ),
    "ZombieConfig.PopulationPeakMultiplier": Setting(
        "float", "Population at the peak day", low=0.0, high=4.0, suggest=("1.0", "1.5", "2.0")
    ),
    "ZombieConfig.PopulationPeakDay": Setting(
        "int", "Which day the population peaks on", low=1, high=365, suggest=("28", "90", "180")
    ),
    "ZombieConfig.RespawnHours": Setting(
        "float",
        "Hours before a cleared cell can respawn zombies. 0 disables respawn entirely",
        low=0.0,
        high=8760.0,
        suggest=("0.0", "72.0", "168.0"),
    ),
    "ZombieConfig.RespawnMultiplier": Setting(
        "float",
        "Fraction of the original population that respawns",
        low=0.0,
        high=1.0,
        suggest=("0.0", "0.1", "0.5"),
    ),
    # --- The world around them ------------------------------------------------------------
    "WaterShut": _enum(
        "When the water goes off",
        "Instant",
        "0-30 days",
        "0-2 months",
        "0-6 months",
        "0-1 year",
        "0-5 years",
        "Never",
    ),
    "ElecShut": _enum(
        "When the power goes off",
        "Instant",
        "0-30 days",
        "0-2 months",
        "0-6 months",
        "0-1 year",
        "0-5 years",
        "Never",
    ),
    "FoodLoot": _enum("How much food is out there", *_RARITY),
    "WeaponLoot": _enum("How many weapons are out there", *_RARITY),
    "OtherLoot": _enum("How much everything else is out there", *_RARITY),
    "XpMultiplier": Setting(
        "float", "How fast skills level", low=0.0, high=10.0, suggest=("1.0", "2.0", "5.0")
    ),
    "ZombieAttractionMultiplier": Setting(
        "float",
        "How far noise carries to zombies",
        low=0.0,
        high=10.0,
        suggest=("0.5", "1.0", "2.0"),
    ),
    "HoursForCorpseRemoval": Setting(
        "int",
        "Hours before corpses despawn. 0 leaves them forever",
        low=0,
        high=8760,
        suggest=("0", "24", "216"),
    ),
    "AllClothesUnlocked": Setting("bool", "Every clothing item available at spawn"),
}

# How `/pz sandbox get` lays the settings out, in the order the in-game screen roughly
# uses. Kept separate from SETTINGS rather than as a field on each entry so that adding a
# setting and forgetting to place it is a test failure (`test_every_setting_has_a_group`)
# instead of a setting that quietly never appears in the embed.
GROUPS: dict[str, tuple[str, ...]] = {
    "Time": ("DayLength", "StartYear", "StartMonth", "StartDay", "StartTime"),
    "Infection": (
        "ZombieLore.Transmission",
        "ZombieLore.Mortality",
        "ZombieLore.Reanimate",
    ),
    "Zombies": (
        "ZombieLore.Speed",
        "ZombieLore.Strength",
        "ZombieLore.Toughness",
        "ZombieLore.Cognition",
        "ZombieLore.Memory",
        "ZombieLore.Sight",
        "ZombieLore.Hearing",
        "ZombieLore.ZombiesDragDown",
        "ZombieLore.ZombiesFenceLunge",
    ),
    "Population": (
        "Zombies",
        "ZombieConfig.PopulationMultiplier",
        "ZombieConfig.PopulationStartMultiplier",
        "ZombieConfig.PopulationPeakMultiplier",
        "ZombieConfig.PopulationPeakDay",
        "ZombieConfig.RespawnHours",
        "ZombieConfig.RespawnMultiplier",
    ),
    "World": (
        "WaterShut",
        "ElecShut",
        "FoodLoot",
        "WeaponLoot",
        "OtherLoot",
        "XpMultiplier",
        "ZombieAttractionMultiplier",
        "HoursForCorpseRemoval",
        "AllClothesUnlocked",
    ),
}


# What a value looks like once it has been through `coerce`. The remote half re-checks
# against this before writing, so a bug in the bot cannot put arbitrary text into a file
# the game will try to execute as Lua.
LITERAL = re.compile(r"^(true|false|-?\d+(\.\d+)?)$")

_ASSIGNMENT = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)(,?)(\s*(?:--.*)?)$")
_OPEN_TABLE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*$")


def coerce(path: str, value: str) -> str:
    """Turn what someone typed into the Lua literal the game expects.

    Accepts the label as readily as the number, because "Saliva only" is what an admin
    knows and `2` is what the file wants.
    """
    setting = SETTINGS.get(path)
    if setting is None:
        raise SandboxError(f"{path} is not a setting this bot can change.")

    text = value.strip()
    if setting.kind == "bool":
        if text.lower() in ("true", "yes", "on", "1"):
            return "true"
        if text.lower() in ("false", "no", "off", "0"):
            return "false"
        raise SandboxError(f"{value!r} is not true or false.")

    if setting.kind == "enum":
        wanted = text.casefold()
        for number, label in setting.options.items():
            if wanted in (str(number), label.casefold()):
                return str(number)
        raise SandboxError(f"{value!r} is not one of: " + ", ".join(setting.options.values()) + ".")

    try:
        number = float(text)
    except ValueError:
        raise SandboxError(f"{value!r} is not a number.") from None
    if not setting.low <= number <= setting.high:
        raise SandboxError(f"Must be between {setting.low:g} and {setting.high:g}.")
    if setting.kind == "int":
        if number != int(number):
            raise SandboxError(f"{value!r} must be a whole number.")
        return str(int(number))
    # Lua distinguishes nothing here, but PZ writes these as floats and a file that
    # matches what the game would have written is a file nobody has to think about.
    return f"{number:.6g}" if "." in f"{number:.6g}" else f"{number:.1f}"


def describe(path: str, literal: str) -> str:
    """A raw Lua value as a human reads it: `2 (Saliva only)`."""
    setting = SETTINGS.get(path)
    if setting is None or setting.kind != "enum":
        return literal
    try:
        label = setting.options.get(int(literal))
    except ValueError:
        return literal
    # An unknown number means this build's option list is not the one in SETTINGS. Say
    # so rather than silently dropping the label.
    return f"{literal} ({label})" if label else f"{literal} (not a known option)"


def parse(text: str) -> dict[str, str]:
    """Every scalar assignment in the file, keyed by dotted path.

    Not a Lua parser -- a scope-aware line reader, which is all the game's own writer
    ever produces. Nesting is tracked so that a key appearing in two sub-tables cannot
    be confused for one another.
    """
    values: dict[str, str] = {}
    scope: list[str] = []
    for raw in text.splitlines():
        line = raw.split("--", 1)[0].rstrip() if not raw.strip().startswith("--") else ""
        if not line.strip():
            continue

        opened = _OPEN_TABLE.match(line)
        if opened:
            # The outermost `SandboxVars = {` is the document, not a scope.
            scope.append("" if not scope and opened.group(1) == "SandboxVars" else opened.group(1))
            continue
        if line.strip().startswith("}"):
            if scope:
                scope.pop()
            continue

        assignment = _ASSIGNMENT.match(line)
        if assignment and assignment.group(3).strip() != "{":
            prefix = ".".join(part for part in scope if part)
            key = assignment.group(2)
            values[f"{prefix}.{key}" if prefix else key] = assignment.group(3).strip()
    return values


def rewrite(text: str, path: str, literal: str) -> str:
    """Replace exactly one value, leaving every other byte of the file alone.

    A key that is not in the file is an error rather than an append: the game writes
    every option it knows about, so a missing one means this build does not have that
    setting, and adding it would at best do nothing.
    """
    if not LITERAL.fullmatch(literal):
        raise SandboxError(f"{literal!r} is not a plain Lua scalar; refusing to write it.")

    wanted_scope, _, wanted_key = path.rpartition(".")
    out: list[str] = []
    scope: list[str] = []
    replaced = False

    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        ending = raw[len(line) :]
        stripped = line.split("--", 1)[0] if not line.strip().startswith("--") else ""

        opened = _OPEN_TABLE.match(stripped)
        if opened:
            scope.append("" if not scope and opened.group(1) == "SandboxVars" else opened.group(1))
            out.append(raw)
            continue
        if stripped.strip().startswith("}"):
            if scope:
                scope.pop()
            out.append(raw)
            continue

        assignment = _ASSIGNMENT.match(stripped)
        here = ".".join(part for part in scope if part)
        if (
            assignment
            and not replaced
            and assignment.group(2) == wanted_key
            and here == wanted_scope
        ):
            indent, key, _, comma, trailing = assignment.groups()
            out.append(f"{indent}{key} = {literal}{comma}{trailing}{ending}")
            replaced = True
            continue
        out.append(raw)

    if not replaced:
        raise SandboxError(
            f"{path} is not in this world's SandboxVars.lua. Either the setting does not "
            "exist in this build of the game, or the file is not the one the server reads."
        )
    return "".join(out)


# --- The half that runs on the game server -------------------------------------------------


def _read(target: str) -> str:
    try:
        with open(target, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        raise SandboxError(
            f"{target} does not exist. The world has not been generated yet -- start the "
            "server once and let it finish loading."
        ) from None


def _write(target: str, text: str) -> None:
    """Write in place, atomically, preserving ownership, keeping one backup.

    Ownership matters: this runs as root over SSM, and a SandboxVars.lua owned by root
    is one the `pzuser` service account cannot rewrite when an admin changes an option
    from inside the game.
    """
    stat = os.stat(target)
    shutil.copy2(target, target + ".pzbot.bak")
    tmp = target + ".pzbot.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, stat.st_mode)
    os.chown(tmp, stat.st_uid, stat.st_gid)
    os.replace(tmp, target)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: sandbox.py get <file> | set <file> <path> <literal>", file=sys.stderr)
        return 2

    action, target = argv[1], argv[2]
    try:
        text = _read(target)
        if action == "get":
            print(json.dumps(parse(text)))
            return 0
        if action == "set":
            path, literal = argv[3], argv[4]
            before = parse(text).get(path, "(absent)")
            _write(target, rewrite(text, path, literal))
            print(json.dumps({"setting": path, "was": before, "now": literal}))
            return 0
    except SandboxError as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        return 1
    print(f"sandbox: unknown action {action!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
