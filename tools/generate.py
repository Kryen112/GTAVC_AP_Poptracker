"""Regenerate the bulk PopTracker pack files from the GTA Vice City apworld.

Everything the pack knows about checks, items, and logic is derived here, so
the tracker cannot drift from the world it tracks. Emits:

    items/items.json
    layouts/items.json
    locations/<Check class>.json
    maps/maps.json
    scripts/locations_import.lua
    scripts/autotracking/item_mapping.lua
    scripts/autotracking/location_mapping.lua
    scripts/autotracking/setting_mapping.lua
    scripts/logic/access_rules.lua

One item is not an AP item: the game's own completion percentage, which the mod
reads and the client publishes to the AP data store. Its code and that key are
read from the apworld too, so the tracker watches what the client writes.

Pins are never placed by hand. Vice City is one map, so every check sits at its
own game position, read from data/check_coords.py and put through the transform
in data/map_geometry.json, which tools/extract_map.py writes alongside the map
image it crops and scales. Checks whose pins would overlap become one pin
holding a section each, which is what every mission strand given from one spot
turns into. The emergency vehicle activities are the one exception: they have no
world position, so they are laid out in open water.

The access rules are the world's own. Rather than reimplement the requirement
tables, this stands in a recorder for the two predicate builders in rules.py and
calls build_location_rules, so what comes back is the exact requirement
structure the generator itself uses, per option configuration. A rule is
emitted for the properties-on and properties-off worlds and switched in Lua,
and each lock item's term carries its own key so an unselected key reads as no
lock, matching how rules.py filters them.

The world package is imported without running its __init__, which would
register the World class a second time against the linked copy in the
Archipelago checkout. Only the data modules are wanted.

Run from the pack root:
    py -3.12 tools/generate.py
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import types
from collections import defaultdict
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
DEFAULT_APWORLD = PACK.parent / "GTA Vice City" / "apworld"
DEFAULT_ARCHIPELAGO = PACK.parent / "Archipelago"

# ---------------------------------------------------------------------------
# The map and its transform
# ---------------------------------------------------------------------------

MAP_GEOMETRY = PACK / "data" / "map_geometry.json"
PIN_SIZE = 14
PIN_BORDER = 2

# Pins closer together than this share one marker holding a section each.
# Anything nearer is unclickable as two markers, and on a map this size it is
# the same street corner anyway.
MERGE_DISTANCE_PIXELS = 14

# Classes whose checks keep a marker each however close together they fall. Each
# of these is one thing standing in one spot, a shopfront to walk into, a package
# to pick up, a rampage icon, a ramp to hit, so a shared marker only hides which
# of the two neighbours is still out there. Merging stays right for the classes
# where several checks really do come from one place: a giver hands out a whole
# strand from one doorway, and a property's purchase and venue missions are one
# building.
NEVER_MERGED_CLASSES = frozenset({
    "robbable_stores", "hidden_packages", "rampages", "stunt_jumps",
    "pickups",
})

# The one class placed rather than derived. Emergency vehicle milestones have no
# world position at all, so their five activities become five markers laid out
# in the open sea north east of Vice Point, each holding that activity's levels.
# These world coordinates are chosen to sit in clear water inside the cropped
# map, not read from the game.
PLACED_ROW_WORLD_Y = 1560.0
PLACED_ROW_WORLD_X = (100.0, 275.0, 450.0, 625.0, 800.0)


class Geometry:
    """How a world position becomes a pixel on the map image.

    Written by tools/extract_map.py, which crops the sea off the radar assembly
    and scales what is left, so the transform is not the plain radar formula and
    is read rather than restated here.
    """

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise SystemExit(f"{path} is missing; run tools/extract_map.py first")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.name = data["name"]
        self.image = data["image"]
        self.width = data["width"]
        self.height = data["height"]
        self.world_left = data["world_left"]
        self.world_top = data["world_top"]
        self.units_per_pixel = data["units_per_pixel"]

    def pixel(self, x: float, y: float) -> tuple[int, int]:
        """A world position as map pixels. North is up, so y counts down."""
        return (
            round((x - self.world_left) / self.units_per_pixel),
            round((self.world_top - y) / self.units_per_pixel),
        )


# ---------------------------------------------------------------------------
# Check classes
# ---------------------------------------------------------------------------

# Check class key -> (display name, the option key enabling it, pin colour name).
# Story missions are always on, so their option key is None. Display order is
# the order the location files and the layout list them in.
CHECK_CLASSES: list[tuple[str, str, str | None]] = [
    ("story_missions", "Story Missions", None),
    ("properties", "Properties", "enable_properties"),
    ("hidden_packages", "Hidden Packages", "enable_hidden_packages"),
    ("rampages", "Rampages", "enable_rampages"),
    ("stunt_jumps", "Stunt Jumps", "enable_stunt_jumps"),
    ("robbable_stores", "Robbable Stores", "enable_robbable_stores"),
    ("side_events", "Side Events", "enable_side_events"),
    ("emergency_vehicles", "Emergency Vehicle Missions", "enable_emergency_vehicles"),
    ("pickups", "Pickups", "enable_pickups"),
    ("shops", "Shops", "shuffle_shops"),
]

CLASS_DISPLAY = {key: display for key, display, _option in CHECK_CLASSES}
CLASS_OPTION = {key: option for key, _display, option in CHECK_CLASSES}

# An emergency level completes wherever the last fare or fire happens to be, so
# that class never has positions to pin and is laid out instead. Stunt jumps
# join it only while their table is missing: the game builds it on the heap and
# writes it nowhere, so it arrives from the mod's runtime dump or not at all.
BASE_UNPINNABLE_CLASSES = frozenset({"emergency_vehicles"})


def unpinnable_classes(check_coords) -> frozenset[str]:
    if not getattr(check_coords, "STUNT_JUMP_COORDS", []):
        return BASE_UNPINNABLE_CLASSES | {"stunt_jumps"}
    return BASE_UNPINNABLE_CLASSES


def class_visibility_code(class_key: str) -> str:
    return f"$vis{''.join(part.title() for part in class_key.split('_'))}"


def pin_images(class_key: str) -> dict[str, str]:
    return {
        "chest_unopened_img": f"{PIN_DIRECTORY}/{class_key}.png",
        "chest_opened_img": f"{PIN_DIRECTORY}/{class_key}_opened.png",
    }


# ---------------------------------------------------------------------------
# Settings shown in the tracker, driven by slot_data
# ---------------------------------------------------------------------------

# Scalar slot_data keys. A binary setting maps false/0 and true/1 onto its two
# stages; a staged setting lists its option keys in value order.
BINARY_SETTINGS: list[tuple[str, str, bool]] = [
    ("enable_hidden_packages", "Hidden packages", True),
    ("enable_rampages", "Rampages", True),
    ("enable_stunt_jumps", "Stunt jumps", True),
    ("enable_emergency_vehicles", "Emergency vehicle missions", True),
    ("enable_properties", "Properties and assets", True),
    ("enable_robbable_stores", "Robbable stores", True),
    ("enable_side_events", "Side events", True),
    ("enable_pickups", "Ambient pickups", False),
    ("shuffle_shops", "Shops", False),
    ("shuffle_emergency_rewards", "Shuffle emergency rewards", False),
    ("randomize_radio_stations", "Randomize radio stations", False),
    ("shuffle_minimap", "Shuffle minimap", False),
    ("randomize_pickups", "Randomize pickups", False),
    ("split_mainland_access", "Split mainland access", False),
    ("death_link", "Death Link", False),
]

# Every check class option must appear in BINARY_SETTINGS as well as in
# CHECK_CLASSES. A class in one list and not the other emits pins whose
# visibility rule asks for a code no item declares, so the pins exist and can
# never show, which is silent in the pack and only visible in game.
_class_options = {option for option in CLASS_OPTION.values() if option}
_declared = {key for key, _label, _default in BINARY_SETTINGS}
if _class_options - _declared:
    raise SystemExit(
        "check class options missing from BINARY_SETTINGS: "
        f"{sorted(_class_options - _declared)}")

STAGED_SETTINGS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("goal", "Goal", [
        ("final_mission", "Final mission"),
        ("hidden_packages", "Hidden package hunt"),
        ("hundred_percent", "100 percent"),
    ]),
    # In the option's own value order, since the mapping reads the slot_data int
    # as a stage index as well as by name.
    ("split_content_locks", "Split content locks", [
        ("off", "Off, one item per class"),
        ("per_district", "Per district"),
        ("per_class", "Per class per district"),
    ]),
]

# The stage keys above, by the option value each one is. A rule is captured once
# per entry, so this is also the order the emitted branches test in.
CONTENT_SPLIT_STAGES: list[tuple[int, str]] = [
    (0, "off"), (1, "per_district"), (2, "per_class"),
]

# The setting the content matrix puts in its corner, since the grid is drawn once
# for every seed and this is what says which of its parts a seed uses.
SPLIT_SETTING_KEY = "split_content_locks"
if SPLIT_SETTING_KEY not in {key for key, _label, _stages in STAGED_SETTINGS}:
    raise SystemExit(f"{SPLIT_SETTING_KEY} is not a staged setting any more, so "
                     "the content matrix has nothing to put in its corner")

# Set-valued slot_data keys: each member key becomes its own binary setting, so
# a rule can ask whether that one lock is selected this seed.
SET_SETTINGS: list[tuple[str, str, str]] = [
    ("ability_locks", "ability_lock", "Ability lock"),
    ("content_locks", "content_lock", "Content lock"),
]


# ---------------------------------------------------------------------------
# Item art, from the legend icons tools/extract_map.py pulls out of hud.txd
# ---------------------------------------------------------------------------

ICON_DIRECTORY = "images/items/hud"

# Progressive strand item -> the giver's own radar blip, so the items panel
# reads like the map does. Both Vercetti strands take the same V, because that is
# the one blip the game marks Tommy's own work with.
STRAND_ICONS = {
    "Rosenberg": "radar_lawyer", "Cortez": "radar_cortez", "Diaz": "radar_diaz",
    "Death Row": "radar_kent", "Avery": "radar_avery", "Phil Cassidy": "radar_phil",
    "Vercetti Protection": "tommy", "Big Mitch Baker": "bikers",
    "Umberto Robina": "cubans", "Auntie Poulet": "haitians",
    "Love Fist": "lovefist", "Mr. Black": "phone",
    "Vercetti Finale": "tommy", "Malibu Club": "club",
    "Film Studio": "filmstudio", "Printworks": "printworks",
    "Kaufman Cabs": "kcabs", "Cherry Popper": "icecream",
    "Boatyard": "boatyard", "Sunshine Autos": "SunYard",
}

RADIO_ICONS = {
    "Wildstyle": "RWildstyle", "Flash FM": "RFlash", "K-Chat": "RKchat",
    "Fever 105": "RFever", "V-Rock": "RVRock", "VCPR": "RVCPR",
    "Radio Espantoso": "REspantoso", "Emotion 98.3": "REmotion",
    "Wave 103": "RWave",
}

FALLBACK_ICON = "radar_centre"

# The pin art tools/make_pins.py draws, one colour per check class. A content
# lock item wears its own class's pin, so a row of the content matrix reads as
# the class its pins read as on the map, and five identical icons become five
# colours. The district items covering every class at once keep the neutral
# radar icon, since no one class owns them.
PIN_DIRECTORY = "images/items/pins"

# The game's own "Percentage completed" stat, which the mod reads off the same
# number the stats menu prints and the client publishes to the AP data store.
# Not an AP item: nothing in the multiworld grants it and no rule reads it, so it
# carries no item id and no logic, and the autotracker draws its number as
# overlay text on this item instead.
PERCENTAGE_ITEM = "Percentage Completed"
PERCENTAGE_ICON = "percentage"

# The content matrix leaves a hole wherever a district holds nothing of a class.
# A PopTracker item grid takes item codes and nothing else, so the hole has to be
# an item: a static one, which no click can change, drawn as nothing at all.
BLANK_ITEM = "Blank"
BLANK_ICON = "blank"

# Items the game has no art for, drawn by tools/make_icons.py instead. Vice City
# renders its weapon and vehicle icons as models rather than sprites, so without
# these the package rewards, the emergency rewards and the minimap all fall back
# to the same marker and a panel of seventeen identical icons says nothing.
DRAWN_ICON_DIRECTORY = "images/items/drawn"
DRAWN_ICONS = {
    "Body Armor Spawn": "body_armor",
    "Chainsaw Spawn": "chainsaw",
    ".357 Spawn": "pistol",
    "Flamethrower Spawn": "flamethrower",
    ".308 Sniper Spawn": "sniper",
    "Minigun Spawn": "minigun",
    "Rocket Launcher Spawn": "rocket_launcher",
    "Sea Sparrow Spawn": "sea_sparrow",
    "Rhino Spawn": "rhino",
    "Hunter Spawn": "hunter",
    "$100,000": "money",
    "Infinite Sprint": "sprint",
    "Fireproof": "fireproof",
    "Max Armor Upgrade": "max_armor",
    "Taxi Jump Ability": "taxi_jump",
    "Max Health Upgrade": "max_health",
    "Minimap": "minimap",
}


# ---------------------------------------------------------------------------
# World import
# ---------------------------------------------------------------------------

def load_world_modules():
    """Import the apworld's data modules without registering the World class."""
    apworld = Path(os.environ.get("GTAVC_APWORLD", DEFAULT_APWORLD))
    archipelago = Path(os.environ.get("AP_ROOT", DEFAULT_ARCHIPELAGO))
    package_directory = apworld / "gta_vice_city"
    if not package_directory.is_dir():
        raise SystemExit(f"apworld not found at {package_directory}; set GTAVC_APWORLD")
    if not (archipelago / "worlds").is_dir():
        raise SystemExit(f"Archipelago checkout not found at {archipelago}; set AP_ROOT")
    sys.path.insert(0, str(archipelago))
    package = types.ModuleType("gta_vice_city")
    package.__path__ = [str(package_directory)]
    sys.modules["gta_vice_city"] = package
    from gta_vice_city import data, items, locations, rules
    return data, items, locations, rules


