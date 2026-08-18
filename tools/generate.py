"""Regenerate the bulk PopTracker pack files from the GTA Vice City apworld.

Everything the pack knows about checks, items, and logic is derived here, so
the tracker cannot drift from the world it tracks. Emits:

    items/items.json
    locations/<Check class>.json
    maps/maps.json
    scripts/locations_import.lua
    scripts/autotracking/item_mapping.lua
    scripts/autotracking/location_mapping.lua
    scripts/autotracking/setting_mapping.lua
    scripts/logic/access_rules.lua

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
        "chest_unopened_img": f"images/items/pins/{class_key}.png",
        "chest_opened_img": f"images/items/pins/{class_key}_opened.png",
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
    ("shuffle_emergency_rewards", "Shuffle emergency rewards", False),
    ("randomize_radio_stations", "Randomize radio stations", False),
    ("shuffle_minimap", "Shuffle minimap", False),
    ("randomize_pickups", "Randomize pickups", False),
    ("death_link", "Death Link", False),
]

STAGED_SETTINGS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("goal", "Goal", [
        ("final_mission", "Final mission"),
        ("hidden_packages", "Hidden package hunt"),
        ("hundred_percent", "100 percent"),
    ]),
]

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
# reads like the map does.
STRAND_ICONS = {
    "Rosenberg": "radar_lawyer", "Cortez": "radar_cortez", "Diaz": "radar_diaz",
    "Death Row": "radar_kent", "Avery": "radar_avery", "Phil Cassidy": "radar_phil",
    "Vercetti Protection": "tommy", "Big Mitch Baker": "bikers",
    "Umberto Robina": "cubans", "Auntie Poulet": "haitians",
    "Love Fist": "lovefist", "Mr. Black": "phone",
    "Vercetti Finale": "radar_centre", "Malibu Club": "club",
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


def load_check_coords():
    sys.path.insert(0, str(PACK / "data"))
    import check_coords
    return check_coords


# ---------------------------------------------------------------------------
# Requirement capture
# ---------------------------------------------------------------------------

# A captured rule is ("all", requirements) or
# ("threshold", requirements, optional_requirement_sets, needed).
Rule = tuple


def capture_rules(rules, properties_enabled: bool, ability_locks: frozenset[str],
                  content_locks: frozenset[str]) -> dict[str, Rule]:
    """The world's own location rules as requirement structures.

    build_location_rules returns predicates built by exactly two helpers, so
    standing a recorder in for both hands back the structures instead, with no
    second copy of the requirement logic to drift.
    """
    original_requires = rules._requires
    original_threshold = rules._requires_with_asset_threshold
    try:
        rules._requires = lambda requirements: ("all", list(requirements))
        rules._requires_with_asset_threshold = (
            lambda requirements, optional, needed: (
                "threshold", list(requirements), [list(each) for each in optional], needed)
        )
        return rules.build_location_rules(
            properties_enabled=properties_enabled,
            ability_locks=ability_locks,
            content_locks=content_locks,
        )
    finally:
        rules._requires = original_requires
        rules._requires_with_asset_threshold = original_threshold


# ---------------------------------------------------------------------------
# Lua rendering
# ---------------------------------------------------------------------------

SLUG = re.compile(r"[^A-Za-z0-9]+")


def slug(text: str) -> str:
    return SLUG.sub("_", text).strip("_")


def lua_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    has to read as satisfied.
    """
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


def rule_expression(rule: Rule | None, region_term: str | None,
                    lock_settings: dict[str, str]) -> str:
    """A location's full access expression: its region entry and its rule."""
    terms: list[str] = []
    if region_term is not None:
        terms.append(region_term)
    if rule is None:
        return conjunction(terms)
    if rule[0] == "all":
        terms.extend(requirement_term(item, count, lock_settings)
                     for item, count in deduplicated(rule[1]))
        return conjunction(terms)
    _kind, requirements, optional, needed = rule
    terms.extend(requirement_term(item, count, lock_settings)
                 for item, count in deduplicated(requirements))
    clauses = ", ".join(
        conjunction([requirement_term(item, count, lock_settings)
                     for item, count in deduplicated(each)])
        for each in optional
    )
    terms.append(f"satisfiedCount({{{clauses}}}) >= {needed}")
    return conjunction(terms)


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
    if name in data.CONTENT_ITEMS:
        return f"{ICON_DIRECTORY}/radar_save.png"
    if name in data.TRAP_ITEMS:
        return f"{ICON_DIRECTORY}/siterocket.png"
    if name == data.PACKAGE_FRAGMENT_ITEM:
        return f"{ICON_DIRECTORY}/tshirt.png"
    if name in items.GENERAL_FILLER_NAMES or name in data.FILLER_ITEMS:
        return f"{ICON_DIRECTORY}/gun.png"
    return f"{ICON_DIRECTORY}/{FALLBACK_ICON}.png"


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


