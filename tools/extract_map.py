"""Build the pack's map image from the player's own Vice City install.

The tracker pins every check at the game's own world position, so its map has
to be the game's own radar art or the pins would not line up. That art lives as
64 radar tiles inside the IMG archive, one RenderWare texture dictionary each.
This tool walks the archive, decodes the tiles, and assembles them into
images/maps/vice_city.png, plus the HUD legend icons the pins use.

Tile n sits at column n % 8, row n // 8. Column 0 starts at world x = -2000 and
row 0 at world y = +2000, each tile spanning 500 world units, so the assembled
1024 x 1024 image covers world x and y from -2000 to +2000. The constants behind
that are the game's own (0x68FD44 = 500.0 tile span and 0x68FD00 = 2000.0
origin, combined as tileIndex * 500.0 - 2000.0 by the radar's world-to-texture
transform).

Note the entity sector grid is a different thing with a different origin
(x from -2400), and using it would shift every pin 400 units east.

Most of that square is open sea, and the whole city has to fit one pane without
scrolling, so the assembly is then cropped to its own land plus a margin and
scaled down to MAP_TARGET_HEIGHT. Both steps move the world-to-pixel transform
away from the plain radar formula, so it is written out to data/map_geometry.json
and generate.py places every pin from that rather than from constants of its own.

Usage:
    py -3.12 tools/extract_map.py "D:/path/to/Grand Theft Auto Vice City"

Needs Pillow and numpy.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy
from PIL import Image

PACK = Path(__file__).resolve().parent.parent
MAP_NAME = "Vice City"
MAP_IMAGE = "images/maps/vice_city.png"
MAP_GEOMETRY = "data/map_geometry.json"

# The radar covers world x and y from -2000 to +2000 across the assembly.
WORLD_ORIGIN = 2000.0
WORLD_SPAN = 4000.0

# How much open sea to keep around the land, in assembly pixels. The north keeps
# more because that sea is where the generator puts the pins for checks the game
# places nowhere.
LAND_MARGIN_PIXELS = 14
NORTH_MARGIN_PIXELS = 18

# The finished image is scaled to this height. PopTracker will not zoom out past
# one image pixel per screen pixel, which a display running at 125 per cent
# makes 1.25 screen pixels, so a 1080p pane holds around 800 image pixels of
# height. Raise it for detail on a taller screen, lower it if the city still
# does not fit.
MAP_TARGET_HEIGHT = 760

# The sea, as the two near-identical blues the radar tiles use, and how far a
# pixel may differ and still count as sea.
SEA_COLOURS = ((156, 202, 255), (148, 202, 255))
SEA_TOLERANCE = 12

SECTOR_SIZE = 2048
TILES_PER_AXIS = 8
DIRECTORY_ENTRY_SIZE = 32

CHUNK_TEXTURE_DICTIONARY = 0x16
CHUNK_STRUCT = 0x01
CHUNK_TEXTURE_NATIVE = 0x15
PLATFORM_DIRECT3D8 = 8
COMPRESSION_DXT1 = 1
COMPRESSION_DXT3 = 3

# The HUD texture dictionary holds the map legend icons. It sits loose beside
# the archive rather than inside it. The pack uses a few as pin art, so the
# whole dictionary is extracted and the pins reference what they need by name.
HUD_DICTIONARY = Path("models") / "hud.txd"


def read_archive_index(directory_path: Path) -> dict[str, tuple[int, int]]:
    """Entry name -> (byte offset, byte size) from an IMG version 1 directory."""
    data = directory_path.read_bytes()
    entries: dict[str, tuple[int, int]] = {}
    for index in range(len(data) // DIRECTORY_ENTRY_SIZE):
        offset, size, name = struct.unpack_from("<II24s", data, index * DIRECTORY_ENTRY_SIZE)
        key = name.split(b"\0")[0].decode("ascii").lower()
        entries[key] = (offset * SECTOR_SIZE, size * SECTOR_SIZE)
    return entries


def walk_chunks(buffer: bytes, start: int, end: int):
    """Yield (type, body offset, body size) for each RenderWare chunk in a span."""
    position = start
    while position + 12 <= end:
        chunk_type, size, _library = struct.unpack_from("<III", buffer, position)
        if chunk_type == 0 and size == 0:
            return
        yield chunk_type, position + 12, size
        position += 12 + size


def parse_texture_dictionary(blob: bytes) -> list[dict]:
    """Every native texture in a TXD, as its header fields plus its mip data."""
    textures: list[dict] = []
    for chunk_type, body, size in walk_chunks(blob, 0, len(blob)):
        if chunk_type != CHUNK_TEXTURE_DICTIONARY:
            continue
        for child_type, child_body, child_size in walk_chunks(blob, body, body + size):
            if child_type != CHUNK_TEXTURE_NATIVE:
                continue
            for sub_type, sub_body, _sub_size in walk_chunks(
                    blob, child_body, child_body + child_size):
                if sub_type != CHUNK_STRUCT:
                    continue
                platform, _filter = struct.unpack_from("<II", blob, sub_body)
                name = blob[sub_body + 8:sub_body + 40].split(b"\0")[0].decode("ascii")
                width, height = struct.unpack_from("<HH", blob, sub_body + 80)
                _depth, levels, _raster_type, compression = struct.unpack_from(
                    "<BBBB", blob, sub_body + 84)
                cursor = sub_body + 88
                mips = []
                for _level in range(levels):
                    mip_size, = struct.unpack_from("<I", blob, cursor)
                    mips.append(blob[cursor + 4:cursor + 4 + mip_size])
                    cursor += 4 + mip_size
                textures.append({
                    "platform": platform, "name": name, "width": width,
                    "height": height, "compression": compression, "mips": mips,
                })
        break
    return textures


def _unpack_565(value):
    red = ((value >> 11) & 0x1F).astype(numpy.uint16)
    green = ((value >> 5) & 0x3F).astype(numpy.uint16)
    blue = (value & 0x1F).astype(numpy.uint16)
    return (
        ((red * 255 + 15) // 31).astype(numpy.uint16),
        ((green * 255 + 31) // 63).astype(numpy.uint16),
        ((blue * 255 + 15) // 31).astype(numpy.uint16),
    )


def decode_dxt1(data: bytes, width: int, height: int):
    """DXT1 to RGBA, honoring the three-color transparent block case."""
    blocks_across, blocks_down = width // 4, height // 4
    words = numpy.frombuffer(data, dtype="<u2").reshape(blocks_down, blocks_across, 4)
    selectors_word = numpy.frombuffer(
        data, dtype="<u4").reshape(blocks_down, blocks_across, 2)[:, :, 1]
    colour0 = words[:, :, 0].astype(numpy.uint32)
    colour1 = words[:, :, 1].astype(numpy.uint32)
    red0, green0, blue0 = _unpack_565(colour0)
    red1, green1, blue1 = _unpack_565(colour1)
    opaque = numpy.full_like(red0, 255)
    four_colour = colour0 > colour1

    palette = numpy.zeros((blocks_down, blocks_across, 4, 4), dtype=numpy.uint8)
    palette[:, :, 0] = numpy.stack([red0, green0, blue0, opaque], axis=-1)
    palette[:, :, 1] = numpy.stack([red1, green1, blue1, opaque], axis=-1)
    two_thirds = numpy.stack([
        (2 * red0 + red1) // 3, (2 * green0 + green1) // 3,
        (2 * blue0 + blue1) // 3, opaque,
    ], axis=-1)
    one_third = numpy.stack([
        (red0 + 2 * red1) // 3, (green0 + 2 * green1) // 3,
        (blue0 + 2 * blue1) // 3, opaque,
    ], axis=-1)
    midpoint = numpy.stack([
        (red0 + red1) // 2, (green0 + green1) // 2, (blue0 + blue1) // 2, opaque,
    ], axis=-1)
    palette[:, :, 2] = numpy.where(four_colour[..., None], two_thirds, midpoint)
    palette[:, :, 3] = numpy.where(four_colour[..., None], one_third,
                                   numpy.zeros_like(midpoint))

    shifts = numpy.arange(16, dtype=numpy.uint32) * 2
    selectors = ((selectors_word[:, :, None] >> shifts) & 3).astype(numpy.intp)
    pixels = numpy.take_along_axis(
        palette, selectors[:, :, :, None].repeat(4, axis=3), axis=2)
    pixels = pixels.reshape(blocks_down, blocks_across, 4, 4, 4).transpose(0, 2, 1, 3, 4)
    return pixels.reshape(height, width, 4)


def decode_dxt3(data: bytes, width: int, height: int):
    """DXT3 to RGBA: a four-bit explicit alpha block ahead of a DXT1 color block."""
    blocks_across, blocks_down = width // 4, height // 4
    raw = numpy.frombuffer(data, dtype=numpy.uint8).reshape(blocks_down, blocks_across, 16)
    colour_blocks = numpy.ascontiguousarray(raw[:, :, 8:]).tobytes()
    rgba = decode_dxt1(colour_blocks, width, height)
    alpha_words = numpy.frombuffer(
        numpy.ascontiguousarray(raw[:, :, :8]).tobytes(), dtype="<u8"
    ).reshape(blocks_down, blocks_across)
    shifts = numpy.arange(16, dtype=numpy.uint64) * 4
    nibbles = ((alpha_words[:, :, None] >> shifts) & 0xF).astype(numpy.uint8) * 17
    alpha = nibbles.reshape(blocks_down, blocks_across, 4, 4).transpose(0, 2, 1, 3)
    rgba[:, :, 3] = alpha.reshape(height, width)
    return rgba


def decode(texture: dict):
    if texture["compression"] == COMPRESSION_DXT1:
        return decode_dxt1(texture["mips"][0], texture["width"], texture["height"])
    if texture["compression"] == COMPRESSION_DXT3:
        return decode_dxt3(texture["mips"][0], texture["width"], texture["height"])
    raise ValueError(f"{texture['name']}: unsupported compression {texture['compression']}")


def read_entry(archive: Path, index: dict[str, tuple[int, int]], name: str) -> bytes:
    offset, size = index[name]
    with archive.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)


def assemble_radar_map(archive: Path, index: dict[str, tuple[int, int]]) -> Image.Image:
    tile_size = None
    atlas = None
    with archive.open("rb") as handle:
        for tile in range(TILES_PER_AXIS * TILES_PER_AXIS):
            entry = f"radar{tile:02d}.txd"
            offset, size = index[entry]
            handle.seek(offset)
            textures = parse_texture_dictionary(handle.read(size))
            if len(textures) != 1:
                raise SystemExit(f"{entry}: expected one texture, found {len(textures)}")
            texture = textures[0]
            if texture["platform"] != PLATFORM_DIRECT3D8:
                raise SystemExit(f"{entry}: platform {texture['platform']}, expected Direct3D 8")
            if tile_size is None:
                tile_size = texture["width"]
                atlas = Image.new("RGBA", (TILES_PER_AXIS * tile_size,) * 2)
            image = Image.fromarray(decode(texture), "RGBA")
            atlas.paste(image, ((tile % TILES_PER_AXIS) * tile_size,
                                (tile // TILES_PER_AXIS) * tile_size))
    return atlas


def extract_hud_icons(dictionary: Path, destination: Path) -> int:
    if not dictionary.is_file():
        print(f"{dictionary} not found; skipping the legend icons")
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    for texture in parse_texture_dictionary(dictionary.read_bytes()):
        try:
            rgba = decode(texture)
        except ValueError as problem:
            print(f"skipping {texture['name']}: {problem}")
            continue
        Image.fromarray(rgba, "RGBA").save(destination / f"{texture['name']}.png")
        written += 1
    return written


def land_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    """The box holding everything that is not open sea, in image pixels."""
    pixels = numpy.array(image.convert("RGB")).astype(int)
    sea = numpy.zeros(pixels.shape[:2], dtype=bool)
    for colour in SEA_COLOURS:
        sea |= numpy.abs(pixels - numpy.array(colour)).sum(axis=-1) <= SEA_TOLERANCE
    land = ~sea
    rows = numpy.where(land.any(axis=1))[0]
    columns = numpy.where(land.any(axis=0))[0]
    if not len(rows) or not len(columns):
        raise SystemExit("the assembled map is all sea; the tiles did not decode")
    return int(columns.min()), int(rows.min()), int(columns.max()), int(rows.max())


def crop_and_scale(atlas: Image.Image) -> tuple[Image.Image, dict]:
    """Trim the open sea around the city, scale to fit a pane, and describe the
    world-to-pixel transform that leaves."""
    assembly_units_per_pixel = WORLD_SPAN / atlas.width
    left, top, right, bottom = land_bounds(atlas)
    box = (
        max(left - LAND_MARGIN_PIXELS, 0),
        max(top - NORTH_MARGIN_PIXELS, 0),
        min(right + LAND_MARGIN_PIXELS + 1, atlas.width),
        min(bottom + LAND_MARGIN_PIXELS + 1, atlas.height),
    )
    cropped = atlas.crop(box)
    scale = min(MAP_TARGET_HEIGHT / cropped.height, 1.0)
    width = max(round(cropped.width * scale), 1)
    height = max(round(cropped.height * scale), 1)
    scaled = cropped.resize((width, height), Image.LANCZOS)
    units_per_pixel = assembly_units_per_pixel * cropped.height / height
    geometry = {
        "name": MAP_NAME,
        "image": MAP_IMAGE,
        "width": width,
        "height": height,
        # The world position of the top left pixel, and how much world a pixel
        # spans. A pin is (worldX - world_left) / units_per_pixel across and
        # (world_top - worldY) / units_per_pixel down.
        "world_left": box[0] * assembly_units_per_pixel - WORLD_ORIGIN,
        "world_top": WORLD_ORIGIN - box[1] * assembly_units_per_pixel,
        "units_per_pixel": units_per_pixel,
    }
    return scaled, geometry


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    game = Path(sys.argv[1])
    archive = game / "models" / "gta3.img"
    directory = game / "models" / "gta3.dir"
    for required in (archive, directory):
        if not required.is_file():
            print(f"not found: {required}")
            return 1

    index = read_archive_index(directory)
    atlas = assemble_radar_map(archive, index)
    trimmed, geometry = crop_and_scale(atlas)
    map_path = PACK / MAP_IMAGE
    map_path.parent.mkdir(parents=True, exist_ok=True)
    trimmed.convert("RGB").save(map_path)
    geometry_path = PACK / MAP_GEOMETRY
    geometry_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_path.write_text(json.dumps(geometry, indent="\t") + "\n", encoding="utf-8")
    print(f"assembled {atlas.size[0]}x{atlas.size[1]}, "
          f"wrote {map_path} at {geometry['width']}x{geometry['height']}")
    print(f"world left {geometry['world_left']:.1f}, top {geometry['world_top']:.1f}, "
          f"{geometry['units_per_pixel']:.3f} units per pixel")
    print(f"wrote {geometry_path}")

    icon_count = extract_hud_icons(game / HUD_DICTIONARY, PACK / "images" / "items" / "hud")
    print(f"wrote {icon_count} legend icons to images/items/hud")
    return 0


if __name__ == "__main__":
    sys.exit(main())