def load_client_protocol():
    """The client's protocol module, for the data store key it publishes the
    completion percentage under. It imports nothing outside the standard
    library, and load_world_modules has already put the package on the path."""
    from gta_vice_city.client import protocol
    return protocol


def load_check_coords():
    sys.path.insert(0, str(PACK / "data"))
    import check_coords
    return check_coords


# ---------------------------------------------------------------------------
# Requirement capture
# ---------------------------------------------------------------------------

# A captured rule is ("all", requirements) or
# ("thresholds", requirements, [(alternative_requirement_sets, needed), ...]).
Rule = tuple


def capture_rules(rules, properties_enabled: bool, ability_locks: frozenset[str],
                  content_locks: frozenset[str],
                  split_mainland_access: bool = False,
                  split_content_locks: int = 0) -> dict[str, Rule]:
    """The world's own location rules as requirement structures.

    build_location_rules returns predicates built by exactly two helpers, so
    standing a recorder in for both hands back the structures instead, with no
    second copy of the requirement logic to drift.
    """
    original_requires = rules._requires
    original_thresholds = rules._requires_with_thresholds
    try:
        rules._requires = lambda requirements: ("all", list(requirements))
        rules._requires_with_thresholds = (
            lambda requirements, thresholds: (
                "thresholds", list(requirements),
                [([list(each) for each in alternatives], needed)
                 for alternatives, needed in thresholds])
        )
        return rules.build_location_rules(
            properties_enabled=properties_enabled,
            ability_locks=ability_locks,
            content_locks=content_locks,
            split_mainland_access=split_mainland_access,
            split_content_locks=split_content_locks,
        )
    finally:
        rules._requires = original_requires
        rules._requires_with_thresholds = original_thresholds


# ---------------------------------------------------------------------------
# Lua rendering
# ---------------------------------------------------------------------------

SLUG = re.compile(r"[^A-Za-z0-9]+")


def slug(text: str) -> str:
    return SLUG.sub("_", text).strip("_")


def lua_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# The world names a mission-passed requirement "<mission> Passed" and places it
# on an event location. There is no such item for the tracker to hold, so the
# rule reads whether the mission's own check has come in, which means naming the
# section that check lives in. MISSION_SECTION_PATHS is filled from the built
# location tree, since only the tree knows which node a mission ended up in.
_MISSION_PASSED_SUFFIX = " Passed"
MISSION_SECTION_PATHS: dict[str, str] = {}


def mission_passed_path(item: str) -> str | None:
    if not item.endswith(_MISSION_PASSED_SUFFIX):
        return None
    mission = item[:-len(_MISSION_PASSED_SUFFIX)]
    path = MISSION_SECTION_PATHS.get(mission)
    if path is None:
        raise SystemExit(f"no tracker section for the mission {mission!r}")
    return path


def content_lock_overlay(data, location_name: str, split: int) -> dict[str, str]:
    """Split-scoped content items -> the setting whose key decides them.

    The base map is keyed by the whole-class item name, which is the only name a
    content term carries while the locks are whole. Split, the term names a
    district item instead, and that name alone does not say which key holds it:
    Ocean Beach Content covers every selected class in Ocean Beach, so the same
    name means the packages key in a package's rule and the rampages key in a
    rampage's. The location is what resolves it, so the map is built per
    location rather than once.

    Without this a split term falls through to a plain has(), which would demand
    the district item even in a seed whose key for that class is unselected, and
    the world puts no term there at all.
    """
    overlay: dict[str, str] = {}

    def claim(item: str, setting: str) -> None:
        previous = overlay.get(item)
        if previous is not None and previous != setting:
            # One name, two keys, in one rule. Nothing produces this today and
            # lockTerm holds one setting, so it is refused rather than resolved
            # to whichever came last.
            raise SystemExit(
                f"{location_name}: {item} would read as both {previous} and "
                f"{setting}, which one lockTerm cannot express")
        overlay[item] = setting

    # The location's own class first, because it is the specific reading. A
    # district item is claimed by the property prerequisite too, and for a
    # package in a district that also holds a property those are the same name:
    # in a PACKAGE's rule that name is there for the package, so the class that
    # owns the location wins and the property claim only fills in names the
    # location does not already answer for. The finale is what needs the second
    # pass, being a mission that spends money at property icons while covered by
    # no content class of its own.
    covering = data.content_item_for(location_name, split)
    if covering is not None:
        whole = data.LOCATION_CONTENT_CLASS[location_name]
        claim(covering, f"content_lock_{data.CONTENT_ITEM_KEY[whole]}")
    for item in data.property_content_items(split):
        if item not in overlay:
            claim(item, "content_lock_properties")
    return overlay


def item_code(name: str) -> str:
    """The tracker code for an item.

    PopTracker reads an item's codes field as a comma-separated list, so a comma
    inside a name would split into two codes that nothing ever provides. The
    thousands separators in the cash items are the only ones, and dropping them
    keeps every other code the item's own name.
    """
    return name.replace(",", "")


