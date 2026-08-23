"""Sandbox option parsing and rewriting, against a file shaped like the game's own.

The invariant these tests exist for: a `set` changes exactly one value and leaves every
other byte alone. This file is executed as Lua by the game at startup, so a rewrite that
mangles it does not fail loudly -- it fails as a server that will not boot, at the moment
somebody was trying to play.
"""

from __future__ import annotations

import pytest

from pzbot import sandbox
from pzbot.sandbox import SandboxError

# Trimmed from a real SandboxVars.lua: the top-level scalars, two nested tables, a
# comment, and a same-named key in two different scopes.
SAMPLE = """SandboxVars = {
    VERSION = 5,
    Zombies = 3,
    Distribution = 1,
    DayLength = 3,
    StartYear = 1,
    StartMonth = 7,
    StartDay = 9,
    StartTime = 2,
    WaterShut = 2,
    ElecShut = 2,
    XpMultiplier = 1.0,
    ZombieAttractionMultiplier = 1.0,
    AllClothesUnlocked = false,
    HoursForCorpseRemoval = 216,
    -- everything below is grouped by the in-game screen
    ZombieLore = {
        Speed = 2,
        Strength = 2,
        Toughness = 2,
        Transmission = 1,
        Mortality = 5,
        Reanimate = 3,
        Cognition = 3,
        Memory = 2,
        Sight = 2,
        Hearing = 2,
        ZombiesDragDown = true,
        ZombiesFenceLunge = true,
    },
    ZombieConfig = {
        PopulationMultiplier = 1.0,
        PopulationStartMultiplier = 1.0,
        PopulationPeakMultiplier = 1.5,
        PopulationPeakDay = 28,
        RespawnHours = 72.0,
        RespawnUnseenHours = 16.0,
        RespawnMultiplier = 0.1,
        Memory = 999,
    },
}
"""


def test_parse_finds_scalars_at_every_depth():
    values = sandbox.parse(SAMPLE)
    assert values["DayLength"] == "3"
    assert values["ZombieLore.Transmission"] == "1"
    assert values["ZombieConfig.PopulationMultiplier"] == "1.0"
    assert values["ZombieLore.ZombiesDragDown"] == "true"


def test_parse_keeps_same_named_keys_in_different_tables_apart():
    # `Memory` is a zombie attribute in one table and a number in the other. Confusing
    # them would write the wrong value into the wrong scope.
    values = sandbox.parse(SAMPLE)
    assert values["ZombieLore.Memory"] == "2"
    assert values["ZombieConfig.Memory"] == "999"


def test_parse_ignores_comments_and_the_outer_table_name():
    values = sandbox.parse(SAMPLE)
    assert not any(key.startswith("SandboxVars") for key in values)
    assert "VERSION" in values


# --- Rewriting -----------------------------------------------------------------------------


def test_setting_infection_to_saliva_only_changes_exactly_one_line():
    after = sandbox.rewrite(SAMPLE, "ZombieLore.Transmission", "2")
    before_lines = SAMPLE.splitlines()
    after_lines = after.splitlines()
    changed = [(a, b) for a, b in zip(before_lines, after_lines, strict=True) if a != b]
    assert changed == [("        Transmission = 1,", "        Transmission = 2,")]
    assert sandbox.parse(after)["ZombieLore.Transmission"] == "2"


def test_rewriting_preserves_indentation_commas_and_the_trailing_newline():
    after = sandbox.rewrite(SAMPLE, "ZombieConfig.PopulationMultiplier", "0.5")
    assert "        PopulationMultiplier = 0.5," in after
    assert after.endswith("}\n")
    assert len(after.splitlines()) == len(SAMPLE.splitlines())


def test_rewriting_a_top_level_key_does_not_touch_the_nested_one():
    after = sandbox.rewrite(SAMPLE, "ZombieConfig.Memory", "5")
    values = sandbox.parse(after)
    assert values["ZombieConfig.Memory"] == "5"
    assert values["ZombieLore.Memory"] == "2"


def test_a_key_that_is_not_in_the_file_is_refused_not_appended():
    with pytest.raises(SandboxError, match="not in this world"):
        sandbox.rewrite(SAMPLE, "ZombieLore.NotAThing", "1")


def test_a_value_that_is_not_a_scalar_never_reaches_the_file():
    # The file is executed as Lua by the game. Nothing that is not a bare scalar may go
    # into it, however it got this far.
    for hostile in ('1, os.execute("rm -rf /")', "dofile('/etc/passwd')", "1;2"):
        with pytest.raises(SandboxError, match="plain Lua scalar"):
            sandbox.rewrite(SAMPLE, "DayLength", hostile)


