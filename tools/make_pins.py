"""Draw the pin art, one colour per check class.

Vice City is one map carrying every class at once, so the pins have to be
readable as classes at a glance. Each class gets a filled rounded square in its
own colour for an open check and the same shape hollowed out for a cleared one.
Drawn rather than taken from the game, so the pins are the pack's own art and
stay legible at map scale.

The palette matches the class order in tools/generate.py, and the settings
panel's on and off squares come out of the same pass so every toggle has art.

Usage:
    py -3.12 tools/make_pins.py

Needs Pillow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PACK = Path(__file__).resolve().parent.parent
PIN_DIRECTORY = PACK / "images" / "items" / "pins"
SETTINGS_DIRECTORY = PACK / "images" / "items" / "settings"

# Drawn well above the 14 pixel map size so the pin stays clean when scaled.
CANVAS = 64
INSET = 4
CORNER = 12
OUTLINE = 6

CLASS_COLOURS = {
    "story_missions": (255, 210, 74),
    "properties": (10, 132, 255),
    "hidden_packages": (255, 74, 210),
    "rampages": (255, 59, 48),
    "stunt_jumps": (52, 199, 89),
    "robbable_stores": (255, 149, 0),
    "side_events": (175, 82, 222),
    "emergency_vehicles": (90, 200, 250),
}

EDGE = (24, 24, 28, 255)
SETTING_ON = (52, 199, 89)
SETTING_OFF = (90, 90, 96)


def rounded(colour: tuple[int, int, int], filled: bool) -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = (INSET, INSET, CANVAS - INSET - 1, CANVAS - INSET - 1)
    if filled:
        draw.rounded_rectangle(box, radius=CORNER, fill=(*colour, 255),
                               outline=EDGE, width=3)
    else:
        draw.rounded_rectangle(box, radius=CORNER, fill=(0, 0, 0, 0),
                               outline=(*colour, 255), width=OUTLINE)
    return image


def main() -> int:
    PIN_DIRECTORY.mkdir(parents=True, exist_ok=True)
    SETTINGS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for class_key, colour in CLASS_COLOURS.items():
        rounded(colour, filled=True).save(PIN_DIRECTORY / f"{class_key}.png")
        rounded(colour, filled=False).save(PIN_DIRECTORY / f"{class_key}_opened.png")
    print(f"wrote {2 * len(CLASS_COLOURS)} pin images to {PIN_DIRECTORY}")

    # Every setting stage picture items.json asks for. An off stage draws
    # hollow and grey, anything else filled and green, so a toggle reads at a
    # glance and a staged setting still has art for each stage.
    stems = read_setting_image_stems()
    for stem in stems:
        if stem.endswith("_off"):
            rounded(SETTING_OFF, filled=False).save(SETTINGS_DIRECTORY / f"{stem}.png")
        else:
            rounded(SETTING_ON, filled=True).save(SETTINGS_DIRECTORY / f"{stem}.png")
    print(f"wrote {len(stems)} setting images to {SETTINGS_DIRECTORY}")
    return 0


def read_setting_image_stems() -> list[str]:
    """The setting image names items.json asks for, so the art always matches
    what the generator emitted rather than a second list kept in step by hand."""
    import json
    items_path = PACK / "items" / "items.json"
    if not items_path.is_file():
        print("items/items.json is missing; run tools/generate.py first")
        return []
    stems: set[str] = set()
    for entry in json.loads(items_path.read_text(encoding="utf-8")):
        for stage in entry.get("stages", []):
            image = Path(stage.get("img", ""))
            if image.parent.name == "settings":
                stems.add(image.stem)
    return sorted(stems)


if __name__ == "__main__":
    sys.exit(main())
