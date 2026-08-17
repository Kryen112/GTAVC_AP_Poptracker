# GTA: Vice City PopTracker pack

Map tracker for the Grand Theft Auto: Vice City Archipelago world. One map, the
whole city, every check pinned at its own position in the game world.

## How it is built

Almost nothing here is written by hand. The pack is generated from the apworld
next door, so the tracker's logic is the world's logic rather than a second copy
of it that can drift.

| Step | Command | Produces |
| --- | --- | --- |
| Map art | `py -3.12 tools/extract_map.py "<game folder>"` | `images/maps/vice_city.png`, `images/items/hud/*.png` |
| Pack files | `py -3.12 tools/generate.py` | `items/`, `locations/`, `maps/`, `layouts/items.json`, `scripts/logic/access_rules.lua`, `scripts/autotracking/*_mapping.lua` |
| Pin art | `py -3.12 tools/make_pins.py` | `images/items/pins/*.png`, `images/items/settings/*.png` |
| Self-test | `py -3.12 tools/check_logic.py` | pass or a list of problems |
| Preview | `py -3.12 tools/preview_map.py` | `preview/pinned_map.png` |

Run the map extractor once, then the generator whenever the world changes, then
the pin art (it reads the generated item list to know which settings need
pictures).

`tools/check_logic.py` is the gate. It runs every generated rule against a stub
tracker and cross-checks the generated files against each other, so a rule
calling a helper that does not exist, a section nothing maps to, an item code no
item declares, or a pin off the edge of the map all fail here instead of at
runtime. It needs `lupa` and `Pillow`.

`tools/preview_map.py` draws the pins as a picture, reading the emitted
`locations/*.json` rather than the coordinate table, so it checks the whole
pipeline. A pin in the sea is obvious there and invisible in a diff.

`data/check_coords.py` is the check position table. It comes from the world
repository, not from here:

    python scripts/dump_check_coords.py clean.txt ../GTAVC_AP_Poptracker/data/check_coords.py

`clean.txt` is the player's own decompiled `main.scm`, the same file the mod
build uses. All decompile parsing lives in the world repository so there is only
one set of regexes to keep working. The dump refuses to write unless every
count, name, and cross-check passes, since three of its tables are ordered by
check index and one dropped entry would shift every later pin.

Hidden packages are the exception: their 100 positions already live in the
world's `package_data.py`, so the generator reads them from there and the dump
only holds that table to account against the decompile.

## Where the pins come from

The map is the game's own radar art, 64 tiles assembled into one 1024 x 1024
image covering world x and y from -2000 to +2000. So a check's pin is just its
game position transformed:

    px = (x + 2000) / 4000 * 1024
    py = (2000 - y) / 4000 * 1024

The constants are the game's own, read out of the executable rather than
guessed; `tools/extract_map.py` records where. Nothing is eyeballed, so a
renamed check keeps its pin and there is no coordinate to re-place by hand.

Positions come from the SCM: a mission from its launcher's own trigger test, a
package or rampage from its pickup, a purchase from its for-sale icon, a store
from its robbery trigger, a side event from its launcher.

Checks sharing a pixel share a pin, which is what happens to every mission
strand given from one spot. The pin then holds one section per check, so the
popup lists the strand and each mission still tracks and gates on its own.

## What is not pinned

Two classes place nothing in the world, so they are listed rather than pinned
(they still autotrack and still count):

- Emergency vehicle milestones. A level completes wherever the last fare or fire
  happens to be, so there is no position to use.
- Unique stunt jumps. These are exe-native; the SCM only registers a jump the
  engine already found. Their 36 positions are compiled into the executable and
  could be dumped from it, or read at runtime by the mod's ASI.

## Colours

Each check class has its own pin colour, drawn by `tools/make_pins.py` rather
than taken from the game, so a dense single map still reads as classes:

| Class | Colour |
| --- | --- |
| Story missions | gold |
| Properties and venue missions | blue |
| Hidden packages | magenta |
| Rampages | red |
| Stunt jumps | green |
| Robbable stores | orange |
| Side events | purple |
| Emergency vehicle missions | light blue |

The Show on map toggles hide a whole class, and a class the seed turned off
hides itself.