# --- Coercion ------------------------------------------------------------------------------


def test_a_label_is_as_good_as_a_number():
    assert sandbox.coerce("ZombieLore.Transmission", "Saliva only") == "2"
    assert sandbox.coerce("ZombieLore.Transmission", "saliva ONLY") == "2"
    assert sandbox.coerce("ZombieLore.Transmission", "2") == "2"


def test_an_unknown_label_lists_the_real_ones():
    with pytest.raises(SandboxError, match="Blood \\+ Saliva"):
        sandbox.coerce("ZombieLore.Transmission", "spit")


def test_enum_bounds_are_the_option_list():
    with pytest.raises(SandboxError):
        sandbox.coerce("ZombieLore.Transmission", "4")


def test_numbers_are_range_checked():
    assert sandbox.coerce("ZombieConfig.PopulationMultiplier", "0.5") == "0.5"
    with pytest.raises(SandboxError, match="between 0 and 4"):
        sandbox.coerce("ZombieConfig.PopulationMultiplier", "10")
    with pytest.raises(SandboxError, match="not a number"):
        sandbox.coerce("ZombieConfig.PopulationMultiplier", "lots")


def test_ints_refuse_fractions():
    assert sandbox.coerce("ZombieConfig.PopulationPeakDay", "28") == "28"
    with pytest.raises(SandboxError, match="whole number"):
        sandbox.coerce("ZombieConfig.PopulationPeakDay", "28.5")


def test_floats_are_written_the_way_the_game_writes_them():
    # `1` in a float field would still load, but a file that matches what the game would
    # have written is a file nobody has to squint at.
    assert sandbox.coerce("ZombieConfig.PopulationMultiplier", "1") == "1.0"
    assert sandbox.coerce("XpMultiplier", "2") == "2.0"


def test_bools_take_english():
    assert sandbox.coerce("ZombieLore.ZombiesDragDown", "no") == "false"
    assert sandbox.coerce("AllClothesUnlocked", "yes") == "true"


def test_an_unknown_setting_is_refused_before_anything_else_happens():
    with pytest.raises(SandboxError, match="not a setting"):
        sandbox.coerce("ZombieLore.RCONPassword", "1")


# --- Presentation ---------------------------------------------------------------------------


def test_describe_labels_enums_and_leaves_numbers_alone():
    assert sandbox.describe("ZombieLore.Transmission", "2") == "2 (Saliva only)"
    assert sandbox.describe("ZombieConfig.PopulationMultiplier", "1.0") == "1.0"


def test_describe_says_so_when_the_game_disagrees_with_our_option_list():
    # If a build reorders or extends an option list, this is how it becomes visible
    # instead of quietly mislabelling the value.
    assert sandbox.describe("ZombieLore.Transmission", "9") == "9 (not a known option)"


# --- The half that runs on the game server ---------------------------------------------------


def test_get_and_set_round_trip_through_a_real_file(tmp_path, capsys):
    import json

    target = tmp_path / "pzprod_SandboxVars.lua"
    target.write_text(SAMPLE)

    assert sandbox.main(["sandbox.py", "get", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ZombieLore.Transmission"] == "1"

    argv = ["sandbox.py", "set", str(target), "ZombieLore.Transmission", "2"]
    assert sandbox.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {
        "setting": "ZombieLore.Transmission",
        "was": "1",
        "now": "2",
    }
    assert sandbox.parse(target.read_text())["ZombieLore.Transmission"] == "2"
    # The previous file is kept, because this one is executed by the game at startup.
    assert (
        sandbox.parse((tmp_path / "pzprod_SandboxVars.lua.pzbot.bak").read_text())[
            "ZombieLore.Transmission"
        ]
        == "1"
    )


def test_a_missing_file_explains_itself(tmp_path, capsys):
    assert sandbox.main(["sandbox.py", "get", str(tmp_path / "nope.lua")]) == 1
    assert "has not been generated" in capsys.readouterr().err


def test_every_setting_has_a_group_and_every_group_entry_is_a_setting():
    # Drift in either direction is a setting that cannot be found in `/pz sandbox get`,
    # or a group entry that renders as a blank line.
    grouped = [path for paths in sandbox.GROUPS.values() for path in paths]
    assert sorted(grouped) == sorted(sandbox.SETTINGS)
    assert len(grouped) == len(set(grouped)), "a setting is listed in two groups"


def test_the_three_settings_this_command_was_built_for_are_present():
    assert sandbox.SETTINGS["DayLength"].kind == "enum"
    assert sandbox.SETTINGS["ZombieConfig.PopulationMultiplier"].kind == "float"
    assert "Saliva only" in sandbox.SETTINGS["ZombieLore.Transmission"].options.values()