def requirement_term(item: str, count: int, lock_settings: dict[str, str]) -> str:
    """One requirement as a Lua boolean.

    A lock item's term carries the setting naming its key: with the key
    unselected the world leaves the term out of the rule entirely, so the term
    has to read as satisfied. A mission-passed term is not an item at all, so it
    reads the mission's section instead.
    """
    section = mission_passed_path(item)
    if section is not None:
        return f"missionPassed({lua_string(section)})"
    setting = lock_settings.get(item)
    if setting is not None:
        return f"lockTerm({lua_string(item_code(item))}, {lua_string(setting)})"
    if count > 1:
        return f"itemAtLeast({lua_string(item_code(item))}, {count})"
    return f"has({lua_string(item_code(item))})"


def deduplicated(requirements: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """One entry per item at its highest count. The world gathers requirements by
    concatenation, so the same item can arrive twice (a venue mission's own
    progressive and the property sale's), and the higher count subsumes the lower."""
    highest: dict[str, int] = {}
    for item, count in requirements:
        highest[item] = max(highest.get(item, 0), count)
    seen: set[str] = set()
    ordered: list[tuple[str, int]] = []
    for item, _count in requirements:
        if item in seen:
            continue
        seen.add(item)
        ordered.append((item, highest[item]))
    return ordered


def conjunction(terms: list[str]) -> str:
    if not terms:
        return "true"
    if len(terms) == 1:
        return terms[0]
    return "(" + " and ".join(terms) + ")"


# Class -> the requirements the tracker calls soft. A check holding everything
# else is out of logic rather than unreachable, which PopTracker draws in the
# sequence break colour. The emergency activities are the case: the world puts
# their later levels behind the mainland, where the city is big enough to keep
# finding work, but the islands alone will get a patient player there, so that
# wall belongs to the routing rather than to the seed.
SOFT_REQUIREMENTS: dict[str, frozenset[str]] = {
    "emergency_vehicles": frozenset({"Mainland Access"}),
}


def threshold_term(alternatives: list[list[tuple[str, int]]], needed: int,
                   lock_settings: dict[str, str]) -> str:
    clauses = ", ".join(
        conjunction([requirement_term(item, count, lock_settings)
                     for item, count in deduplicated(each)])
        for each in alternatives
    )
    return f"satisfiedCount({{{clauses}}}) >= {needed}"


def region_expression(groups: list[list[str]], region_item: str | None,
                      lock_settings: dict[str, str],
                      omitted: frozenset[str]) -> str | None:
    """A region's entry expression, or None when it asks for nothing.

    One way in renders as its own terms; several render as a count of them, which
    is the mainland once its crossings are split. The whole expression drops when
    the region's unsplit area item is omitted, so a soft requirement relaxes the
    region however many ways in it has.
    """
    if not groups or (region_item is not None and region_item in omitted):
        return None
    if len(groups) == 1:
        return conjunction([requirement_term(item, 1, lock_settings)
                            for item in groups[0]])
    return threshold_term([[(item, 1) for item in group] for group in groups],
                          1, lock_settings)


def rule_expression(rule: Rule | None, region_groups: list[list[str]],
                    region_item: str | None,
                    lock_settings: dict[str, str],
                    omitted: frozenset[str] = frozenset()) -> str:
    """A location's full access expression: its region entry and its rule.

    Items in omitted drop out, which is how the caller asks what a rule still
    demands once a soft requirement is set aside. Threshold clauses are left
    alone: they count alternatives, so dropping a term from one changes what the
    count means rather than relaxing it.
    """
    terms: list[str] = []
    region = region_expression(region_groups, region_item, lock_settings, omitted)
    if region is not None:
        terms.append(region)
    if rule is None:
        return conjunction(terms)
    if rule[0] == "all":
        terms.extend(requirement_term(item, count, lock_settings)
                     for item, count in deduplicated(rule[1]) if item not in omitted)
        return conjunction(terms)
    _kind, requirements, thresholds = rule
    terms.extend(requirement_term(item, count, lock_settings)
                 for item, count in deduplicated(requirements) if item not in omitted)
    terms.extend(threshold_term(alternatives, needed, lock_settings)
                 for alternatives, needed in thresholds)
    return conjunction(terms)


def rule_function(function: str,
                  calls: dict[tuple[bool, bool, int], str]) -> list[str]:
    """One Lua function for a rule, switching only on the settings it depends on.

    calls is keyed by (properties class on, crossings split, content split).
    Identical versions collapse at every level, so a rule that ignores a setting
    carries no test for it, and the great majority ignore all three.
    """
    def content_branch(properties: bool, crossings: bool,
                       indent: str) -> list[str]:
        versions = [calls[properties, crossings, value]
                    for value, _stage in CONTENT_SPLIT_STAGES]
        if len(set(versions)) == 1:
            return [f"{indent}return {versions[0]}"]
        lines = []
        for position, (_value, stage) in enumerate(CONTENT_SPLIT_STAGES):
            if position == len(CONTENT_SPLIT_STAGES) - 1:
                lines.append(f"{indent}else")
                lines.append(f"{indent}\treturn {versions[position]}")
                continue
            keyword = "if" if position == 0 else "elseif"
            lines.append(f"{indent}{keyword} contentSplit({lua_string(stage)}) then")
            lines.append(f"{indent}\treturn {versions[position]}")
        lines.append(f"{indent}end")
        return lines

    def branch(properties: bool, indent: str) -> list[str]:
        whole = content_branch(properties, False, indent)
        split = content_branch(properties, True, indent)
        if whole == split:
            return whole
        return [f"{indent}if mainlandCrossingsSplit() then",
                *content_branch(properties, True, indent + "\t"),
                f"{indent}else",
                *content_branch(properties, False, indent + "\t"),
                f"{indent}end"]

    if len(set(calls.values())) == 1:
        return [f"function {function}() return {next(iter(calls.values()))} end"]
    on, off = branch(True, "\t"), branch(False, "\t")
    if on == off:
        return [f"function {function}()", *on, "end"]
    return [f"function {function}()",
            "\tif propertiesEnabled() then",
            *branch(True, "\t\t"),
            "\telse",
            *branch(False, "\t\t"),
            "\tend",
            "end"]


def access_call(rule: Rule | None, region_groups: list[list[str]],
                region_item: str | None,
                lock_settings: dict[str, str], soft: frozenset[str]) -> str:
    """The reachAccess call for one rule.

    A rule carrying a soft requirement passes what is left of it without that
    requirement as a second argument, so holding the rest reads as out of logic
    instead of unreachable.
    """
    full = rule_expression(rule, region_groups, region_item, lock_settings)
    relaxed = rule_expression(rule, region_groups, region_item, lock_settings, soft)
    if not soft or relaxed == full:
        return f"reachAccess({full})"
    return f"reachAccess({full}, {relaxed})"


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------

def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent="\t", ensure_ascii=False) + "\n",
                    encoding="utf-8")


def write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


def icon_for_item(name: str, data, items) -> str:
    drawn = DRAWN_ICONS.get(name)
    if drawn is not None:
        return f"{DRAWN_ICON_DIRECTORY}/{drawn}.png"
    for strand, icon in STRAND_ICONS.items():
        if name == data.progressive_item_name(strand):
            return f"{ICON_DIRECTORY}/{icon}.png"
    for station, icon in RADIO_ICONS.items():
        if name.endswith(station):
            return f"{ICON_DIRECTORY}/{icon}.png"
    if name in data.AREA_ITEMS:
        return f"{ICON_DIRECTORY}/arrow.png"
    if name in data.PROPERTY_OWNERSHIP_ITEMS:
        return f"{ICON_DIRECTORY}/property.png"
    if name in data.ABILITY_ITEMS:
        return f"{ICON_DIRECTORY}/fist.png"
    content_class = content_class_key(name, data)
    if content_class is not None:
        return f"{PIN_DIRECTORY}/{content_class}.png"
    if name in data.TRAP_ITEMS:
        return f"{ICON_DIRECTORY}/siterocket.png"
    if name == data.PACKAGE_FRAGMENT_ITEM:
        return f"{ICON_DIRECTORY}/tshirt.png"
    if name in items.GENERAL_FILLER_NAMES or name in data.FILLER_ITEMS:
        return f"{ICON_DIRECTORY}/gun.png"
    return f"{ICON_DIRECTORY}/{FALLBACK_ICON}.png"


def content_class_key(name: str, data) -> str | None:
    """The content class a lock item holds, whole city or one district of it.

    A district item covers every selected class in its district, so it belongs
    to no single class and answers None.
    """
    whole = data.CONTENT_ITEM_KEY.get(name)
    if whole is not None:
        return whole
    for item, districts in data.CONTENT_CLASS_DISTRICTS.items():
        if name in {data.district_class_item_name(district, item)
                    for district in districts}:
            return data.CONTENT_ITEM_KEY[item]
    return None


