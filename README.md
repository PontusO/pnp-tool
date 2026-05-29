# ClaudePnP — SMT Pick and Place Optimisation Tool

## Background and Problem Statement

Surface-mount PCB assembly on a pick and place machine involves two
interdependent decisions that directly determine how long a board takes to
populate: **where each component reel sits in the feeder bank**, and **in what
order the machine picks and places each component**.

The machines this tool was designed for have the following physical layout:

- **70 feeder slots** in total, each one 8 mm wide.
- **Slots 1–38** form the **front row**, numbered left to right when standing
  in front of the machine.
- **Slots 39–70** form the **rear row**, numbered right to left (slot 39 is
  physically adjacent to slot 38 on the right side of the machine; slot 70 is
  on the left).
- The **front row** has 8 high-speed cameras located at the centre of the
  machine. These can inspect components up to approximately 52-pin QFN size
  (~7 × 7 mm body). The front row handles the vast majority of components on a
  typical board.
- The **rear row** has a single high-precision camera, also centred, used for
  larger components such as RF modules, BGA packages, and other oversized parts
  that exceed the optical window of the front cameras.
- The machine has **8 nozzles**. Up to **4 components can be picked
  simultaneously** in one head descent, provided their feeder slots are spaced
  at multiples of 3 slot positions apart (i.e. at the same position modulo 3).
  A full set of 8 components therefore ideally requires only 2 head descents.
- Both the camera array and the PCB work area are located at the **physical
  centre** of the machine. Feeders placed near the centre of the rack therefore
  minimise the head travel on every pick–inspect–place cycle.

Older machines of this type have no built-in intelligence for feeder layout or
sequence optimisation. The operator must manually decide where every reel goes
and in what order the machine runs. On a complex board with 50+ unique
component types and hundreds of placements this is time-consuming and the
results are rarely optimal.

ClaudePnP automates both decisions:

1. **Feeder assignment** — assigns each component type to the slot(s) that
   minimise average head travel, grouping high-volume parts at the centre of
   the rack and aligning reels to the 3-slot simultaneous-pick grid where
   possible.

2. **Sequence generation** — orders all placements into pick sequences of up
   to 8 using a nearest-neighbour tour of the PCB, keeping front-row and
   rear-row components in separate passes to avoid the head switching rows
   mid-sequence.

---

## Files

### Scripts and configuration

| File | Purpose |
|---|---|
| `claudepnp.py` | Main script |
| `feeder_table.csv` | Maps tape width (mm) to number of slots consumed |
| `package_rules.csv` | Ordered rules for auto-assigning feeder width and row from package name |

### Generated files (one set per board)

| File | Purpose |
|---|---|
| `<stem>_components.csv` | Intermediate component table — one row per unique component type. Reviewed and optionally edited by the user between Phase 1 and Phase 2. |
| `<stem>_feeders.csv` | Final feeder assignment — one row per assigned reel, with slot number, physical position, and component details. Handed to the machine operator. |
| `<stem>_sequence.txt` | Machine job file — one line per placement, space-separated, ordered into optimised pick sequences. |

---

## Input BOM Format

ClaudePnP reads **comma-separated `.csv`** and **space-separated `.txt`** files
automatically based on file extension.

The following column names are recognised (case-sensitive alternatives are
listed where they differ between tools):

| Canonical name | Accepted aliases | Required |
|---|---|---|
| `refdes` | `Designator`, `RefDes`, `Ref` | Yes |
| `value` | `Value`, `Val` | Yes |
| `package` | `Package`, `Footprint` | Yes |
| `X` | `PosX`, `posx` | Yes |
| `Y` | `PosY`, `posy` | Yes |
| `A` | `Angle`, `Rotation` | Yes |
| `feeder_width` | — | Optional* |
| `feeder_row` | — | Optional* |
| `nozzle_type` | — | Optional |
| `name` | — | Optional |

\* If `feeder_width` or `feeder_row` are absent, ClaudePnP fills them in
automatically using `package_rules.csv` during Phase 1. Any component that
cannot be matched is flagged for manual entry.

Components whose `value` field is `DNM`, `DNP`, `DNF`, or similar are skipped
automatically. Use `--include-dnm` to override this.

---

## Two-Phase Workflow

