"""Turn positions clicked on the map image into game world coordinates.

Every coordinate table in the pack is in world units, but the easiest way to
find a place by hand is to open images/maps/vice_city.png and read the pixel off
an image editor. This converts, using the same data/map_geometry.json the
generator pins from, so the two can never disagree about where a pixel is.

Reads pixel pairs from the command line or from a file, one "x y" or "x,y" per
line, and prints the world coordinates as a block ready to paste into
data/check_coords.py. Blank lines and lines starting with # are ignored, so a
file can be annotated while it is built up.

    py -3.12 tools/pixel_to_world.py 512 300 640 210
    py -3.12 tools/pixel_to_world.py --from jumps.txt
    py -3.12 tools/pixel_to_world.py --from jumps.txt --write
    py -3.12 tools/pixel_to_world.py --from jumps.txt --preview check.png
    py -3.12 tools/pixel_to_world.py --reverse -1035.5 1247.0

--write replaces the STUNT_JUMP_COORDS list in data/check_coords.py in place.
--preview draws the points on the map so they can be checked before pasting.
--reverse goes the other way, world to pixel, to see where a coordinate lands.

Needs Pillow only for --preview.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
GEOMETRY = PACK / "data" / "map_geometry.json"
CHECK_COORDS = PACK / "data" / "check_coords.py"
STUNT_JUMP_TABLE = "STUNT_JUMP_COORDS"
# The pins only use x and y, so a hand-placed point carries no height.
PLACEHOLDER_HEIGHT = 0.0
SEPARATOR = re.compile(r"[,\s]+")


class Geometry:
    """The map's world-to-pixel transform, as tools/extract_map.py wrote it."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise SystemExit(f"{path} is missing; run tools/extract_map.py first")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.width = data["width"]
        self.height = data["height"]
        self.world_left = data["world_left"]
        self.world_top = data["world_top"]
        self.units_per_pixel = data["units_per_pixel"]

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.world_left + x * self.units_per_pixel,
            self.world_top - y * self.units_per_pixel,
        )

    def to_pixel(self, x: float, y: float) -> tuple[int, int]:
        return (
            round((x - self.world_left) / self.units_per_pixel),
            round((self.world_top - y) / self.units_per_pixel),
        )

    def holds(self, x: float, y: float) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height


def read_pairs(fields: list[str]) -> list[tuple[float, float]]:
    if len(fields) % 2 != 0:
        raise SystemExit(f"{len(fields)} numbers given; coordinates come in pairs")
    numbers = []
    for field in fields:
        try:
            numbers.append(float(field))
        except ValueError:
            raise SystemExit(f"not a number: {field}") from None
    return list(zip(numbers[0::2], numbers[1::2], strict=False))


def read_pairs_from(path: str) -> list[tuple[float, float]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    fields: list[str] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = [part for part in SEPARATOR.split(stripped) if part]
        if len(parts) != 2:
            raise SystemExit(f"{path} line {number}: expected two numbers, got {len(parts)}")
        fields += parts
    return read_pairs(fields)


def render_block(points: list[tuple[float, float]]) -> str:
    rows = "\n".join(
        f"    ({x:.1f}, {y:.1f}, {PLACEHOLDER_HEIGHT})," for x, y in points)
    return (f"{STUNT_JUMP_TABLE}: list[tuple[float, float, float]] = [\n"
            f"{rows}\n]")


def write_table(points: list[tuple[float, float]]) -> None:
    """Replace the stunt jump list in check_coords.py, leaving the rest alone."""
    source = CHECK_COORDS.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^{STUNT_JUMP_TABLE}: list\[tuple\[float, float, float\]\] = \[.*?^\]",
        re.DOTALL | re.MULTILINE)
    if not pattern.search(source):
        raise SystemExit(f"no {STUNT_JUMP_TABLE} list found in {CHECK_COORDS}")
    CHECK_COORDS.write_text(pattern.sub(lambda _match: render_block(points), source, count=1),
                            encoding="utf-8")
    print(f"wrote {len(points)} positions into {CHECK_COORDS}")


def draw_preview(geometry: Geometry, points: list[tuple[float, float]],
                 destination: str) -> None:
    from PIL import Image, ImageDraw
    image_path = PACK / "images" / "maps" / "vice_city.png"
    if not image_path.is_file():
        raise SystemExit(f"{image_path} is missing; run tools/extract_map.py first")
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    for index, (x, y) in enumerate(points, start=1):
        pixel_x, pixel_y = geometry.to_pixel(x, y)
        draw.ellipse([pixel_x - 6, pixel_y - 6, pixel_x + 6, pixel_y + 6],
                     fill=(52, 199, 89), outline=(20, 20, 24), width=2)
        draw.text((pixel_x + 8, pixel_y - 6), str(index), fill=(20, 20, 24))
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    print(f"wrote {destination}")


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments:
        print(__doc__)
        return 2
    geometry = Geometry(GEOMETRY)

    reverse = "--reverse" in arguments
    write = "--write" in arguments
    preview = None
    if "--preview" in arguments:
        index = arguments.index("--preview")
        if index + 1 >= len(arguments):
            raise SystemExit("--preview needs a file to write")
        preview = arguments[index + 1]
        del arguments[index:index + 2]
    source_file = None
    if "--from" in arguments:
        index = arguments.index("--from")
        if index + 1 >= len(arguments):
            raise SystemExit("--from needs a file to read")
        source_file = arguments[index + 1]
        del arguments[index:index + 2]
    fields = [argument for argument in arguments
              if argument not in ("--reverse", "--write")]

    pairs = read_pairs_from(source_file) if source_file else read_pairs(fields)
    if not pairs:
        raise SystemExit("no coordinates given")

    if reverse:
        print(f"# map is {geometry.width} x {geometry.height} pixels")
        for x, y in pairs:
            pixel_x, pixel_y = geometry.to_pixel(x, y)
            outside = "" if geometry.holds(pixel_x, pixel_y) else "   OUTSIDE THE MAP"
            print(f"world ({x:9.1f}, {y:9.1f})  ->  pixel ({pixel_x:5}, {pixel_y:5}){outside}")
        return 0

    outside = [pair for pair in pairs if not geometry.holds(*pair)]
    if outside:
        print(f"warning: {len(outside)} point(s) are outside the "
              f"{geometry.width} x {geometry.height} image: {outside}", file=sys.stderr)
    points = [geometry.to_world(x, y) for x, y in pairs]

    if preview:
        draw_preview(geometry, points, preview)
    if write:
        write_table(points)
        if len(points) != 36:
            print(f"note: {len(points)} positions written, and the generator needs "
                  "all 36 before it will pin the class", file=sys.stderr)
    if not write:
        print(render_block(points))
    return 0


if __name__ == "__main__":
    sys.exit(main())