def build_items_json(data, items) -> list[dict]:
    """Every AP item, then the settings panel the autotracker fills from slot_data,
    then the per-class display toggles."""
    counters = {
        data.progressive_item_name(strand): data.progressive_item_count(strand)
        for strand in data.progressive_strands()
    }
    counters[data.PACKAGE_FRAGMENT_ITEM] = data.HIDDEN_PACKAGE_COUNT

    entries: list[dict] = []
    for name in items.ITEM_NAME_TO_ID:
        image = icon_for_item(name, data, items)
        maximum = counters.get(name)
        if maximum is not None:
            entries.append({
                "name": name, "type": "consumable", "img": image,
                "codes": item_code(name),
                "min_quantity": 0, "max_quantity": maximum,
                "increment": 1, "decrement": 1,
            })
        else:
            entries.append({"name": name, "type": "toggle", "img": image,
                            "codes": item_code(name)})

    # A toggle rather than a consumable: the number is overlay text the
    # autotracker writes, and the icon itself lights up on a finished game.
    entries.append({
        "name": PERCENTAGE_ITEM, "type": "toggle",
        "img": f"{DRAWN_ICON_DIRECTORY}/{PERCENTAGE_ICON}.png",
        "codes": item_code(PERCENTAGE_ITEM),
    })

    entries.append({
        "name": BLANK_ITEM, "type": "static",
        "img": f"{DRAWN_ICON_DIRECTORY}/{BLANK_ICON}.png",
        "codes": item_code(BLANK_ITEM),
    })

    for key, label, default_on in BINARY_SETTINGS:
        entries.append(binary_setting_item(key, label, default_on))
    for key, label, stages in STAGED_SETTINGS:
        entries.append({
            "name": label, "type": "progressive", "loop": False,
            "allow_disabled": False,
            "stages": [
                {
                    "img": f"images/items/settings/{key}_{stage_key}.png",
                    "name": f"{label}: {stage_label}",
                    "codes": f"{key},{key}_{stage_key}",
                    "inherit_codes": False,
                }
                for stage_key, stage_label in stages
            ],
        })
    for slot_key, prefix, label in SET_SETTINGS:
        entries.extend(
            binary_setting_item(f"{prefix}_{member}", f"{label}: {member}", False)
            for member in sorted(lock_keys_for(slot_key, data)))

    for class_key, display, _option in CHECK_CLASSES:
        entries.append(binary_setting_item(f"show_{class_key}", f"Show {display}", True))
    return entries


def binary_setting_item(key: str, label: str, default_on: bool) -> dict:
    # A progressive item carries its codes on the stages, never at the top: the
    # bare key rides every stage, so looking the object up by key still works
    # and each stage adds the one code a rule tests.
    entry = {
        "name": label, "type": "progressive", "loop": False,
        "allow_disabled": False,
        "stages": [
            {
                "img": f"images/items/settings/{key}_off.png",
                "name": f"{label}: off", "codes": f"{key},{key}_off",
                "inherit_codes": False,
            },
            {
                "img": f"images/items/settings/{key}_on.png",
                "name": f"{label}: on", "codes": f"{key},{key}_on",
                "inherit_codes": False,
            },
        ],
    }
    if default_on:
        entry["initial_stage_idx"] = 1
    return entry


def lock_keys_for(slot_key: str, data) -> set[str]:
    if slot_key == "ability_locks":
        return set(data.ABILITY_LOCK_ITEMS)
    return set(data.CONTENT_LOCK_ITEMS)


def check_positions(data, locations, check_coords,
                    unpinnable: frozenset[str]) -> dict[str, tuple[float, float]]:
    """Check name -> world position, for every check the game places."""
    positions: dict[str, tuple[float, float]] = {}
    for name, (x, y, _z) in check_coords.MISSION_COORDS.items():
        positions[name] = (x, y)
    # The packages come from the world's own table rather than check_coords, so
    # there is one copy of them; the dump script holds that table to account
    # against the decompile.
    for index, (x, y, _z) in enumerate(data.PACKAGE_COORDS):
        positions[data.hidden_package_name(index + 1)] = (x, y)
    for index, (x, y, _z) in enumerate(check_coords.RAMPAGE_COORDS):
        positions[data.rampage_name(index + 1)] = (x, y)
    # The ambient pickups come from the world's own table for the reason the
    # packages do: pickup_data holds all 110 positions, so check_coords keeps no
    # copy that could drift from it.
    for index, slot in enumerate(data.PICKUP_SLOTS):
        positions[data.pickup_name(index)] = (slot[0], slot[1])
    # Each shop's stock hangs within a metre of the next item, so the pack's own
    # merging turns a shop into one marker holding what it sells, which is what a
    # shop is from the map: one place to walk into.
    for item in data.shop_data.SHOP_ITEMS:
        positions[data.shop_data.shop_item_name(item)] = (item.x, item.y)
    for name, (x, y, _z) in check_coords.PROPERTY_COORDS.items():
        positions[name] = (x, y)
    for index, (x, y, _z) in enumerate(check_coords.STORE_COORDS):
        positions[data.robbable_store_name(index + 1)] = (x, y)
    for name, (x, y, _z) in check_coords.SIDE_EVENT_COORDS.items():
        positions[name] = (x, y)
    # Present only once the mod's runtime dump has been folded in.
    for index, (x, y, _z) in enumerate(getattr(check_coords, "STUNT_JUMP_COORDS", [])):
        positions[data.stunt_jump_name(index + 1)] = (x, y)
    unknown = sorted(name for name in positions if name not in locations.LOCATION_NAME_TO_ID)
    if unknown:
        raise SystemExit(f"check_coords names no location knows: {unknown}")
    # A check in any other class losing its position means a name moved apart in
    # the two tables, which would quietly unpin it. Fail instead: renaming checks
    # is ordinary work and the pins have to follow.
    unplaced = sorted(
        name for name in locations.LOCATION_NAME_TO_ID
        if name not in positions
        and locations.LOCATION_CLASS[name] not in unpinnable
    )
    if unplaced:
        raise SystemExit(
            "these checks have no position, and their class is one the game does "
            f"place, so the names have drifted from check_coords: {unplaced}")
    return positions


PURCHASE_SUFFIX = " Purchase"


def property_owners(data) -> dict[str, str]:
    """Every properties-class check mapped to the property it belongs to.

    A property is one building, so a pin covering nothing but that building's
    checks is named after the building: its purchase, its venue strand's
    missions, and its own activities. Sunshine Autos is why this exists: its lot
    holds a for-sale icon, a showroom door and a garage door within one marker's
    distance, so eleven checks share a pin.
    """
    owners: dict[str, str] = {}
    for purchase in data.PROPERTY_PURCHASES:
        assert purchase.endswith(PURCHASE_SUFFIX), purchase
        owners[purchase] = purchase[: -len(PURCHASE_SUFFIX)]
    for venue, missions in data.VENUE_STRANDS.items():
        for mission in missions:
            owners[mission] = venue
    for venue, activities in data.VENUE_ACTIVITIES.items():
        for activity in activities:
            owners[activity] = venue
    return owners


def shop_of(name: str) -> str | None:
    """The shop a check is sold in, or None when the check is not a shop item."""
    if not name.startswith("Shop - "):
        return None
    parts = name.split(" - ")
    return f"{parts[1]} {parts[2]}" if len(parts) >= 4 else None


# What a strand is called on a pin, where the world names it for the giver alone.
# A player reads the map, so the pin says who the missions are for; the item
# names stay the world's, which is what hints and the spoiler log print.
STRAND_DISPLAY_NAMES: dict[str, str] = {
    "Rosenberg": "Ken Rosenberg",
    "Cortez": "General Cortez",
    "Diaz": "Ricardo Diaz",
}


def strand_display(strand: str) -> str:
    return STRAND_DISPLAY_NAMES.get(strand, strand)


def node_name(members: list[str], locations, owners: dict[str, str]) -> str:
    """The pin's name. Checks sharing a pixel share a pin, and when they are one
    strand's missions the strand names it, when they are one property's checks
    the property does, when they are one shop's stock the shop does; otherwise
    the members do."""
    if len(members) == 1:
        return members[0]
    shops = {shop_of(member) for member in members}
    if len(shops) == 1 and None not in shops:
        return shops.pop()
    # A mall holds two shops close enough to share a pin, so the district names
    # it rather than eleven weapons doing so.
    if None not in shops:
        districts = {member.split(" - ")[1] for member in members}
        if len(districts) == 1:
            return f"{districts.pop()} Shops"
    strands = {locations.MISSION_GIVER.get(member) for member in members}
    if len(strands) == 1 and None not in strands:
        return strand_display(strands.pop())
    # Several strands handed out from one spot, which the mansion is: naming the
    # strands says whose missions they are, where listing a dozen mission names
    # runs off the pin and tells a player nothing they cannot read below it.
    if None not in strands:
        ordered = sorted(strands, key=list(locations.STRAND_MISSIONS).index)
        return " + ".join(strand_display(strand) for strand in ordered)
    properties = {owners.get(member) for member in members}
    if len(properties) == 1 and None not in properties:
        return properties.pop()
    return " & ".join(members)


def cluster(named_pixels: list[tuple[str, tuple[int, int]]], class_key: str,
            ) -> list[tuple[tuple[int, int], list[str]]]:
    """Group checks whose pins would sit on top of each other.

    Greedy by distance: a check joins the first cluster it is within
    MERGE_DISTANCE_PIXELS of, and the marker then sits at the middle of its
    members. This is what turns a giver's whole strand, all of it handed out from
    one spot, into one marker. A class in NEVER_MERGED_CLASSES skips the joining
    and keeps every check on its own marker at its own position.
    """
    if class_key in NEVER_MERGED_CLASSES:
        return [(position, [name]) for name, position in named_pixels]
    clusters: list[list[tuple[str, tuple[int, int]]]] = []
    for name, (x, y) in named_pixels:
        for members in clusters:
            first_x, first_y = members[0][1]
            if (abs(x - first_x) <= MERGE_DISTANCE_PIXELS
                    and abs(y - first_y) <= MERGE_DISTANCE_PIXELS):
                members.append((name, (x, y)))
                break
        else:
            clusters.append([(name, (x, y))])
    placed = []
    for members in clusters:
        centre = (
            round(sum(position[0] for _name, position in members) / len(members)),
            round(sum(position[1] for _name, position in members) / len(members)),
        )
        placed.append((centre, [name for name, _position in members]))
    return placed