ClaudePnP always works in two phases:

```
Phase 1 — Enrich
  Read BOM → apply package rules → write <stem>_components.csv
  If any component type could not be resolved:
    Print a table of unmatched packages and EXIT.
    The user fills in the blank rows in <stem>_components.csv and re-runs.
  If all resolved:
    Continue directly to Phase 2.

Phase 2 — Optimise
  Read <stem>_components.csv → merge into placements
  Assign feeder slots (centre-out, mod-3 aligned)
  Build pick sequences (nearest-neighbour PCB tour)
  Write <stem>_feeders.csv and <stem>_sequence.txt
```

The component file is always (re-)written in Phase 1. If it already exists,
any rows marked `matched_by = MANUAL` are preserved and never overwritten by
rules. This means manual entries survive re-runs even when `package_rules.csv`
is updated.

If the BOM changes (components added or removed), delete the existing
`_components.csv` and re-run from scratch to ensure consistency.

---

## The Component File (`_components.csv`)

This is the key intermediate artefact. It has one row per unique
(value, package) combination and the following columns:

| Column | Description |
|---|---|
| `value` | Component value (e.g. `100nF`, `STM32F405RGT6`) |
| `package` | Package / footprint name |
| `count` | Number of placements on the board |
| `feeder_width` | Tape width in mm — **must match a row in `feeder_table.csv`** |
| `feeder_row` | `FRONT` or `REAR` |
| `nozzle_type` | Nozzle identifier (informational, not used by machine directly) |
| `name` | MPN or other identifier (written to the job file) |
| `matched_by` | Which rule matched (`prefix:C0402`, `MANUAL`, `BOM`, etc.) |
| `status` | `OK` or `INCOMPLETE` |

When filling in manually, set `matched_by` to `MANUAL` and `status` to `OK`.
Sort by `status` to find all rows that still need attention.

---

## The Package Rules File (`package_rules.csv`)

Rules are evaluated **top to bottom; the first match wins**. Lines starting
with `#` are comments and are ignored.

Columns:

| Column | Description |
|---|---|
| `pattern` | The string to match against the package name |
| `match_type` | `exact`, `prefix`, `contains`, or `regex` |
| `feeder_width` | Tape width in mm to assign |
| `feeder_row` | `FRONT` or `REAR` to assign |
| `nozzle_type` | Nozzle to assign (can be blank) |
| `notes` | Free-text description (ignored by the script) |

Matching is **case-insensitive** for all match types.

To add a rule for a package that keeps appearing as unmatched, add a row near
the top of the file (before any generic `contains` catch-alls for the same
family):

```
MY_CUSTOM_QFN,exact,8,FRONT,N08,Our library name for a 3x3 QFN
```

---

## The Feeder Table (`feeder_table.csv`)

Maps tape width to the number of 8 mm rack slots consumed:

| `width_mm` | `slots_consumed` | `description` |
|---|---|---|
| 8 | 1 | Standard 8 mm tape |
| 12 | 2 | 12 mm tape |
| 16 | 2 | 16 mm tape |
| 24 | 3 | 24 mm tape |
| 32 | 4 | 32 mm tape |
| 44 | 6 | 44 mm tape |
| 56 | 7 | 56 mm tape |

Add rows for any non-standard tape widths your feeders require.

---

## Command-Line Reference

```
python3 claudepnp.py --bom <file> [options]
```

| Argument | Short | Default | Description |
|---|---|---|---|
| `--bom` | `-b` | *(required)* | Input BOM file (`.csv` or `.txt`) |
| `--feeder-table` | `-f` | `feeder_table.csv` | Feeder specification file |
| `--package-rules` | | `package_rules.csv` | Package rules file |
| `--output-dir` | `-o` | `.` (current dir) | Directory for all output files |
| `--job-prefix` | `-p` | BOM filename stem | Prefix for all output filenames |
| `--multi-reel` | `-m` | off | Suggest duplicate reels for high-volume components |
| `--multi-reel-threshold` | `-t` | `20` | Placements per reel before a second reel is added |
| `--include-dnm` | | off | Include DNM/DNP components instead of skipping them |

---

## Examples

### Basic run — space-separated export from CAD tool

```bash
python3 claudepnp.py --bom nr52-top.txt
```

