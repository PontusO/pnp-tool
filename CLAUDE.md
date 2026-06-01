# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the tool

```bash
python3 claudepnp.py --bom <file.txt|file.csv> [options]
```

There is no build step, no virtual environment, and no external dependencies — stdlib only. There are no automated tests; verify changes by running the tool against `nr52-top.txt` and inspecting the output files.

Key flags for development verification:
```bash
# Single machine, clean run (delete components CSV first to force Phase 1)
rm -f nr52-top_components.csv && python3 claudepnp.py --bom nr52-top.txt

# Two-machine split with nozzle skew
python3 claudepnp.py --bom nr52-top.txt --machines 2 --machine1-skew 20

# 10-head machine
python3 claudepnp.py --bom nr52-top.txt --heads 10
```

## Architecture

The entire tool is in a single file: `claudepnp.py`. Supporting data files:

| File | Role |
|---|---|
| `feeder_table.csv` | Tape width → slots consumed |
| `package_rules.csv` | Package name patterns → feeder_width / feeder_row / nozzle_type |
| `timing_config.csv` | Calibrated placement-time bounds (seconds) per row |

### Two-phase workflow

**Phase 1** reads the BOM, groups placements into `ComponentType` objects, applies `package_rules.csv`, then writes `<stem>_components.csv`. If any component is missing `feeder_width`, `feeder_row`, or `nozzle_type` it prints an INCOMPLETE table and exits — the operator fills in the CSV and re-runs. Manual edits (`matched_by = MANUAL`) survive all subsequent re-runs.

**Phase 2** reads the completed component CSV, assigns feeder slots, optimises the nozzle head configuration, builds pick sequences, and writes the feeder CSV, sequence file, and nozzle config CSV.

The only state passed between phases is the `_components.csv` file on disk.

### Physical machine constraints (drive most design decisions)

- **70 slots**: FRONT 1–38 (left→right), REAR 39–70 (right→left). Both rows share physical centre at **148 mm** from the left edge.
- **Simultaneous picking**: Up to `MAX_SIMULTANEOUS = 4` components in one head descent, only if their feeder slots are in the same row and the same **mod-3 group** (`slot % 3` for FRONT, `(70 - slot) % 3` for REAR).
- **Head-reuse firing**: The machine accumulates components into physical heads (one per nozzle) and fires as soon as it would need to reuse a head already loaded. Two components requiring the same nozzle type use the same physical head — they cannot be in the same pick batch unless multiple heads carry that nozzle type.
- **Single nozzle load per job**: The operator loads nozzles once before the job starts. The head configuration **cannot be changed mid-run** without risking calibration loss. There is no per-row or per-pass head config — `head_config` is global for the entire machine run.

### Key data flow

```
load_bom()
  → list[Placement]
    → group_components()
      → list[ComponentType]
        → apply_package_rules() / load_components_csv()   # Phase 1
          → split_components_across_machines()             # nozzle-aware, skew-biased
            → assign_slots()                               # centre-out, mod-3 aligned
              → optimize_head_config() / _greedy_head_alloc()
                → build_sequences()                        # interleaved by nozzle type
                  → _bundle_rear_into_front()              # sparse REAR absorbed into FRONT
                    → write_feeder_csv()
                    → write_job_file()
                    → write_nozzle_config_csv()
```

### Feeder slot assignment (`assign_slots`)

Slots are tried in **centre-out order** (closest to 148 mm first) to minimise head travel. Wide feeders (12 mm, 16 mm, etc.) consume multiple consecutive slots. If the preferred row is full, the component overflows to the other row and a warning is printed. The component's `feeder_row` attribute is updated in place so sequences are built correctly.

### Sequence generation (`build_sequences`)

Components are grouped by nozzle type and sorted within each group by nearest-neighbour PCB tour. Each pick cycle takes `h_i` components from nozzle type `i` (where `h_i` is the head allocation from `optimize_head_config`). This interleaving prevents head-reuse firing.

FRONT and REAR are sequenced separately. If the REAR average cycle size is below `n_heads / 2`, `_bundle_rear_into_front()` absorbs as many REAR placements as head capacity allows into FRONT cycles, eliminating dedicated short REAR passes.

### Head configuration (`optimize_head_config` / `_greedy_head_alloc`)

Greedy bottleneck minimisation: start with 1 head per nozzle type, repeatedly assign the next head to whichever nozzle type has the highest `placements / current_heads` ratio. This minimises `max_i(ceil(P_i / h_i))`.

`_greedy_head_alloc(counts: dict[str, int], n_heads: int)` is the reusable core; `optimize_head_config(components, n_heads)` is the component-level wrapper.

### Machine-split bias (`split_components_across_machines`)

`_SMALL_NOZZLES = {'#500', '#501', '#502', '#503'}` (passives) are preferentially routed to machine 1 up to a configurable budget (`machine1_skew` %). Large-nozzle components (ICs, modules) go to machines 2+ by tie-breaking in reverse machine-index order. This reflects the physical reality that machine 1 is optimised for fast small-nozzle work.

### Auto-learn (`update_package_rules_from_manual`)

After Phase 1, any component with `matched_by = MANUAL` whose package isn't already covered by any rule in `package_rules.csv` gets a new `exact` rule appended under an `# Auto-learned` section. This runs automatically on every Phase 1 pass.

### Juki nozzle numbering

Nozzles are identified as `#500`–`#508`. The mapping used in `package_rules.csv`:

| Nozzle | Typical use |
|---|---|
| `#500` | 0201 and smaller |
| `#501` | 0402 |
| `#502` | 0603 |
| `#503` | 0805, 1206 |
| `#504` | Small ICs: SOT-23, small QFN/DFN, SOIC-8 |
| `#505` | Medium ICs: QFN >3 mm, SOIC-16+, TQFP, TSSOP |
| `#506`–`#508` | Modules, BGA, LGA — left blank in rules, require manual entry |

### Timing model (`_placement_time`)

Linear interpolation between `timing_config.csv` bounds based on physical distance from 148 mm:
- FRONT: 0.5 s (centre) → 0.7 s (edge)
- REAR: 1.0 s (centre) → 1.2 s (edge)

Calibrate by timing a real board run and adjusting `timing_config.csv`.

## Important invariants

- `feeder_row` on a `ComponentType` / `Placement` is updated in place by `assign_slots()` if the component overflows to the alternate row. Always read `feeder_row` from the object, not from the original BOM data, when building sequences.
- `_component_status()` checks `_REQUIRED_COMP_FIELDS = ('feeder_width', 'feeder_row', 'nozzle_type')`. All three must be non-empty for a component to be `OK`.
- The sequence file is space-separated with no commas or semicolons. `_sanitize()` enforces this on all string fields. `_clean_field()` handles European decimal commas in values (e.g. `4,7uF` → `4.7uF`) during BOM load.
- FRONT and REAR sequences are always written in that order (FRONT first) in the job file. The machine reads the file strictly top to bottom.
