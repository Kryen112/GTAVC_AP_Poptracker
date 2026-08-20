"""Self-test the generated pack without launching PopTracker.

The pack is mostly generated, so the failures worth catching are the ones that
cross file boundaries: a rule naming a helper that does not exist, a section the
autotracker points at by a path nothing answers to, an item code no item
declares, a pin outside the map. PopTracker reports these one at a time at
runtime; this reports all of them at once.

Checks run:
    every Lua file parses
    every rule function runs against a stub tracker and returns a level
    every location section has a rule, and every rule has a section
    every item code a rule tests is declared in items.json
    every location id the world knows maps to exactly one section path
    every pin sits inside the map image

Usage:
    py -3.12 tools/check_logic.py

Needs lupa (a Lua runtime) and Pillow for the image bounds check.
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path

import lupa

PACK = Path(__file__).resolve().parent.parent

# The PopTracker API the logic layer touches. ProviderCountForCode answers zero
# for everything, which is the state a fresh seed starts in, so a rule that runs
# clean here runs clean at connect. CODES_ON names codes to answer one for
# instead, which is how the rules are run for each setting a rule can switch on:
# with everything zero only one branch of each switch ever executes.
#
# FindObjectForCode answers a section with its chest still available, the state
# of a mission not yet passed, which is what missionPassed reads. REQUESTED_PATHS
# records what was asked for, so a rule naming a section the pack does not have
# is caught here rather than in PopTracker.
STUB = """
AccessibilityLevel = {
    None = 0, Inspect = 1, Partial = 2, SequenceBreak = 3, Normal = 4, Cleared = 5,
}
REQUESTED_CODES = {}
REQUESTED_PATHS = {}
CODES_ON = {}
Tracker = {}
function Tracker:ProviderCountForCode(code)
    REQUESTED_CODES[code] = true
    if CODES_ON[code] then return 1 end
    return 0
end
function Tracker:FindObjectForCode(path)
    REQUESTED_PATHS[path] = true
    return { AvailableChestCount = 1, ChestCount = 1 }