def build_locations(data, locations, positions, geometry: Geometry,
                    ) -> dict[str, list[dict]]:
    """Class display name -> the group's location nodes.

    Checks the game places are pinned at their own position, merged when they
    would overlap. The emergency vehicle activities are laid out in open water
    instead, since a level completes wherever the last fare happens to be.
    Anything else the game places nowhere is listed without a pin.
    """
    by_class: dict[str, list[str]] = defaultdict(list)
    for name in locations.LOCATION_NAME_TO_ID:
        by_class[locations.LOCATION_CLASS[name]].append(name)

    owners = property_owners(data)
    groups: dict[str, list[dict]] = {}
    for class_key, display, _option in CHECK_CLASSES:
        members = by_class.get(class_key, [])
        if not members:
            continue
        nodes: list[dict] = []

        pinned = [(name, geometry.pixel(*positions[name]))
                  for name in members if name in positions]
        for (x, y), shared in cluster(pinned, class_key):
            nodes.append({
                "name": node_name(shared, locations, owners),
                **pin_images(class_key),
                "sections": [{"name": name} for name in shared],
                "map_locations": [{"map": geometry.name, "x": x, "y": y}],
                "visibility_rules": [class_visibility_code(class_key)],
            })

        unpinned = [name for name in members if name not in positions]
        for index, (group, group_members) in enumerate(
                unpinned_nodes(class_key, unpinned, data)):
            node = {
                "name": group,
                **pin_images(class_key),
                "sections": [{"name": name} for name in group_members],
                "visibility_rules": [class_visibility_code(class_key)],
            }
            if class_key == "emergency_vehicles" and index < len(PLACED_ROW_WORLD_X):
                x, y = geometry.pixel(PLACED_ROW_WORLD_X[index], PLACED_ROW_WORLD_Y)
                node["map_locations"] = [{"map": geometry.name, "x": x, "y": y}]
            nodes.append(node)
        groups[display] = nodes
    stands = shop_stand_merges(data, locations, positions)
    for host_name, guests in merge_markers(
            groups, locations, {**stands, **CONSENTED_MARKER_MERGES},
            {**{store: shop_node_name(store) for store in stands.values()},
             **CONSENTED_MARKER_HEADERS}):
        print(f"merged into {host_name}: {', '.join(sorted(guests))}")
    return groups


# How close a pay stand has to be to a shop's own check to be inside that shop.
# The six stands that share a shop with a robbery sit 3.3 to 15.2 world units
# from it, and the nearest stand to a shop it is NOT in is far outside this, so
# the band separates the two cases without naming a single shop.
SHOP_STAND_DISTANCE_UNITS = 20.0


def shop_stand_merges(data, locations, positions: dict[str, tuple[float, float]],
                      ) -> dict[str, str]:
    """Pay stand check -> the store check standing in the same shop.

    The in-shop stands are the one place two classes are the same PLACE rather
    than the same point: a stand is inside a shop, and the shop's own robbery
    check is the same doorway a player walks through. On the map they are one
    marker, and merging them is what stops one hiding under the other.

    Only the ten type-1 stands can merge, and only into a robbable store, so
    nothing else in the pack is joined across classes. Distance and not a named
    list, so a shop the audit moves keeps working.
    """
    stores = [name for name in locations.LOCATION_NAME_TO_ID
              if locations.LOCATION_CLASS[name] == "robbable_stores"
              and name in positions]
    merges: dict[str, str] = {}
    for index in sorted(data.PICKUP_PAY_STAND_INDICES):
        stand = data.pickup_name(index)
        if stand not in positions:
            continue
        stand_x, stand_y = positions[stand]
        nearest, best = None, None
        for store in stores:
            store_x, store_y = positions[store]
            distance = math.hypot(stand_x - store_x, stand_y - store_y)
            if distance <= SHOP_STAND_DISTANCE_UNITS and (best is None
                                                          or distance < best):
                nearest, best = store, distance
        if nearest is not None:
            merges[stand] = nearest
    return merges


# Checks whose markers stand on one place across two classes, where the place is
# a single named thing rather than a rule anything derives. Guest check -> the
# check whose marker absorbs it. Every entry is a merge asked for by name, and
# the host is the thing a player would say they are standing at.
#
# Full names on both sides because the short ones are not unique: there is a
# Jewelers in Downtown as well as in Vice Point, and a Body Armor 01 in four
# districts.
CONSENTED_MARKER_MERGES: dict[str, str] = {
    # The purchase icon and the mission that starts at its door, 5.2 units.
    "Cap the Collector": "Printworks Purchase",
    # A package inside a shop and that shop's own robbery, 2.7 and 4.0 units.
    "Hidden Package - Vice Point - Inside the Jewelers":
        "Store Robbery - Vice Point - The Jewelers",
    "Hidden Package - Little Havana - Inside Calleggi Delicatessen Restaurant":
        "Store Robbery - Little Havana - Calleggi Delicatessen Restaurant",
    "Hidden Package - Little Havana - Inside the Laundromat":
        "Store Robbery - Little Havana - Laundromat",
    # A package named for the body armour it lies beside, 3.2 units. The armour
    # hosts, since it is the thing the package is named after.
    "Hidden Package - Washington Beach - Near Body armour behind big Pink building":
        "Pickup - Washington Beach - Body Armor behind big pink building",
}

# What to call a marker that now stands for more than the check it was named for.
# Host check -> heading. Without an entry the host keeps its own name, which is
# right for a marker like the Printworks that is already named for its building.
CONSENTED_MARKER_HEADERS: dict[str, str] = {
    "Store Robbery - Vice Point - The Jewelers": "Vice Point Jewelers",
    "Store Robbery - Little Havana - Calleggi Delicatessen Restaurant":
        "Little Havana Calleggi Delicatessen",
    "Pickup - Washington Beach - Body Armor behind big pink building":
        "Washington Beach Body Armor",
    "Store Robbery - Little Havana - Laundromat": "Little Havana Laundromat",
}


def merge_markers(groups: dict[str, list[dict]], locations,
                  merges: dict[str, str], headers: dict[str, str],
                  ) -> list[tuple[str, list[str]]]:
    """Fold each guest check's marker into the marker holding its host check.

    Cross-class, and safe only because PopTracker carries visibility per SECTION
    as well as per location and inherits the parent's rules on top. The marker is
    visible when EITHER class is on and each section only with its own class, so a
    seed with the guest's class off keeps the host's checks and drops the guest's
    rather than showing checks the seed does not contain, which would leave a
    marker that can never complete.

    Returns what it merged, so the run prints it and nothing is quiet about a
    marker standing for more than one class.
    """
    holding = {}
    for display, nodes in groups.items():
        for node in nodes:
            for section in node["sections"]:
                holding[section["name"]] = (display, node)

    def visibility_of(check: str) -> str:
        return class_visibility_code(locations.LOCATION_CLASS[check])

    absorbed = []
    for guest_check, host_check in sorted(merges.items()):
        host_entry = holding.get(host_check)
        guest_entry = holding.get(guest_check)
        if host_entry is None or guest_entry is None:
            raise SystemExit(f"cannot merge {guest_check} into {host_check}: "
                             "one of them has no marker")
        _host_display, host = host_entry
        guest_display, guest = guest_entry
        if host is guest:
            continue
        if len(guest["sections"]) != 1:
            # Moving a marker that already holds several checks would take the
            # others with it, which is a merge nobody asked for.
            raise SystemExit(f"{guest_check} shares its marker, so it cannot be "
                             "folded without taking its neighbours along")
        for section in host["sections"]:
            section.setdefault("visibility_rules",
                               [visibility_of(section["name"])])
        moved = guest["sections"][0]
        moved["visibility_rules"] = [visibility_of(guest_check)]
        host["sections"].append(moved)
        if host_check in headers:
            host["name"] = headers[host_check]
        # Either class shows the marker, which is what the OR of a rules list is.
        host["visibility_rules"] = sorted({
            visibility_of(section["name"]) for section in host["sections"]})
        groups[guest_display].remove(guest)
        absorbed.append((host["name"], guest_check))

    merged = {}
    for host_name, guest_check in absorbed:
        merged.setdefault(host_name, []).append(guest_check)
    return sorted(merged.items())


def shop_node_name(store_check: str) -> str:
    """The shop's own name, for a marker that is no longer only its robbery.

    "Store Robbery - Vice Point - Dispensary" becomes "Vice Point Dispensary",
    which is what a player calls the place they are standing in.
    """
    parts = [part.strip() for part in store_check.split(" - ")]
    return " ".join(parts[1:]) if len(parts) >= 3 else store_check


def unpinned_nodes(class_key: str, members: list[str],
                   data) -> list[tuple[str, list[str]]]:
    """The unpinned checks of a class, bundled into listed nodes."""
    if not members:
        return []
    if class_key == "emergency_vehicles":
        return [
            (activity, [name for name in members if name.startswith(f"{activity} ")])
            for activity in data.EMERGENCY_LEVELS
        ]
    return [(CLASS_DISPLAY[class_key], members)]


def section_path(display: str, node: str, section: str) -> str:
    return f"@{display}/{node}/{section}"