If all packages are recognised the script completes in one pass and writes
`nr52-top_components.csv`, `nr52-top_feeders.csv`, and
`nr52-top_sequence.txt` to the current directory.

---

### Basic run — comma-separated BOM with full feeder columns

```bash
python3 claudepnp.py --bom myboard.csv
```

When the BOM already contains `feeder_width` and `feeder_row` columns the
package rules are still applied as a consistency check, but the BOM values
take precedence.

---

### Handling unmatched packages

If Phase 1 cannot resolve all packages it prints a table and stops:

```
Phase 1 — Component enrichment
  INCOMPLETE: 2 component type(s) need manual input:

  Value                Package                        Missing fields
  -------------------- ------------------------------ --------------------
  TPS7A0218PD          SUP_TPS3831L30DQNR             feeder_width, feeder_row
  MY_MODULE            CUSTOM_MODULE_V2               feeder_width, feeder_row

  Component file written: nr52-top_components.csv
  Fill in the INCOMPLETE rows and re-run to continue.
```

Open `nr52-top_components.csv` in a spreadsheet, find the two `INCOMPLETE`
rows (they sort to the top), fill in `feeder_width`, `feeder_row`, and
optionally `nozzle_type` and `name`, then set `matched_by` to `MANUAL` and
`status` to `OK`. Save and re-run:

```bash
python3 claudepnp.py --bom nr52-top.txt
```

The script picks up the manual entries and proceeds to Phase 2.

---

### Writing outputs to a separate directory

```bash
python3 claudepnp.py --bom nr52-top.txt --output-dir ./jobs/nr52
```

---

### Multi-reel mode for high-volume components

For boards with many identical passives (decoupling capacitors, pull-up
resistors) it can be faster to load duplicate reels at slots spaced 3 apart so
the machine can pick 2, 3, or 4 of the same component in one head descent:

```bash
python3 claudepnp.py --bom nr52-top.txt --multi-reel --multi-reel-threshold 15
```

Any component with 15 or more placements gets a second reel. Components with
30 or more get a third, up to a maximum of 4 reels (one per nozzle that can
fire simultaneously). The duplicate reels are placed in the same mod-3
alignment group so simultaneous picking applies.

---

### Custom output prefix

```bash
python3 claudepnp.py --bom nr52-top.txt --job-prefix nr52_rev_b
```

Outputs: `nr52_rev_b_components.csv`, `nr52_rev_b_feeders.csv`,
`nr52_rev_b_sequence.txt`.

---

## Output File Details

### Feeder assignment CSV (`_feeders.csv`)

Handed to the machine operator to physically load the feeder bank.

| Column | Description |
|---|---|
| `slot` | Slot number (1–38 front, 39–70 rear) |
| `slots_consumed` | Number of slots this feeder occupies |
| `physical_x_mm` | Distance from the left end of the rack in mm |
| `row` | `FRONT` or `REAR` |
| `reel_index` | `0` = primary reel, `1+` = duplicate reels (multi-reel mode) |
| `value` | Component value |
| `package` | Package name |
| `feeder_width_mm` | Tape width |
| `nozzle_type` | Nozzle identifier |
| `name_mpn` | MPN or name |
| `total_placements` | Total placements of this component on the board |
| `mod_group` | Simultaneous-pick alignment group (0, 1, or 2) |

Components with the same `mod_group` value and in the same row can be picked
simultaneously.

### Job sequence file (`_sequence.txt`)

Space-separated, no commas or semicolons. Comment lines (starting with `#`)
carry sequence metadata and pick sub-group information for operator reference
but are ignored by the machine.

```
# SEQ 0001  row=FRONT  placements=8  pick_descends=2
#   pick 1: slots [19 19 19 19] refs [C1 C2 C3 C4]
#   pick 2: slots [19 22 16 25] refs [C5 R1 C9 R3]
C1 100nF 10.5000 20.3000 0.00 0402 GRM155R61A104KA01D
C2 100nF 15.2000 20.3000 0.00 0402 GRM155R61A104KA01D
...
```

Each data line contains: `refdes  value  X  Y  angle  package  name`

Sequences are separated by a blank line. The machine reads the file top to
bottom with no internal reordering.
