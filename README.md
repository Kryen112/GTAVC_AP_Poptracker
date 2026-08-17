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

The map is the game's own radar art: 64 tiles assembled into a 1024 x 1024
square covering world x and y from -2000 to +2000, using constants read out of
the executable rather than guessed (`tools/extract_map.py` records where). Most
of that square is open sea, so the assembly is cropped to its own land plus a
margin, then padded back out sideways so the fitted image is short enough to
sit in a pane.

Cropping and padding move the transform off the plain radar formula, so
`extract_map.py` writes what is left to `data/map_geometry.json` and the
generator places every pin from that:

    px = (worldX - world_left) / units_per_pixel
    py = (world_top - worldY) / units_per_pixel

Nothing is eyeballed, so a renamed check keeps its pin and there is no
coordinate to re-place by hand.

Positions come from the SCM: a mission from its launcher's own trigger test, a
package or rampage from its pickup, a purchase from its for-sale icon, a store
from its robbery trigger, a side event from its launcher.

Checks whose pins would overlap share one pin, which is what happens to every
mission strand given from one spot. The pin then holds one section per check, so
the popup lists the strand and each mission still tracks and gates on its own.
`MERGE_DISTANCE_PIXELS` in the generator sets how close is too close.

## If the map does not fit your screen

PopTracker fits the map across the width it is given and lets the height fall
where it may, so a tall image overflows the pane and no amount of zooming out
recovers it. The image's pixel count does not come into it: a smaller file draws
at the same size, only blurrier.

What controls the drawn size is the image's shape. `MAP_ASPECT_RATIO` in
`tools/extract_map.py` pads the sides with open sea, which leaves the city where
it is and makes the fitted image shorter. Raise it to draw the city smaller,
lower it to draw it larger, then re-run the extractor and the generator.

## What is not pinned

Two classes have no world position.

Emergency vehicle milestones have none because a level completes wherever the
last fare or fire happens to be, so the five activities are laid out as five
markers in the open sea north east of Vice Point, each holding that activity's
levels. These are the only coordinates in the pack not read from the game.

Unique stunt jumps have no position anywhere a build step can read: the SCM
never defines them, and the executable holds no static table either. The game
builds the table while it runs, so it exists only in a live process.

The mod's ASI looks for it there. Load a game with the mod installed and press
**F7**: it scans for an array of world positions at a constant stride, spread
across the city, and writes `gtavc_ap_stuntjumps.txt` beside `gta-vc.exe`. Read
the header before trusting it:

    # span 3704 units, 100 percent away from the origin, 0 percent fits ...

That fit percentage is the honest signal. The heap holds a great many arrays of
positions, and a spatial grid can share the table's length, reach and spread; only
the fit says whether the floats form a jump record. A low percentage means the
scan did not find the table, whatever else the numbers say. The file carries six
alternatives with their own fit scores so a near-miss is recoverable.

At the time of writing the scan has not found it: five sessions, best fit 81
percent on a run of small round configuration values, everything else in single
digits. So the 36 jumps are listed without pins. They autotrack and count either
way, and the pack is complete without them.

Feeding a dump in, once one is right:

    python scripts/dump_check_coords.py clean.txt \n        ../GTAVC_AP_Poptracker/data/check_coords.py gtavc_ap_stuntjumps.txt

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