def main() -> int:
    data, items, locations, rules = load_world_modules()
    protocol = load_client_protocol()
    check_coords = load_check_coords()
    # The Lua builds the percentage key from the prefix, the team and the slot.
    # A client that assembled it differently would leave the tracker watching a
    # key nothing writes, showing no number and saying nothing, so it fails here.
    if protocol.percentage_key(0, 3) != f"{protocol.PERCENTAGE_KEY_PREFIX}0_3":
        raise SystemExit(
            "the client's percentage key is no longer the prefix followed by "
            "team and slot; setting_mapping.lua would watch the wrong key")

    geometry = Geometry(MAP_GEOMETRY)
    unknown_soft = sorted(
        [key for key in SOFT_REQUIREMENTS if key not in CLASS_DISPLAY]
        + [item for soft in SOFT_REQUIREMENTS.values() for item in soft
           if item not in items.ITEM_NAME_TO_ID])
    if unknown_soft:
        raise SystemExit(
            f"SOFT_REQUIREMENTS names classes or items that do not exist: {unknown_soft}")
    unknown_icons = sorted(set(DRAWN_ICONS) - set(items.ITEM_NAME_TO_ID))
    if unknown_icons:
        raise SystemExit(
            f"DRAWN_ICONS names items the world does not have: {unknown_icons}")
    missing_art = sorted(
        drawn for drawn in set(DRAWN_ICONS.values()) | {PERCENTAGE_ICON, BLANK_ICON}
        if not (PACK / DRAWN_ICON_DIRECTORY / f"{drawn}.png").is_file())
    if missing_art:
        raise SystemExit(
            f"drawn icons missing, run tools/make_icons.py: {missing_art}")
    # A content lock item wears its class's pin, so a class whose pin is not
    # drawn would leave a row of the content matrix blank.
    missing_pins = sorted(
        key for key in set(data.CONTENT_ITEM_KEY.values())
        if not (PACK / PIN_DIRECTORY / f"{key}.png").is_file())
    if missing_pins:
        raise SystemExit(
            f"content lock pin art missing, run tools/make_pins.py: {missing_pins}")

    positions = check_positions(data, locations, check_coords,
                                unpinnable_classes(check_coords))
    groups = build_locations(data, locations, positions, geometry)
    attach_access_rules(groups)

    # ---- items/items.json and the panels showing them --------------------
    item_entries = build_items_json(data, items)
    layout = build_items_layout(data, items)
    check_layout_shows_items(data, items, item_entries, layout)
    write_json(PACK / "items" / "items.json", item_entries)
    write_json(PACK / "layouts" / "items.json", layout)

    # ---- maps/maps.json ---------------------------------------------------
    write_json(PACK / "maps" / "maps.json", [{
        "name": geometry.name,
        "location_size": PIN_SIZE,
        "location_border_thickness": PIN_BORDER,
        "img": geometry.image,
    }])

    # ---- locations/<class>.json and the import list ----------------------
    import_lines = []
    for class_key, display, _option in CHECK_CLASSES:
        nodes = groups.get(display)
        if not nodes:
            continue

        write_json(PACK / "locations" / f"{display}.json", [{
            "name": display,
            **pin_images(class_key),
            "children": nodes,
        }])
        import_lines.append(f'Tracker:AddLocations("locations/{display}.json")')
    write_text(PACK / "scripts" / "locations_import.lua", "\n".join(import_lines) + "\n")

    # ---- scripts/autotracking/location_mapping.lua ------------------------
    path_of: dict[str, str] = {}
    for _class_key, display, _option in CHECK_CLASSES:
        for node in groups.get(display, []):
            for section in node["sections"]:
                path_of[section["name"]] = section_path(display, node["name"],
                                                        section["name"])
    missing_paths = [name for name in locations.LOCATION_NAME_TO_ID if name not in path_of]
    if missing_paths:
        raise SystemExit(f"locations with no tracker section: {missing_paths}")
    location_rows = "\n".join(
        f"\t[{location_id}] = {{{lua_string(path_of[name])}}},"
        for name, location_id in locations.LOCATION_NAME_TO_ID.items()
    )
    write_text(PACK / "scripts" / "autotracking" / "location_mapping.lua",
               "LOCATION_MAPPING = {\n" + location_rows + "\n}\n")

    MISSION_SECTION_PATHS.update(
        (mission, path_of[mission]) for mission in data.ROUTE_MISSIONS)

    # ---- scripts/autotracking/item_mapping.lua ---------------------------
    counters = {
        data.progressive_item_name(strand) for strand in data.progressive_strands()
    } | {data.PACKAGE_FRAGMENT_ITEM}
    item_rows = "\n".join(
        f"\t[{item_id}] = {{{lua_string(item_code(name))}, "
        f"{lua_string('consumable' if name in counters else 'toggle')}}},"
        for name, item_id in items.ITEM_NAME_TO_ID.items()
    )
    write_text(PACK / "scripts" / "autotracking" / "item_mapping.lua",
               "ITEM_MAPPING = {\n" + item_rows + "\n}\n")

    # ---- scripts/autotracking/setting_mapping.lua ------------------------
    write_text(PACK / "scripts" / "autotracking" / "setting_mapping.lua",
               render_setting_mapping(data, protocol.PERCENTAGE_KEY_PREFIX))

    # ---- scripts/logic/access_rules.lua ---------------------------------
    # A lock item -> the setting whose on stage means its key was selected. The
    # two families have their own settings, so a term names exactly one.
    lock_settings = {
        **{item: f"ability_lock_{key}" for item, key in data.ABILITY_ITEM_KEY.items()},
        **{item: f"content_lock_{key}" for item, key in data.CONTENT_ITEM_KEY.items()},
    }
    all_ability_locks = frozenset(data.ABILITY_LOCK_ITEMS)
    all_content_locks = frozenset(data.CONTENT_LOCK_ITEMS)
    # Two settings change what a rule asks for, so every combination is captured
    # and only the differences are emitted: the properties class decides whether
    # the finale carries its asset prerequisite, and the mainland crossing split
    # decides whether the mainland is one item or a choice of four.
    captured = {
        (properties, split, content_split): capture_rules(
            rules, properties, all_ability_locks, all_content_locks, split,
            content_split)
        for properties in (True, False)
        for split in (False, True)
        for content_split, _stage in CONTENT_SPLIT_STAGES
    }
    regions_by_split = {
        split: {
            region: data.region_access_groups(region, split)
            for region in (data.REGION_VICE_CITY, data.REGION_MAINLAND,
                           data.REGION_STARFISH)
        }
        for split in (False, True)
    }
    region_items = {
        region: (None if region == data.REGION_VICE_CITY
                 else data.AREA_ITEM_BY_REGION[region])
        for region in {data.REGION_VICE_CITY, data.REGION_MAINLAND, data.REGION_STARFISH}
    }

    lines = [
        "-- Generated by tools/generate.py from the GTA Vice City apworld.",
        "-- One function per AP location returning its PopTracker AccessibilityLevel:",
        "-- its region entry ANDed with the world's own requirements for it. A rule",
        "-- that differs between the properties class being on and off, between",
        "-- the mainland crossings being split and whole, or between the three",
        "-- content lock granularities, carries each version and switches on the",
        "-- seed's own settings. A second argument is what the",
        "-- rule still demands once its soft requirement is set aside, and holding",
        "-- that much makes the check out of logic rather than unreachable.",
        "",
    ]
    for name in locations.LOCATION_NAME_TO_ID:
        region = locations.LOCATION_REGIONS[name]
        region_item = region_items[region]
        soft = SOFT_REQUIREMENTS.get(locations.LOCATION_CLASS[name], frozenset())
        calls = {
            key: access_call(
                rules_for.get(name), regions_by_split[key[1]][region], region_item,
                {**lock_settings, **content_lock_overlay(data, name, key[2])}, soft)
            for key, rules_for in captured.items()
        }
        # The item a content term names is the one the world would name at that
        # granularity, and nothing downstream can tell a wrong-but-real name from
        # a right one: naming Hidden Packages where the seed hands over Ocean
        # Beach Hidden Packages is a rule that can never pass, and it reads as
        # ordinary Lua to every other check. So it is asserted here, where both
        # sides are in hand.
        for key, call in calls.items():
            expected = data.content_item_for(name, key[2])
            if expected is None:
                continue
            if f'"{item_code(expected)}"' not in call:
                raise SystemExit(
                    f"{name}: the rule for split {key[2]} does not name "
                    f"{expected!r}, which is the item covering it")
        lines.extend(rule_function(f"rule_{slug(name)}", calls))
    write_text(PACK / "scripts" / "logic" / "access_rules.lua", "\n".join(lines) + "\n")

    pinned = sum(1 for name in locations.LOCATION_NAME_TO_ID if name in positions)
    print(f"items      {len(items.ITEM_NAME_TO_ID):>4}")
    print(f"locations  {len(locations.LOCATION_NAME_TO_ID):>4} "
          f"({pinned} pinned, {len(locations.LOCATION_NAME_TO_ID) - pinned} listed)")
    print(f"pins       {sum(1 for nodes in groups.values() for node in nodes if 'map_locations' in node):>4}")
    # Counted by the CHECK's own class and not by the file its section ended up
    # in, since a pay stand's section lives in the shop it stands in: counting
    # sections per file read that stand as a robbable store and left the pickups
    # six short of the 110 there are.
    for class_key, display, _option in CHECK_CLASSES:
        nodes = groups.get(display, [])
        checks = sum(1 for name in locations.LOCATION_NAME_TO_ID
                     if locations.LOCATION_CLASS[name] == class_key)
        print(f"  {display:<28} {checks:>4} checks in {len(nodes):>3} nodes")
    return 0