end
PopVersion = "0.31.0"
"""

# The settings a generated rule branches on, so every combination of them is run.
# A rule that carries both (the finale's last mission) has four expressions, and
# the mixed corners are only reached by toggling them apart.
SWITCH_SETTINGS = ("enable_properties_on", "split_mainland_access_on")


def load_runtime() -> lupa.LuaRuntime:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(STUB)
    for name in ("scripts/logic/logic.lua", "scripts/logic/access_rules.lua"):
        source = (PACK / name).read_text(encoding="utf-8")
        try:
            runtime.execute(source)
        except lupa.LuaError as error:
            raise SystemExit(f"{name} failed to load: {error}") from error
    return runtime


def lua_files_parse(problems: list[str]) -> None:
    runtime = lupa.LuaRuntime()
    for path in sorted(PACK.glob("scripts/**/*.lua")):
        try:
            runtime.compile(path.read_text(encoding="utf-8"))
        except lupa.LuaSyntaxError as error:
            problems.append(f"{path.relative_to(PACK)} does not parse: {error}")


def rule_names() -> list[str]:
    source = (PACK / "scripts" / "logic" / "access_rules.lua").read_text(encoding="utf-8")
    return re.findall(r"^function (rule_\w+)\(\)", source, flags=re.MULTILINE)


def rules_run(runtime: lupa.LuaRuntime, problems: list[str],
              codes_on: tuple[str, ...] = ()) -> set[str]:
    """Call every rule. Returns the item codes the rules asked about."""
    globals_table = runtime.globals()
    on = runtime.table()
    for code in codes_on:
        on[code] = True
    globals_table["CODES_ON"] = on
    levels = {0, 1, 2, 3, 4, 5}
    for name in rule_names():
        function = globals_table[name]
        if function is None:
            problems.append(f"{name} is declared but not callable")
            continue
        try:
            result = function()
        except lupa.LuaError as error:
            problems.append(f"{name} raised: {error}")
            continue
        if result not in levels:
            problems.append(f"{name} returned {result!r}, not an AccessibilityLevel")
    for helper in ("visStoryMissions", "visProperties", "visHiddenPackages",
                   "visRampages", "visStuntJumps", "visRobbableStores",
                   "visSideEvents", "visEmergencyVehicles"):
        function = globals_table[helper]
        if function is None:
            problems.append(f"visibility helper {helper} is missing")
            continue
        try:
            function()
        except lupa.LuaError as error:
            problems.append(f"{helper} raised: {error}")
    requested = globals_table["REQUESTED_CODES"]
    paths = globals_table["REQUESTED_PATHS"]
    return set(requested.keys()), set(paths.keys())


def location_files() -> dict[str, list]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PACK.glob("locations/*.json"))
    }


def section_paths_and_rules(problems: list[str]) -> tuple[set[str], set[str]]:
    """Every "@group/node/section" path the locations declare, and every rule
    they reference."""
    paths: set[str] = set()
    referenced: set[str] = set()
    for name, payload in location_files().items():
        for group in payload:
            if group["name"] != name:
                problems.append(
                    f"locations/{name}.json declares group {group['name']!r}, "
                    f"so its section paths would not match the file")
            for node in group.get("children", []):
                for section in node.get("sections", []):
                    paths.add(f"@{group['name']}/{node['name']}/{section['name']}")
                    for rule in section.get("access_rules", []):
                        referenced.add(rule.lstrip("^$"))
    return paths, referenced


def declared_item_codes() -> set[str]:
    codes: set[str] = set()
    for entry in json.loads((PACK / "items" / "items.json").read_text(encoding="utf-8")):
        for field in (entry.get("codes"), *(stage.get("codes")
                                            for stage in entry.get("stages", []))):
            if field:
                codes.update(part.strip() for part in field.split(","))
    return codes


def lua_unescaped(literal: str) -> str:
    """The string a Lua literal denotes, not the source that spells it.

    One location name carries double quotes, so the generator escapes them and a
    literal read straight out of the source keeps the backslashes. Comparing that
    against a JSON value, which its own reader has already decoded, reports a
    mismatch where the two agree.
    """
    out: list[str] = []
    index = 0
    while index < len(literal):
        if literal[index] == "\\" and index + 1 < len(literal):
            out.append(literal[index + 1])
            index += 2
        else:
            out.append(literal[index])
            index += 1
    return "".join(out)


def mapping_table(name: str) -> dict[int, list[str]]:
    """Read one of the generated Lua mapping tables without running it."""
    source = (PACK / "scripts" / "autotracking" / f"{name}.lua").read_text(encoding="utf-8")
    table: dict[int, list[str]] = {}
    for key, body in re.findall(r"\[(\d+)\] = \{(.*?)\},", source):
        table[int(key)] = [lua_unescaped(literal) for literal
                           in re.findall(r'"((?:[^"\\]|\\.)*)"', body)]
    return table


def check_pins(problems: list[str]) -> int:
    maps = json.loads((PACK / "maps" / "maps.json").read_text(encoding="utf-8"))
    sizes: dict[str, tuple[int, int]] = {}
    for entry in maps:
        image = PACK / entry["img"]
        if not image.is_file():
            problems.append(f"map {entry['name']!r} image is missing: {entry['img']}"
                            " (run tools/extract_map.py)")
            continue
        from PIL import Image
        with Image.open(image) as handle:
            sizes[entry["name"]] = handle.size
    pins = 0
    for name, payload in location_files().items():
        for group in payload:
            for node in group.get("children", []):
                for pin in node.get("map_locations", []):
                    pins += 1
                    size = sizes.get(pin["map"])
                    if size is None:
                        problems.append(
                            f"{name}: node {node['name']!r} pins on unknown map "
                            f"{pin['map']!r}")
                        continue
                    if not (0 <= pin["x"] < size[0] and 0 <= pin["y"] < size[1]):
                        problems.append(
                            f"{name}: node {node['name']!r} pin "
                            f"({pin['x']}, {pin['y']}) is outside {pin['map']!r}")
    return pins


def main() -> int:
    problems: list[str] = []
    lua_files_parse(problems)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1

    runtime = load_runtime()
    requested_codes, requested_paths = rules_run(runtime, problems)
    # Again for every combination of the settings a rule switches on, so each
    # expression a rule carries is evaluated at least once, mixed corners
    # included.
    for combination in itertools.product(*[(None, code) for code in SWITCH_SETTINGS]):
        codes_on = tuple(code for code in combination if code)
        if codes_on:
            more_codes, more_paths = rules_run(runtime, problems, codes_on)
            requested_codes |= more_codes
            requested_paths |= more_paths
    paths, referenced_rules = section_paths_and_rules(problems)
    declared_rules = set(rule_names())

    problems.extend(
        f"a section references {rule}, which access_rules.lua does not define"
        for rule in sorted(referenced_rules - declared_rules))
    problems.extend(
        f"access_rules.lua defines {rule}, which no section references"
        for rule in sorted(declared_rules - referenced_rules))

    item_codes = declared_item_codes()
    problems.extend(
        f"a rule tests item code {code!r}, which items.json does not declare"
        for code in sorted(requested_codes - item_codes))
    # A missionPassed term names a section rather than an item, so the path it
    # asks for has to be one the locations declare.
    problems.extend(
        f"a rule reads section {path!r}, which no location declares"
        for path in sorted(requested_paths - paths))

    locations = mapping_table("location_mapping")
    problems.extend(
        f"location id {location_id} maps to {code!r}, which no section declares"
        for location_id, codes in sorted(locations.items())
        for code in codes if code not in paths)
    mapped_paths = {code for codes in locations.values() for code in codes}
    problems.extend(
        f"section {path!r} has no location id mapped to it"
        for path in sorted(paths - mapped_paths))

    items = mapping_table("item_mapping")
    for item_id, entry in sorted(items.items()):
        if entry and entry[0] not in item_codes:
            problems.append(
                f"item id {item_id} maps to {entry[0]!r}, which items.json does not declare")

    pins = check_pins(problems)

    for problem in problems:
        print(f"FAIL {problem}")
    print(f"rules {len(declared_rules)}, sections {len(paths)}, "
          f"location ids {len(locations)}, item ids {len(items)}, pins {pins}")
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
