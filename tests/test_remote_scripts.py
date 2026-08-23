"""The two editors that run on the game server, driven through the real command shape.

`GameServer.run_python` builds `echo <base64> | base64 -d | python3 - <argv>` and hands it
to SSM. These tests run that exact string through a shell, so a quoting or encoding
mistake fails here rather than on the box, where the only symptom would be a server that
will not boot.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import sys

import pytest

from pzbot import sandbox
from pzbot.server import _INI_SCRIPT, _SANDBOX_SOURCE

SANDBOX_FILE = """SandboxVars = {
    VERSION = 5,
    Zombies = 3,
    DayLength = 3,
    ZombieLore = {
        Transmission = 1,
        Mortality = 5,
    },
    ZombieConfig = {
        PopulationMultiplier = 1.0,
    },
}
"""


def ship(source: str, args: list[str]) -> subprocess.CompletedProcess:
    """Byte-for-byte what `run_python` sends to AWS-RunShellScript."""
    payload = base64.b64encode(source.encode("utf-8")).decode("ascii")
    argv = " ".join(shlex.quote(arg) for arg in args)
    command = f"echo {payload} | base64 -d | {shlex.quote(sys.executable)} - {argv}"
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


def test_the_ini_editor_changes_one_key_and_leaves_the_rest(tmp_path):
    ini = tmp_path / "pzprod.ini"
    ini.write_text("PVP=false\nMaxPlayers=16\nPublicName=pzprod\n")

    result = ship(_INI_SCRIPT, [str(ini), "PVP", "true"])
    assert result.returncode == 0, result.stderr
    assert ini.read_text() == "PVP=true\nMaxPlayers=16\nPublicName=pzprod\n"


def test_the_ini_editor_survives_a_value_full_of_shell_metacharacters(tmp_path):
    # PublicDescription is free text. `$(…)`, backticks and quotes in it must land in the
    # file as characters, not run as shell.
    ini = tmp_path / "pzprod.ini"
    ini.write_text("PublicDescription=old\n")
    hostile = "$(reboot) `whoami` 'quoted' \"double\" & | ;"

    result = ship(_INI_SCRIPT, [str(ini), "PublicDescription", hostile])
    assert result.returncode == 0, result.stderr
    assert ini.read_text() == f"PublicDescription={hostile}\n"


def test_the_sandbox_editor_sets_infection_to_saliva_only(tmp_path):
    lua = tmp_path / "pzprod_SandboxVars.lua"
    lua.write_text(SANDBOX_FILE)

    result = ship(_SANDBOX_SOURCE, ["set", str(lua), "ZombieLore.Transmission", "2"])
    assert result.returncode == 0, result.stderr
    assert '"was": "1"' in result.stdout
    assert sandbox.parse(lua.read_text())["ZombieLore.Transmission"] == "2"
    # Every other line is untouched.
    assert lua.read_text().replace("Transmission = 2", "Transmission = 1") == SANDBOX_FILE


def test_the_sandbox_editor_reads_the_whole_file_back_as_json(tmp_path):
    import json

    lua = tmp_path / "pzprod_SandboxVars.lua"
    lua.write_text(SANDBOX_FILE)

    result = ship(_SANDBOX_SOURCE, ["get", str(lua)])
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["DayLength"] == "3"
    assert values["ZombieConfig.PopulationMultiplier"] == "1.0"


@pytest.mark.parametrize(
    "hostile",
    ['1, os.execute("rm -rf /")', "dofile('/etc/passwd')", "$(reboot)"],
)
def test_the_sandbox_editor_refuses_anything_that_is_not_a_scalar(tmp_path, hostile):
    lua = tmp_path / "pzprod_SandboxVars.lua"
    lua.write_text(SANDBOX_FILE)

    result = ship(_SANDBOX_SOURCE, ["set", str(lua), "DayLength", hostile])
    assert result.returncode == 1
    assert "plain Lua scalar" in result.stderr
    assert lua.read_text() == SANDBOX_FILE