def attach_access_rules(groups: dict[str, list[dict]]) -> None:
    """Point each section at its generated rule. A shared pin holds several
    checks whose rules differ, so the rule belongs on the section, not the node."""
    for nodes in groups.values():
        for node in nodes:
            for section in node["sections"]:
                section["access_rules"] = [f"^$rule_{slug(section['name'])}"]


def item_grid(rows: list[list[str]]) -> dict:
    return {"type": "itemgrid", "item_margin": "2,2", "rows": rows}


def wrap(names: list[str], per_row: int) -> list[list[str]]:
    codes = [item_code(name) for name in names]
    return [codes[start:start + per_row] for start in range(0, len(codes), per_row)]


def content_matrix(data) -> list[list[str]]:
    """The content lock items as a grid, a column per district and a row per class.

    Which of them a seed puts in the pool is split_content_locks' answer, and a
    layout cannot ask: it is drawn once and shown to every seed, PopTracker
    having visibility rules for locations and none for layouts. So all three
    granularities are laid out at once, placed so the grid itself says which is
    which. Column zero holds the whole-class items, the first row the
    per-district ones, and the rest is one item per class per district. The
    corner is the split setting, the axis the rest is read along, and a district
    holding nothing of a class leaves a blank.
    """
    districts = list(data.CONTENT_DISTRICTS)
    rows = [[SPLIT_SETTING_KEY]
            + [item_code(data.district_content_item_name(district))
               for district in districts]]
    for whole in data.CONTENT_ITEMS:
        covered = data.CONTENT_CLASS_DISTRICTS[whole]
        rows.append(
            [item_code(whole)]
            + [item_code(data.district_class_item_name(district, whole))
               if district in covered else item_code(BLANK_ITEM)
               for district in districts])

    # Every content item exactly once, so a new district or a new content class
    # cannot quietly miss its cell: an item the grid never names is an item the
    # player never sees arrive.
    laid_out = [code for row in rows for code in row
                if code not in (SPLIT_SETTING_KEY, item_code(BLANK_ITEM))]
    expected = [item_code(name) for name in
                [*data.CONTENT_ITEMS, *data.all_district_content_items()]]
    missing = sorted(set(expected) - set(laid_out))
    extra = sorted(set(laid_out) - set(expected))
    twice = sorted({code for code in laid_out if laid_out.count(code) > 1})
    if missing or extra or twice or len(laid_out) != len(expected):
        raise SystemExit(
            "the content matrix does not hold every content lock item exactly "
            f"once; missing {missing}, unexpected {extra}, twice over {twice}")
    return rows


def build_items_layout(data, items) -> dict:
    """The two panels beside the map, and the pack settings window.

    Generated alongside items.json so a renamed item cannot leave a dead code
    behind in a hand-written layout. Cash, filler, and traps are left out: they
    gate nothing and would bury the items that do.

    Three homes, each reading as one thing. One column holds what the player
    has. The other holds the content matrix, which is wide enough to want a
    column of its own, and the map filters. What the seed was rolled with goes
    into PopTracker's own pack settings window rather than beside the map, since
    the autotracker fills it from slot_data and a player changes it about never.

    Two columns rather than one because PopTracker lays the window out to
    whatever height its tallest column needs and fits the map into that: a
    column taller than the window makes the map taller than the window too.
    """
    story = [data.progressive_item_name(giver) for giver in data.STORY_GIVERS]
    venues = [data.progressive_item_name(venue) for venue in data.VENUE_STRANDS]
    settings_codes = (
        [key for key, _label, _stages in STAGED_SETTINGS]
        + [key for key, _label, _default in BINARY_SETTINGS]
    )
    lock_codes = [
        f"{prefix}_{member}"
        for slot_key, prefix, _label in SET_SETTINGS
        for member in sorted(lock_keys_for(slot_key, data))
    ]
    display_codes = [f"show_{class_key}" for class_key, _display, _option in CHECK_CLASSES]

    # A group is its header, what it holds, and how wide it sits in a column.
    # The strip shapes the same holdings for itself, so the two variants cannot
    # come to hold different items.
    owned_groups = [
        ("Area access", list(data.AREA_ITEMS), 6),
        ("Goal", [data.PACKAGE_FRAGMENT_ITEM, PERCENTAGE_ITEM], 2),
        ("Story strands", story, 7),
        ("Venue strands", venues, 7),
        ("Property ownership", list(data.PROPERTY_OWNERSHIP_ITEMS), 8),
        ("Abilities", list(data.ABILITY_ITEMS), 8),
        ("Package rewards", list(data.PACKAGE_REWARD_ITEMS), 6),
        ("Emergency rewards", list(data.EMERGENCY_REWARD_ITEMS), 5),
        ("Radio and minimap", [*data.RADIO_STATION_ITEMS, data.MINIMAP_ITEM], 5),
    ]
    filter_groups = [("Show on map", display_codes, 8)]
    settings_groups = [
        ("Seed options", settings_codes, 7),
        ("Locks selected", lock_codes, 6),
    ]

    owned_sections = column_sections(owned_groups)
    content_sections = [("Content locks", content_matrix(data))]
    beside_sections = [*content_sections, *column_sections(filter_groups)]
    settings_sections = column_sections(settings_groups)
    # The strip holds every group the two columns hold, regrouped for a band,
    # since the horizontal variant is those columns laid along the bottom.
    strip_sections = strip_shaped([*owned_groups, *filter_groups],
                                  dict(content_sections))
    report_column_sizes(owned_sections, beside_sections)
    report_strip_size(strip_sections)
    return {
        # One column of what the player holds, for the broadcast window, which
        # is its own narrow thing. The map filters are left out of it for want
        # of a map, and the seed settings for want of the button that opens
        # them, which that window does not carry.
        "items": column_layout(owned_sections + content_sections),
        "panel_one": column_layout(owned_sections),
        "panel_two": column_layout(beside_sections),
        # The same groups as one row, for the horizontal variant, whose window
        # puts them along the bottom with the map above.
        "panel_strip": strip_layout(strip_sections),
        # PopTracker's own pack settings window, opened from the button in its
        # top bar. The key is the one the tracker looks that layout up by, and
        # the margin is what a window of its own wants and a docked panel does
        # not.
        "settings_popup": column_layout(settings_sections, margin=POPUP_MARGIN),
    }


def layout_item_codes(layout) -> set[str]:
    """Every item code a layout names, grids and single items alike."""
    codes: set[str] = set()
    if isinstance(layout, dict):
        if layout.get("type") == "itemgrid":
            codes.update(code for row in layout.get("rows", [])
                         for code in row if code)
        if layout.get("type") == "item" and layout.get("item"):
            codes.add(layout["item"])
        for value in layout.values():
            codes |= layout_item_codes(value)
    elif isinstance(layout, list):
        for value in layout:
            codes |= layout_item_codes(value)
    return codes


def entry_codes(entry: dict) -> set[str]:
    """Every code one items.json entry provides, its stages included."""
    fields = [entry.get("codes"),
              *(stage.get("codes") for stage in entry.get("stages", []))]
    return {part.strip() for field in fields if field
            for part in field.split(",")}


# The panels each window of the pack draws. An item shown in one window and not
# the other is a variant holding less than its sibling, which the union of every
# panel cannot see, so the check below runs per window. The broadcast column is
# deliberately a subset of what the player holds, so it is not a window here.
WINDOW_PANELS: dict[str, tuple[str, ...]] = {
    "the columns": ("panel_one", "panel_two", "settings_popup"),
    "the strip": ("panel_strip", "settings_popup"),
}


def check_layout_shows_items(data, items, entries: list[dict], layout: dict) -> None:
    """Every item standing for progress or for a setting sits in every window.

    An item can be declared, mapped by the autotracker, and laid out nowhere: it
    arrives, turns on, and shows the player nothing, which is silent in the pack
    and visible only in the tracker. Each window is asked separately, since a
    window drawing its own panels can hold less than its sibling does.
    """
    declared = {code for entry in entries for code in entry_codes(entry)}
    undeclared = sorted(layout_item_codes(layout) - declared)
    if undeclared:
        raise SystemExit(
            f"a layout names item codes items.json does not declare: {undeclared}")
    # Cash, filler and traps gate nothing and would bury the items that do, so
    # they are deliberately absent; the blank cell is furniture, not an item.
    quiet = {item_code(name) for name in
             [*data.FILLER_ITEMS, *items.GENERAL_FILLER_NAMES, *data.TRAP_ITEMS,
              BLANK_ITEM]}
    for window, panels in WINDOW_PANELS.items():
        absent = [panel for panel in panels if panel not in layout]
        if absent:
            raise SystemExit(
                f"WINDOW_PANELS names panels the layout does not hold: {absent}")
        shown = layout_item_codes({panel: layout[panel] for panel in panels})
        hidden = sorted(entry["name"] for entry in entries
                        if not entry_codes(entry) & shown
                        and not entry_codes(entry) & quiet)
        if hidden:
            raise SystemExit(
                f"items.json declares items {window} never shows: {hidden}")


def column_sections(groups: list[tuple[str, list[str], int]]
                    ) -> list[tuple[str, list[list[str]]]]:
    """Groups shaped for a column, each as wide as it was written to be."""
    return [(header, wrap(names, per_row)) for header, names, per_row in groups]


