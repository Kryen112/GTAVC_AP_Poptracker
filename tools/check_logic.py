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
    the percentage item the autotracker draws on is declared and named
    the autotracker draws the percentage it is given, and only its own slot's
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
# PopTracker's own AccessibilityLevel values, read off its Lua API definition.
# The numbers are not contiguous and Partial sits BELOW Inspect, so they are
# declared once here and both the Lua stub and the validation below are built
# from this rather than each carrying a copy. An earlier version had two copies
# that disagreed, which left the checker validating against a numbering
# PopTracker does not use.
ACCESSIBILITY_LEVELS = {
    "None": 0, "Partial": 1, "Inspect": 3, "SequenceBreak": 5,
    "Normal": 6, "Cleared": 7,
}
_LEVEL_LINE = ", ".join(
    f"{name} = {value}" for name, value in ACCESSIBILITY_LEVELS.items())

STUB = """
AccessibilityLevel = {
    -- LEVELS --
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
# The PopTracker API the autotracker layer touches, for the one thing in it that
# is hand-written rather than generated: the completion percentage. Everything
# else archipelago.lua does is driven by the generated mapping tables, which the
# checks below read directly. FindObjectForCode answers the percentage item alone
# and records what was drawn on it.
AUTOTRACKER_STUB = """
OVERLAY = ""
ACTIVE = nil
GETS = {}
NOTIFIES = {}

Tracker = {}
function Tracker:FindObjectForCode(code)
    if code ~= PERCENTAGE_CODE then return nil end
    return {
        Active = false,
        SetOverlay = function(self, text) OVERLAY = text; ACTIVE = self.Active end,
    }
end

ScriptHost = {}
function ScriptHost:LoadScript(path) LOAD(path) end

Archipelago = { PlayerNumber = 3, TeamNumber = 0 }
function Archipelago:Get(keys) for _, key in ipairs(keys) do GETS[key] = true end end
function Archipelago:SetNotify(keys)
    for _, key in ipairs(keys) do NOTIFIES[key] = true end
end
function Archipelago:AddClearHandler(_name, _handler) end
function Archipelago:AddItemHandler(_name, _handler) end
function Archipelago:AddLocationHandler(_name, _handler) end
function Archipelago:AddRetrievedHandler(_name, _handler) end
function Archipelago:AddSetReplyHandler(_name, _handler) end

