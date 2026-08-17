"""Draw the pinned map as a picture, from the pack's own emitted pin data.

A pin that lands in the sea or on the wrong block is obvious in a picture and
invisible in a JSON diff, so this renders exactly what the pack tells PopTracker
to draw: every map_locations entry in locations/*.json, coloured by class, over
the extracted map image. Reading the emitted files rather than the coordinate
table means it checks the whole pipeline, not just its input.

Writes preview/pinned_map.png, at four times the map size so the pins separate.

Usage:
    py -3.12 tools/preview_map.py

Needs Pillow.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_pins import CLASS_COLOURS

PACK = Path(__file__).resolve().parent.parent
OUTPUT = PACK / "preview" / "pinned_map.png"
SCALE = 3

# Location file name -> the class whose colour its pins take. Mirrors the
# display names in tools/generate.py.
CLASS_BY_FILE = {
    "Story Missions": "story_missions",
    "Properties": "properties",
    "Hidden Packages": "hidden_packages",
    "Rampages": "rampages",
    "Stunt Jumps": "stunt_jumps",
    "Robbable Stores": "robbable_stores",
    "Side Events": "side_events",
    "Emergency Vehicle Missions": "emergency_vehicles",
}


def main() -> int:
    maps = json.loads((PACK / "maps" / "maps.json").read_text(encoding="utf-8"))
    # Pins are drawn at the size PopTracker will draw them, so the preview
    # shows the real crowding rather than a prettier version of it.
    pin_radius = max(round(maps[0].get("location_size", 10) * SCALE / 2), 1)
    image_path = PACK / maps[0]["img"]
    if not image_path.is_file():
        print(f"{image_path} is missing; run tools/extract_map.py first")
        return 1
    base = Image.open(image_path).convert("RGB")
    base = base.resize((base.width * SCALE, base.height * SCALE), Image.NEAREST)
    draw = ImageDraw.Draw(base)

    drawn = 0
    unpinned = 0
    for path in sorted(PACK.glob("locations/*.json")):
        colour = CLASS_COLOURS.get(CLASS_BY_FILE.get(path.stem, ""), (255, 255, 255))
        for group in json.loads(path.read_text(encoding="utf-8")):
            for node in group.get("children", []):
                pins = node.get("map_locations")
                if not pins:
                    unpinned += len(node.get("sections", []))
                    continue
                for pin in pins:
                    x, y = pin["x"] * SCALE, pin["y"] * SCALE
                    draw.rectangle(
                        [x - pin_radius, y - pin_radius, x + pin_radius, y + pin_radius],
                        fill=colour, outline=(20, 20, 24), width=2)
                    drawn += 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    base.save(OUTPUT)
    print(f"drew {drawn} pins ({unpinned} checks are listed rather than pinned)")
    print(f"wrote {OUTPUT} at {base.width}x{base.height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