def strip_shaped(groups: list[tuple[str, list[str], int]],
                 fixed: dict[str, list[list[str]]]
                 ) -> list[tuple[str, list[list[str]]]]:
    """The groups regrouped and reshaped for the strip along the bottom.

    A group in a strip costs length, and length is what a strip runs out of, so
    each one is made as deep as the band allows and no wider than it has to be.
    The regrouping is STRIP_GROUPS' doing and the items are the columns' items
    either way: a group named there and not here, or here and not there, is
    refused rather than quietly dropped from one variant.
    """
    holdings: dict[str, list[str]] = {}
    for header, names, _per_row in groups:
        if header in holdings:
            raise SystemExit(
                f"two column groups are both headed {header!r}, so the strip "
                "cannot say which of them it is regrouping")
        holdings[header] = names
    named = [source for _header, sources in STRIP_GROUPS for source in sources]
    if sorted(named) != sorted(holdings):
        raise SystemExit(
            "STRIP_GROUPS does not regroup exactly the column groups, and each "
            f"of them exactly once: {sorted(set(named) ^ set(holdings))}")
    # Both directions on the fixed sections too: one the table forgets is a
    # section the columns draw and the strip does not.
    shaped = {header for header, sources in STRIP_GROUPS if not sources}
    if shaped != set(fixed):
        raise SystemExit(
            "STRIP_GROUPS and the sections keeping their own shape do not name "
            f"the same ones: {sorted(shaped ^ set(fixed))}")

    sections = []
    for header, sources in STRIP_GROUPS:
        if not sources:
            sections.append((header, fixed[header]))
            continue
        names = [name for source in sources for name in holdings[source]]
        if not names:
            raise SystemExit(f"the strip's {header!r} holds nothing")
        sections.append((header, wrap(names, math.ceil(len(names) / STRIP_ROWS))))
    return sections


def strip_layout(sections: list[tuple[str, list[list[str]]]]) -> dict:
    """The groups as one row, each docked to the left of the space left.

    Every grid in the band is drawn at the strip's own icon size, the content
    grid included, so the band is one size throughout and shorter than a row of
    panel-sized icons would be.
    """
    return {
        "type": "dock",
        "content": [
            {"type": "group", "dock": "left", "header": header,
             "content": {**item_grid(rows), "item_size": STRIP_ICON_SIZE}}
            for header, rows in sections
        ],
    }


# What the pack settings window keeps between its content and its own edges.
POPUP_MARGIN = 5


def column_layout(sections: list[tuple[str, list[list[str]]]],
                  margin: int | None = None) -> dict:
    layout = {
        "type": "array",
        "orientation": "vertical",
        "content": [
            {"type": "group", "header": header, "content": item_grid(rows)}
            for header, rows in sections
        ],
    }
    if margin is not None:
        layout["margin"] = margin
    return layout


# Rough sizes, for the warnings below only: one row of icons, and the group
# header above it, in the proportions PopTracker draws them.
ROW_HEIGHT = 38
HEADER_HEIGHT = 30
# An icon and its margins are as wide as they are tall, so a row of them is as
# wide as a row is high.
ICON_WIDTH = ROW_HEIGHT
# About what a 1080p window leaves for a column once its own furniture is out of
# the way. A column past this makes the map taller than the window.
COLUMN_HEIGHT_BUDGET = 950
# What both columns may take together. The map draws contained in the pane, so a
# column height around 900 has the city wanting some 700 px of width, and a 1920
# window has about 1200 to give the columns before the map has to shrink for
# them.
PANEL_WIDTH_BUDGET = 1200
# The horizontal variant's strip is a glance bar rather than a panel, so it is
# drawn in its own size: smaller icons than a column's, and enough rows that a
# group spends its space downwards rather than along the band, since length is
# what a strip runs out of. Eight rows of the smaller icon is about as deep as
# the content grid's six, so the band stays shallow either way.
STRIP_ICON_SIZE = 24
STRIP_CELL = STRIP_ICON_SIZE + 6
STRIP_ROWS = 8
# What the strip may take. A 1080p window has about 978 px of height, and the
# city wants some 500 px of it to stay readable, so the band may have 350; and
# the row runs out of window at about 1900 px wide.
STRIP_HEIGHT_BUDGET = 350
STRIP_WIDTH_BUDGET = 1900

# How the strip regroups the columns' groups. A group two icons wide under the
# words "Property ownership" is as wide as the words are, so a band of them is
# mostly headers: the strip carries fewer frames and shorter words, and the gate
# in strip_shaped refuses a column group this table forgets. An entry naming no
# column group is a section that keeps its own shape, the content grid being the
# one that says what it says by being a matrix.
STRIP_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Access", ("Area access",)),
    ("Goal", ("Goal",)),
    ("Story", ("Story strands",)),
    ("Venues", ("Venue strands",)),
    ("Property", ("Property ownership",)),
    ("Abilities", ("Abilities",)),
    ("Rewards", ("Package rewards", "Emergency rewards")),
    ("Radio", ("Radio and minimap",)),
    ("Content locks", ()),
    ("Show", ("Show on map",)),
]


def section_height(section: tuple[str, list[list[str]]]) -> int:
    _header, rows = section
    return len(rows) * ROW_HEIGHT + HEADER_HEIGHT


def report_strip_size(sections: list[tuple[str, list[list[str]]]]) -> None:
    """Say how deep and how long the horizontal variant's strip comes out.

    A strip trades the columns' problem for the mirror of it: its depth is taken
    off the map's height rather than its width, and its length is what runs off
    the side of the window.

    The length counts icons alone. A group is never narrower than its own header,
    and most of the band's groups are one or two icons wide, so the band draws
    longer than this by the words it carries: the budget is set well inside the
    window for that reason, and only a render says by how much.
    """
    depth = max((len(rows) * STRIP_CELL + HEADER_HEIGHT
                 for _header, rows in sections), default=0)
    icons = sum(max((len(row) for row in rows), default=0)
                for _header, rows in sections)
    length = icons * STRIP_CELL
    print(f"strip     about {depth:>4} px deep, {icons} icons "
          f"({length} px of icons, headers on top) long, {len(sections)} sections")
    if depth > STRIP_HEIGHT_BUDGET:
        print(f"  a strip past {STRIP_HEIGHT_BUDGET} px deep leaves the map less "
              "height than the city needs; lower STRIP_ROWS or STRIP_ICON_SIZE")
    if length > STRIP_WIDTH_BUDGET:
        print(f"  a strip past {STRIP_WIDTH_BUDGET} px long runs off the side of "
              "the window; raise STRIP_ROWS or merge groups in STRIP_GROUPS")


def report_column_sizes(*columns: list[tuple[str, list[list[str]]]]) -> None:
    """Say how tall and how wide each column comes out, and warn past what a
    window holds. Height is what pushes the map off the screen, and the two
    widths together are what squeeze it narrower than its own image."""
    together = 0
    for number, sections in enumerate(columns, start=1):
        height = sum(section_height(section) for section in sections)
        icons = max((len(row) for _header, rows in sections for row in rows),
                    default=0)
        together += icons * ICON_WIDTH
        note = "" if height <= COLUMN_HEIGHT_BUDGET else "  OVER BUDGET"
        print(f"column {number}  about {height:>4} px tall, {icons:>2} icons "
              f"wide, {len(sections)} sections{note}")
        if height > COLUMN_HEIGHT_BUDGET:
            print(f"  a column past {COLUMN_HEIGHT_BUDGET} px makes the map taller "
                  "than the window; rebalance the sections or add a column")
    print(f"columns   about {together} px wide together")
    if together > PANEL_WIDTH_BUDGET:
        print(f"  columns past {PANEL_WIDTH_BUDGET} px wide leave the map narrower "
              "than its own image; narrow a grid or move a section")






def render_setting_mapping(data, percentage_key_prefix: str) -> str:
    lines = ["-- Generated by tools/generate.py. slot_data key -> tracker setting.",
             "SLOT_CODES = {"]
    for key, _label, _default in BINARY_SETTINGS:
        lines.append(f"\t{key} = {{")
        lines.append(f'\t\tcode = "{key}",')
        lines.append("\t\tmapping = { [0] = 0, [1] = 1, [false] = 0, [true] = 1 },")
        lines.append("\t},")
    for key, _label, stages in STAGED_SETTINGS:
        lines.append(f"\t{key} = {{")
        lines.append(f'\t\tcode = "{key}",')
        lines.append("\t\tmapping = {")
        for index, (stage_key, _stage_label) in enumerate(stages):
            lines.append(f'\t\t\t["{stage_key}"] = {index}, [{index}] = {index},')
        lines.append("\t\t},")
        lines.append("\t},")
    lines.append("}")
    lines.append("")
    lines.append("-- Set-valued slot_data keys: member key -> the setting it turns on.")
    lines.append("SLOT_SET_CODES = {")
    for slot_key, prefix, _label in SET_SETTINGS:
        lines.append(f"\t{slot_key} = {{")
        lines.extend(f'\t\t["{member}"] = "{prefix}_{member}",'
                     for member in sorted(lock_keys_for(slot_key, data)))
        lines.append("\t},")
    lines.append("}")
    lines.append("")
    lines.append("-- The game's own completion percentage: the item its number is")
    lines.append("-- drawn on, and the AP data store key the client publishes it")
    lines.append("-- under, one per team and slot.")
    lines.append(f'PERCENTAGE_CODE = "{item_code(PERCENTAGE_ITEM)}"')
    lines.append(f'PERCENTAGE_KEY_PREFIX = "{percentage_key_prefix}"')
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