def node_name(members: list[str], locations) -> str:
    """The pin's name. Checks sharing a pixel share a pin, and when they are one
    strand's missions the strand names it; otherwise the members do."""
    if len(members) == 1:
        return members[0]
    strands = {locations.MISSION_GIVER.get(member) for member in members}
    if len(strands) == 1 and None not in strands:
        return strands.pop()
    return " & ".join(members)


def cluster(named_pixels: list[tuple[str, tuple[int, int]]],
            ) -> list[tuple[tuple[int, int], list[str]]]:
    """Group checks whose pins would sit on top of each other.

    Greedy by distance: a check joins the first cluster it is within
    MERGE_DISTANCE_PIXELS of, and the marker then sits at the middle of its
    members. This is what turns a giver's whole strand, given from one spot,
    into one marker, and what stops two packages a few metres apart from
    covering each other.
    """
    clusters: list[list[tuple[str, tuple[int, int]]]] = []
    for name, (x, y) in named_pixels:
        for members in clusters:
            first_x, first_y = members[0][1]
            if abs(x - first_x) <= MERGE_DISTANCE_PIXELS \
                    and abs(y - first_y) <= MERGE_DISTANCE_PIXELS:
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

    groups: dict[str, list[dict]] = {}
    for class_key, display, _option in CHECK_CLASSES:
        members = by_class.get(class_key, [])
        if not members:
            continue
        nodes: list[dict] = []

        pinned = [(name, geometry.pixel(*positions[name]))
                  for name in members if name in positions]
        for (x, y), shared in cluster(pinned):
            nodes.append({
                "name": node_name(shared, locations),
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
    return groups


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
    check_coords = load_check_coords()

    geometry = Geometry(MAP_GEOMETRY)
    positions = check_positions(data, locations, check_coords,
                                unpinnable_classes(check_coords))
    groups = build_locations(data, locations, positions, geometry)
    attach_access_rules(groups)

    # ---- items/items.json and the panels showing them --------------------
    write_json(PACK / "items" / "items.json", build_items_json(data, items))
    write_json(PACK / "layouts" / "items.json", build_items_layout(data, items))

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
               render_setting_mapping(data))

    # ---- scripts/logic/access_rules.lua ---------------------------------
    # A lock item -> the setting whose on stage means its key was selected. The
    # two families have their own settings, so a term names exactly one.
    lock_settings = {
        **{item: f"ability_lock_{key}" for item, key in data.ABILITY_ITEM_KEY.items()},
        **{item: f"content_lock_{key}" for item, key in data.CONTENT_ITEM_KEY.items()},
    }
    all_ability_locks = frozenset(data.ABILITY_LOCK_ITEMS)
    all_content_locks = frozenset(data.CONTENT_LOCK_ITEMS)
    with_properties = capture_rules(rules, True, all_ability_locks, all_content_locks)
    without_properties = capture_rules(rules, False, all_ability_locks, all_content_locks)
    region_terms = {
        region: (None if region == data.REGION_VICE_CITY
                 else f"has({lua_string(item_code(data.AREA_ITEM_BY_REGION[region]))})")
        for region in {data.REGION_VICE_CITY, data.REGION_MAINLAND, data.REGION_STARFISH}
    }

    lines = [
        "-- Generated by tools/generate.py from the GTA Vice City apworld.",
        "-- One function per AP location returning its PopTracker AccessibilityLevel:",
        "-- its region entry ANDed with the world's own requirements for it. A rule",
        "-- that differs between the properties class being on and off carries both",
        "-- and switches on the seed's own setting.",
        "",
    ]
    for name in locations.LOCATION_NAME_TO_ID:
        region_term = region_terms[locations.LOCATION_REGIONS[name]]
        on = rule_expression(with_properties.get(name), region_term, lock_settings)
        off = rule_expression(without_properties.get(name), region_term, lock_settings)
        function = f"rule_{slug(name)}"
        if on == off:
            lines.append(f"function {function}() return reachAccess({on}) end")
        else:
            lines.append(f"function {function}()")
            lines.append("\tif propertiesEnabled() then")
            lines.append(f"\t\treturn reachAccess({on})")
            lines.append("\telse")
            lines.append(f"\t\treturn reachAccess({off})")
            lines.append("\tend")
            lines.append("end")
    write_text(PACK / "scripts" / "logic" / "access_rules.lua", "\n".join(lines) + "\n")

    pinned = sum(1 for name in locations.LOCATION_NAME_TO_ID if name in positions)
    print(f"items      {len(items.ITEM_NAME_TO_ID):>4}")
    print(f"locations  {len(locations.LOCATION_NAME_TO_ID):>4} "
          f"({pinned} pinned, {len(locations.LOCATION_NAME_TO_ID) - pinned} listed)")
    print(f"pins       {sum(1 for nodes in groups.values() for node in nodes if 'map_locations' in node):>4}")
    for _class_key, display, _option in CHECK_CLASSES:
        nodes = groups.get(display, [])
        sections = sum(len(node["sections"]) for node in nodes)
        print(f"  {display:<28} {sections:>4} checks in {len(nodes):>3} nodes")
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


def build_items_layout(data, items) -> dict:
    """The two panels beside the map.

    Generated alongside items.json so a renamed item cannot leave a dead code
    behind in a hand-written layout. Cash, filler, and traps are left out: they
    gate nothing and would bury the items that do.

    One column holds what the player has, the other what the seed was rolled
    with, so each column reads as one thing. Two columns rather than one because
    PopTracker lays the window out to whatever height its tallest column needs
    and fits the map into that: a column taller than the window makes the map
    taller than the window too.
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

    owned_sections = [
        ("Area access", wrap(list(data.AREA_ITEMS), 2)),
        ("Goal", wrap([data.PACKAGE_FRAGMENT_ITEM], 1)),
        ("Story strands", wrap(story, 7)),
        ("Venue strands", wrap(venues, 7)),
        ("Property ownership", wrap(list(data.PROPERTY_OWNERSHIP_ITEMS), 8)),
        ("Abilities", wrap(list(data.ABILITY_ITEMS), 8)),
        ("Content locks", wrap(list(data.CONTENT_ITEMS), 5)),
        ("Package rewards", wrap(list(data.PACKAGE_REWARD_ITEMS), 6)),
        ("Emergency rewards", wrap(list(data.EMERGENCY_REWARD_ITEMS), 5)),
        ("Radio and minimap",
         wrap([*data.RADIO_STATION_ITEMS, data.MINIMAP_ITEM], 5)),
    ]
    seed_sections = [
        ("Seed options", wrap(settings_codes, 7)),
        ("Locks selected", wrap(lock_codes, 6)),
        ("Show on map", wrap(display_codes, 8)),
    ]
    report_column_heights(owned_sections, seed_sections)
    return {
        # One column of everything, for the broadcast window, which is its own
        # narrow thing and has no map to sit beside.
        "items": column_layout(owned_sections + seed_sections),
        "panel_one": column_layout(owned_sections),
        "panel_two": column_layout(seed_sections),
    }


def column_layout(sections: list[tuple[str, list[list[str]]]]) -> dict:
    return {
        "type": "array",
        "orientation": "vertical",
        "content": [
            {"type": "group", "header": header, "content": item_grid(rows)}
            for header, rows in sections
        ],
    }


# Rough heights, for the warning below only: one row of icons, and the group
# header above it, in the proportions PopTracker draws them.
ROW_HEIGHT = 38
HEADER_HEIGHT = 30
# About what a 1080p window leaves for a column once its own furniture is out of
# the way. A column past this makes the map taller than the window.
COLUMN_HEIGHT_BUDGET = 950


def section_height(section: tuple[str, list[list[str]]]) -> int:
    _header, rows = section
    return len(rows) * ROW_HEIGHT + HEADER_HEIGHT


def report_column_heights(*columns: list[tuple[str, list[list[str]]]]) -> None:
    """Say how tall each column comes out, and warn when one grows past what a
    window holds, since that is what pushes the map off the screen."""
    for number, sections in enumerate(columns, start=1):
        height = sum(section_height(section) for section in sections)
        note = "" if height <= COLUMN_HEIGHT_BUDGET else "  OVER BUDGET"
        print(f"column {number}  about {height:>4} px tall, "
              f"{len(sections)} sections{note}")
        if height > COLUMN_HEIGHT_BUDGET:
            print(f"  a column past {COLUMN_HEIGHT_BUDGET} px makes the map taller "
                  "than the window; rebalance the sections or add a column")






def render_setting_mapping(data) -> str:
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
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
