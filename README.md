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
| Item art | `py -3.12 tools/make_icons.py` | `images/items/drawn/*.png` |
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

PopTracker fits the map inside the space its layout gives it, so the image's own
shape and pixel count do not decide how large it draws. Measured from two
sessions:

| image | pane | drawn | limited by |
| --- | --- | --- | --- |
| 1024 x 1024 | 1605 x 1269 | 1269 x 1269 | height |
| 1790 x 942 | 1599 x 1269 | 1599 x 841 | width |

The number that matters is the second one in the pane column. PopTracker lays the
window out to whatever height its tallest column needs and fits the map into
that, so a column taller than the screen makes the map bigger than the screen
too: it is clipped at the bottom, with a band of background above it, and no
amount of zooming out recovers it. That 1269 was a single tall column of items,
against a window of about 978.

So the fix is never in the map. Everything beside it goes in two columns: one
holds what the player has, the other the content lock grid and the map filters,
so each reads as one thing. What the seed was rolled with is not beside the map at
all; it is in the pack settings window. The generator prints both column heights
and warns past `COLUMN_HEIGHT_BUDGET`, since a column taller than the window is
what pushes the map off the screen. If that warning appears, move a section across
or add a column.

Width is the other half, and the grid is what made it one: the generator prints
the two columns' widths as well and warns past `PANEL_WIDTH_BUDGET`. The map draws
contained in the pane it is given, so at a column height around 900 px the city
wants some 700 px of width, and a 1920 window has about 1200 to give the columns
before the map has to shrink for them.

With the columns short, the map fits the window and no padding is wanted: the
image is the cropped city and nothing else, and PopTracker centres it in whatever
space is left. Padding the sides out to a wide aspect, which an earlier version
did, only shrinks the city, since the scale then follows the padded width rather
than the height.

## The panels, and where the settings went

Three homes, and which one a thing goes in follows from how often a player looks
at it.

The first column is what the player holds: area access, the goal, the strands,
property ownership, the abilities, the package and emergency rewards, the radio
stations and the minimap.

The second column is the content lock grid and the map filters. The grid is one
item grid read as a matrix: a column per district in the order the item ids run,
a row per content class, and a blank where a district holds nothing of that class.
Column zero is the whole-class item, the first row the per-district items, and the
rest one item per class per district, which is exactly the three granularities
`split_content_locks` chooses between. The corner cell is that setting itself,
sitting where its two axes meet, because a layout cannot ask what a seed rolled:
PopTracker has visibility rules for locations and none for layouts, so the grid is
drawn once and shown to every seed, and only the corner says which part of it is
live. The other two thirds stay dark all game, which is the price of a static
layout and the reason the coarse items sit in a column rather than scattered.

What the seed was rolled with, the seed options and the selected locks, is in
PopTracker's own pack settings window, opened from the button in its top bar. The
autotracker fills those from slot_data and a player changes them about never, so
they are not worth the width beside the map. The layout key the tracker looks them
up by is `settings_popup`. A gear on a group header is not the route: the pack
schema marks `header_content` and `button_popup` "not implemented yet" on every
field, and the string does not appear in the tracker binary at all.

The broadcast window gets one column of what the player holds plus the grid. It
has no map to filter and no settings button to open, so neither of those goes in
it.

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

### Placing them by hand

`tools/pixel_to_world.py` converts positions clicked on `images/maps/vice_city.png`
into the world coordinates the tables hold, using the same
`data/map_geometry.json` the generator pins from, so the two cannot disagree.

Open the map in an image editor, note the pixel under each jump, and put them in
a file, one per line. Comments and blank lines are ignored, so it can be
annotated as it is built:

    # 1  Washington Beach, the hotel ramp
    1032 636
    # 2  Ocean Drive
    900, 400

Then:

    py -3.12 tools/pixel_to_world.py --from jumps.txt --preview check.png

That draws the points on the map, numbered, so they can be checked before
anything is written. When they look right:

    py -3.12 tools/pixel_to_world.py --from jumps.txt --write
    py -3.12 tools/generate.py

`--write` replaces the `STUNT_JUMP_COORDS` list in `data/check_coords.py` and
leaves the rest of the file alone. Without it the block is printed to paste by
hand. `--reverse` goes the other way, world to pixel, to see where a coordinate
already in the tables lands.

Two things to know. The list must hold all 36 before the generator will pin the
class: a partial list fails loudly, naming the checks still without a position.
And entry *n* becomes `Unique Stunt Jump n`, which the mod detects through global
`$795 + n - 1`, so the order has to be the engine's. Place them in a different
order and every pin still sits on a real jump, but the names are shuffled against
what the game reports.

Feeding a dump in, once one is right:

    python scripts/dump_check_coords.py clean.txt \n        ../GTAVC_AP_Poptracker/data/check_coords.py gtavc_ap_stuntjumps.txt

## Icons

Most items wear the game's own legend art, pulled out of `hud.txd` by the map
extractor: each giver's progressive shows that giver's radar blip, the venues
show their own icons, the radio stations their station logos.

The game has no art for the rest. Vice City draws its weapon and vehicle icons as
models rather than sprites, so there is no sheet to lift them from, and the only
usable pieces anywhere in its textures are a banknote, a radar disc and a couple
of flames. `tools/make_icons.py` draws those instead: a white glyph on a rounded
tile, the tile colour naming the family and the glyph the item. Package rewards
are amber, emergency rewards teal, the minimap blue, the completion percentage
violet.

Content lock items are the third kind: each wears its own class's map pin, so a
row of the content grid reads as the class its pins read as on the map, and the
per-district items covering every class at once keep the neutral radar disc. The
blank cells are an item too, drawn as nothing, since an item grid takes item codes
and a hole in one still has to be one.

Without them all eleven package rewards, all five emergency rewards and the
minimap fall back to the same marker, and a panel of identical icons says
nothing. The generator refuses to run if a drawn icon is missing or names an item
the world does not have, so a rename cannot quietly put one back on the fallback.

## The completion percentage

The Goal group holds one item that no multiworld ever grants: `Percentage
Completed`, the game's own "Percentage completed" stat. The mod reads it off the
same number the stats menu prints, truncated the same way that screen truncates
it, and the client publishes it to the AP data store under
`gta_vice_city_percentage_<team>_<slot>`. The autotracker subscribes to that key
and writes the number as overlay text, so the icon is dark while the number
climbs and lights up at a hundred.

The key and the item's code both come from the apworld: `generate.py` imports
`protocol.percentage_key` and emits its prefix into `setting_mapping.lua`, so
the tracker cannot end up watching a key nothing writes. `check_logic.py` checks
that the emitted code names an item `items.json` declares, because the
hand-written `archipelago.lua` reads both names and a missing one would take
autotracking down with it rather than just the number.

It shows on every seed, not only the 100 percent goal. The goal itself is
still every check checked; this is a readout beside it.

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