AUTOTRACKER_ENABLE_DEBUG_LOGGING_AP = false
Highlight = nil
"""

# The settings a generated rule branches on, so every combination of them is run.
# A rule that carries both (the finale's last mission) has four expressions, and
# the mixed corners are only reached by toggling them apart.
# Every setting a rule switches on. The rules are called once per combination,
# so a branch left out here is a branch no run ever enters: one that raises, or
# returns something that is not an accessibility level, would never be found.
#
# It does NOT make the item codes in those branches visible. lockTerm returns
# early when its own setting is off, so the item it names is never asked for, and
# no combination here turns a lock on. Reading the source is what covers that,
# in rule_item_codes below.
#
# The two content stages give all three granularities between them, since off is
# what no stage code means, and both at once reads as per_district.
SWITCH_SETTINGS = ("enable_properties_on", "split_mainland_access_on",
                   "split_content_locks_per_district",
                   "split_content_locks_per_class")


def load_runtime() -> lupa.LuaRuntime:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(STUB.replace("-- LEVELS --", _LEVEL_LINE))
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
    levels = set(ACCESSIBILITY_LEVELS.values())
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
    # Read out of the locations rather than listed here. A list goes stale
    # silently: it named eight helpers and the pickups class had made a ninth,
    # which nothing checked. Sections carry their own rules as well as nodes,
    # since a marker standing for two classes hides each half with its own.
    for helper in sorted(visibility_helpers()):
        function = globals_table[helper]
        if function is None:
            problems.append(f"visibility helper {helper} is missing, and a node "
                            "or section names it")
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


def visibility_helpers() -> set[str]:
    """Every helper named by a visibility rule anywhere in the locations.

    Rules are OR-ed across entries and AND-ed within a comma separated entry, so
    an entry is split before the leading $ is stripped. A helper that does not
    exist makes its node or section permanently invisible, which is silent in the
    pack and only shows in game, so the names are collected from the files rather
    than written down twice.
    """
    helpers: set[str] = set()

    def walk(nodes) -> None:
        for node in nodes:
            for holder in [node, *node.get("sections", [])]:
                for entry in holder.get("visibility_rules", []) or []:
                    for rule in str(entry).split(","):
                        name = rule.strip()
                        if name.startswith("$"):
                            helpers.add(name[1:])
            if "children" in node:
                walk(node["children"])

    for path in sorted((PACK / "locations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        walk(data if isinstance(data, list) else [data])
    if not helpers:
        # The reader is the only thing that looks at these, so finding none means
        # the files changed shape rather than that nothing is gated.
        raise SystemExit("no visibility rules found in locations, so nothing was "
                         "checked")
    return helpers


def declared_item_codes() -> set[str]:
    codes: set[str] = set()
    for entry in json.loads((PACK / "items" / "items.json").read_text(encoding="utf-8")):
        for field in (entry.get("codes"), *(stage.get("codes")
                                            for stage in entry.get("stages", []))):
            if field:
                codes.update(part.strip() for part in field.split(","))
    return codes


def rule_item_codes(problems: list[str], item_codes: set[str]) -> None:
    """Every item any rule names, read out of the source rather than run.

    Running the rules cannot see these. A lock term returns early when its own
    setting is off, so the item it names is never requested, and a rule carries
    one version per granularity of which a run enters exactly one. So an item
    name that nothing provides is invisible to every dynamic check, which is how
    the content locks came to name Hidden Packages in a seed whose item was
    called Ocean Beach Hidden Packages: the rule was unsatisfiable and every gate
    read clean.

    Read as a whole file rather than per rule, since the codes are what matter
    and a name is spelled the same wherever it appears.
    """
    source = (PACK / "scripts" / "logic" / "access_rules.lua").read_text(
        encoding="utf-8")
    named: set[str] = set()
    for pattern in (r'\blockTerm\("((?:[^"\\]|\\.)*)"',
                    r'\bhas\("((?:[^"\\]|\\.)*)"\)',
                    r'\bitemAtLeast\("((?:[^"\\]|\\.)*)"'):
        named.update(lua_unescaped(literal)
                     for literal in re.findall(pattern, source))
    problems.extend(
        f"a rule names item code {code!r}, which items.json does not declare"
        for code in sorted(named - item_codes))
    if not named:
        # The patterns above are the only reader of these, so a rename that made
        # them all miss would report a clean file rather than nothing checked.
        problems.append("no rule names any item, so access_rules.lua either has "
                        "no requirements or is no longer being read")


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


def check_percentage_item(problems: list[str], item_codes: set[str]) -> None:
    """The completion percentage is not an AP item, so nothing above reaches it:
    the generator emits its code and its data store key into setting_mapping.lua
    and archipelago.lua reads both by name. A generator that stopped emitting
    them would leave the hand-written half concatenating nil, which takes down
    autotracking as a whole, so the two names are checked here."""
    generated = (PACK / "scripts" / "autotracking" / "setting_mapping.lua").read_text(
        encoding="utf-8")
    code = re.search(r'PERCENTAGE_CODE = "([^"]+)"', generated)
    if code is None:
        problems.append("setting_mapping.lua declares no PERCENTAGE_CODE, which "
                        "archipelago.lua looks the percentage item up by")
    elif code.group(1) not in item_codes:
        problems.append(f"PERCENTAGE_CODE is {code.group(1)!r}, which items.json "
                        "does not declare")
    if not re.search(r'PERCENTAGE_KEY_PREFIX = "[^"]+"', generated):
        problems.append("setting_mapping.lua declares no PERCENTAGE_KEY_PREFIX, "
                        "which archipelago.lua builds its data store key from")


def check_autotracker_percentage(problems: list[str]) -> None:
    """Run the autotracker's percentage path against a stub PopTracker.

    The number is the one thing the tracker shows that no AP item or location
    carries, so nothing else here reaches it: it arrives on a data store key, is
    dispatched by hand-written Lua, and is drawn as overlay text. Cheap to run
    for real, and a wrong key or a nil the dispatcher cannot take would show up
    in game as silence.
    """
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.globals()["LOAD"] = lambda path: runtime.execute(
        (PACK / path).read_text(encoding="utf-8"))
    runtime.execute(AUTOTRACKER_STUB)
    try:
        runtime.execute((PACK / "scripts" / "autotracking" / "archipelago.lua")
                        .read_text(encoding="utf-8"))
        lua = runtime.globals()
        lua["onClear"](runtime.table())
    except lupa.LuaError as error:
        problems.append(f"archipelago.lua failed to clear: {error}")
        return

    key = lua["PERCENTAGE_KEY"]
    expected = f"{lua['PERCENTAGE_KEY_PREFIX']}0_3"
    if key != expected:
        problems.append(f"the percentage key is {key!r}, not {expected!r}")
    if not (lua["GETS"][key] and lua["NOTIFIES"][key]):
        problems.append("onClear does not subscribe to the percentage key")
    if lua["OVERLAY"] != "":
        problems.append(f"a fresh seed draws {lua['OVERLAY']!r} on the percentage")

    # Whole and float numbers alike (JSON carries either), the unset key a Get
    # answers with before the mod has reported, and, since the key carries no
    # read-only prefix and anything in the room can Set it, the values that would
    # raise inside the handler if they reached the format: out of range, an
    # infinity, a not-a-number, and something that is not a number at all.
    drawn = [(0, "0%", False), (93, "93%", False), (93.0, "93%", False),
             (100, "100%", True), (100.0, "100%", True), (None, "", False),
             ("93", "93%", False), (-5, "0%", False), (150, "100%", True),
             (1e19, "100%", True), (float("inf"), "100%", True),
             (float("-inf"), "0%", False), (float("nan"), "0%", False),
             ("not a number", "", False), (True, "", False)]
    for value, overlay, active in drawn:
        try:
            lua["onDataStorageUpdate"](key, value, None)
        except lupa.LuaError as error:
            problems.append(f"the percentage {value!r} raised: {error}")
            continue
        if lua["OVERLAY"] != overlay:
            problems.append(
                f"the percentage {value!r} drew {lua['OVERLAY']!r}, not {overlay!r}")
        if lua["ACTIVE"] != active:
            problems.append(
                f"the percentage {value!r} left the icon active={lua['ACTIVE']}")
    lua["onDataStorageUpdate"](key, 42, None)
    lua["onDataStorageUpdate"](f"{lua['PERCENTAGE_KEY_PREFIX']}0_9", 77, None)
    if lua["OVERLAY"] != "42%":
        problems.append(f"another slot's percentage key drew {lua['OVERLAY']!r}")


def check_accessibility_levels(problems: list[str]) -> None:
    """Pins ACCESSIBILITY_LEVELS against PopTracker's own Lua API definition.

    The values are PopTracker's, not the pack's, so nothing inside the pack can
    prove them. PopTracker ships the definition it generates its Lua API from,
    and it sits beside this repository the way the Archipelago checkout sits
    beside the world's, so when it is there the numbers are checked rather than
    trusted. Absent, this says so and moves on rather than failing: the pack has
    to build on a machine with no PopTracker install.
    """
    definition = PACK.parent / "Poptracker" / "api" / "lua" / "definition" / "poptracker.lua"
    if not definition.is_file():
        print("  accessibility levels: no PopTracker install beside the pack, "
              "so the values are taken on trust")
        return
    text = definition.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"AccessibilityLevel\s*=\s*\{(.*?)\}", text, re.S)
    if block is None:
        problems.append(
            f"{definition} has no AccessibilityLevel table, so the pack's copy "
            "of the values cannot be checked")
        return
    theirs = {name: int(value)
              for name, value in re.findall(r"(\w+)\s*=\s*(\d+)", block.group(1))}
    if theirs != ACCESSIBILITY_LEVELS:
        problems.append(
            f"ACCESSIBILITY_LEVELS is {ACCESSIBILITY_LEVELS} but PopTracker's own "
            f"definition says {theirs}; take theirs")
        return
    print(f"  accessibility levels: match PopTracker's own definition, "
          f"{len(theirs)} of them")


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
    rule_item_codes(problems, item_codes)
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

    check_percentage_item(problems, item_codes)
    check_autotracker_percentage(problems)

    check_accessibility_levels(problems)

    pins = check_pins(problems)

    for problem in problems:
        print(f"FAIL {problem}")
    print(f"rules {len(declared_rules)}, sections {len(paths)}, "
          f"location ids {len(locations)}, item ids {len(items)}, pins {pins}")
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
