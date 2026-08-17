"""Build the pack's map image from the player's own Vice City install.

The tracker pins every check at the game's own world position, so its map has
to be the game's own radar art or the pins would not line up. That art lives as
64 radar tiles inside the IMG archive, one RenderWare texture dictionary each.
This tool walks the archive, decodes the tiles, and assembles them into
images/maps/vice_city.png, plus the HUD legend icons the pins use.

Tile n sits at column n % 8, row n // 8. Column 0 starts at world x = -2000 and
row 0 at world y = +2000, each tile spanning 500 world units, so the assembled
1024 x 1024 image covers world x and y from -2000 to +2000. That is where the
pin transform in generate.py comes from; the constants behind it are the
game's own (0x68FD44 = 500.0 tile span and 0x68FD00 = 2000.0 origin, combined
as tileIndex * 500.0 - 2000.0 by the radar's world-to-texture transform).

Note the entity sector grid is a different thing with a different origin
(x from -2400), and using it would shift every pin 400 units east.

Usage:
    py -3.12 tools/extract_map.py "D:/path/to/Grand Theft Auto Vice City"

Needs Pillow and numpy.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy
from PIL import Image

PACK = Path(__file__).resolve().parent.parent

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
    map_path = PACK / "images" / "maps" / "vice_city.png"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    atlas.convert("RGB").save(map_path)
    print(f"wrote {map_path} at {atlas.size[0]}x{atlas.size[1]}")

    icon_count = extract_hud_icons(game / HUD_DICTIONARY, PACK / "images" / "items" / "hud")
    print(f"wrote {icon_count} legend icons to images/items/hud")
    return 0


if __name__ == "__main__":
    sys.exit(main())
