"""Draw the item icons the game has no art for.

Vice City draws its weapon and vehicle icons as models rather than sprites, so
there is no sheet to lift them from: the only usable pieces in its textures are a
banknote, a radar disc and a couple of flames. Everything else here is drawn, in
the same flat style as the map pins, so the panel reads as one set rather than a
mix of borrowed textures at different sizes and eras.

Each icon is a white glyph on a rounded tile, the tile colour saying what family
the item belongs to and the glyph saying which one it is. Without this the
package rewards, the emergency rewards and the minimap all fall back to the same
marker and a panel of eleven identical icons says nothing.

    py -3.12 tools/make_icons.py

Writes images/items/drawn/<name>.png. Needs Pillow.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PACK = Path(__file__).resolve().parent.parent
DIRECTORY = PACK / "images" / "items" / "drawn"

# Drawn well above the size the panel shows them at, so they stay clean scaled.
CANVAS = 96
INSET = 6
CORNER = 16
EDGE = (24, 24, 28, 255)
GLYPH = (255, 255, 255, 255)

# Tile colour per family, so a glance at the panel groups them before the glyph
# is read at all.
PACKAGE_REWARD = (196, 122, 40)
EMERGENCY_REWARD = (32, 150, 140)
MINIMAP = (70, 110, 190)


def tile(colour: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([INSET, INSET, CANVAS - INSET - 1, CANVAS - INSET - 1],
                           radius=CORNER, fill=(*colour, 255), outline=EDGE, width=3)
    return image, draw


def shield(draw: ImageDraw.ImageDraw, top: int = 22, bottom: int = 76) -> None:
    middle = CANVAS // 2
    draw.polygon([(middle, top), (middle + 22, top + 10), (middle + 18, bottom - 14),
                  (middle, bottom), (middle - 18, bottom - 14), (middle - 22, top + 10)],
                 fill=GLYPH)


def flame(draw: ImageDraw.ImageDraw, colour=GLYPH, scale: float = 1.0,
          shift: int = 0) -> None:
    """A teardrop flame, drawn about the middle so it can sit inside a shield."""
    middle = CANVAS // 2
    points = [(0, -29), (16, -7), (10, 1), (18, 11), (12, 25), (0, 29),
              (-12, 25), (-18, 11), (-10, 1), (-16, -7)]
    draw.polygon([(middle + x * scale, middle + shift + y * scale) for x, y in points],
                 fill=colour)


def cross(draw: ImageDraw.ImageDraw) -> None:
    middle = CANVAS // 2
    draw.rectangle([middle - 9, 24, middle + 9, 72], fill=GLYPH)
    draw.rectangle([24, middle - 9, 72, middle + 9], fill=GLYPH)


def chevrons(draw: ImageDraw.ImageDraw) -> None:
    for offset in (-18, 0, 18):
        draw.polygon([(36 + offset, 26), (54 + offset, 48), (36 + offset, 70),
                      (28 + offset, 70), (46 + offset, 48), (28 + offset, 26)],
                     fill=GLYPH)


def pistol(draw: ImageDraw.ImageDraw) -> None:
    draw.polygon([(24, 38), (72, 38), (72, 50), (56, 50), (50, 62), (38, 74),
                  (28, 74), (36, 56), (24, 56)], fill=GLYPH)


def chainsaw(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle([22, 40, 46, 62], radius=5, fill=GLYPH)
    draw.polygon([(46, 44), (78, 46), (78, 56), (46, 58)], fill=GLYPH)
    for x in range(50, 76, 7):
        draw.polygon([(x, 44), (x + 4, 38), (x + 6, 44)], fill=GLYPH)


def scope(draw: ImageDraw.ImageDraw) -> None:
    middle = CANVAS // 2
    draw.ellipse([26, 26, 70, 70], outline=GLYPH, width=7)
    draw.line([middle, 16, middle, 34], fill=GLYPH, width=6)
    draw.line([middle, 62, middle, 80], fill=GLYPH, width=6)
    draw.line([16, middle, 34, middle], fill=GLYPH, width=6)
    draw.line([62, middle, 80, middle], fill=GLYPH, width=6)


def minigun(draw: ImageDraw.ImageDraw) -> None:
    """The barrel cluster head on, which is what reads as a minigun at this size."""
    middle = CANVAS // 2
    draw.ellipse([24, 24, 72, 72], outline=GLYPH, width=6)
    for step in range(6):
        angle = math.radians(step * 60)
        x = middle + 15 * math.cos(angle)
        y = middle + 15 * math.sin(angle)
        draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=GLYPH)


def rocket(draw: ImageDraw.ImageDraw) -> None:
    draw.polygon([(48, 18), (60, 40), (60, 66), (36, 66), (36, 40)], fill=GLYPH)
    draw.polygon([(36, 58), (24, 78), (36, 72)], fill=GLYPH)
    draw.polygon([(60, 58), (72, 78), (60, 72)], fill=GLYPH)


def helicopter(draw: ImageDraw.ImageDraw, gunship: bool = False) -> None:
    draw.line([16, 30, 80, 30], fill=GLYPH, width=6)
    draw.line([48, 30, 48, 40], fill=GLYPH, width=5)
    draw.ellipse([28, 38, 64, 64], fill=GLYPH)
    draw.polygon([(62, 44), (84, 50), (84, 56), (62, 56)], fill=GLYPH)
    draw.line([80, 42, 80, 62], fill=GLYPH, width=5)
    if gunship:
        draw.rectangle([26, 62, 40, 68], fill=GLYPH)
        draw.rectangle([52, 62, 66, 68], fill=GLYPH)


def tank(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle([18, 54, 82, 74], radius=8, fill=GLYPH)
    draw.rounded_rectangle([32, 36, 66, 54], radius=5, fill=GLYPH)
    draw.rectangle([64, 42, 88, 48], fill=GLYPH)


def banknote(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle([20, 34, 76, 66], radius=5, fill=GLYPH)
    draw.ellipse([40, 42, 56, 58], outline=EDGE, width=4)
    draw.line([48, 38, 48, 62], fill=EDGE, width=4)


def jump(draw: ImageDraw.ImageDraw) -> None:
    draw.polygon([(48, 18), (68, 44), (56, 44), (56, 58), (40, 58), (40, 44), (28, 44)],
                 fill=GLYPH)
    draw.rounded_rectangle([22, 66, 74, 78], radius=4, fill=GLYPH)


def radar(draw: ImageDraw.ImageDraw) -> None:
    draw.ellipse([22, 22, 74, 74], outline=GLYPH, width=6)
    draw.ellipse([38, 38, 58, 58], outline=GLYPH, width=4)
    draw.polygon([(48, 12), (54, 26), (42, 26)], fill=GLYPH)


def fireproof(draw: ImageDraw.ImageDraw) -> None:
    shield(draw)
    flame(draw, colour=(*EMERGENCY_REWARD, 255), scale=0.52, shift=2)


def armour_plus(draw: ImageDraw.ImageDraw) -> None:
    shield(draw)
    middle = CANVAS // 2
    draw.rectangle([middle - 5, 36, middle + 5, 62], fill=(*EMERGENCY_REWARD, 255))
    draw.rectangle([middle - 14, 44, middle + 14, 54], fill=(*EMERGENCY_REWARD, 255))


# Icon file name -> (tile colour, glyph). The names are what generate.py asks for.
ICONS = {
    "body_armor": (PACKAGE_REWARD, shield),
    "chainsaw": (PACKAGE_REWARD, chainsaw),
    "pistol": (PACKAGE_REWARD, pistol),
    "flamethrower": (PACKAGE_REWARD, flame),
    "sniper": (PACKAGE_REWARD, scope),
    "minigun": (PACKAGE_REWARD, minigun),
    "rocket_launcher": (PACKAGE_REWARD, rocket),
    "sea_sparrow": (PACKAGE_REWARD, helicopter),
    "hunter": (PACKAGE_REWARD, lambda draw: helicopter(draw, gunship=True)),
    "rhino": (PACKAGE_REWARD, tank),
    "money": (PACKAGE_REWARD, banknote),
    "sprint": (EMERGENCY_REWARD, chevrons),
    "fireproof": (EMERGENCY_REWARD, fireproof),
    "max_armor": (EMERGENCY_REWARD, armour_plus),
    "taxi_jump": (EMERGENCY_REWARD, jump),
    "max_health": (EMERGENCY_REWARD, cross),
    "minimap": (MINIMAP, radar),
}


def main() -> int:
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, (colour, glyph) in ICONS.items():
        image, draw = tile(colour)
        glyph(draw)
        image.save(DIRECTORY / f"{name}.png")
    print(f"wrote {len(ICONS)} icons to {DIRECTORY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
