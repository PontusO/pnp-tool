#!/usr/bin/env python3
"""
ClaudePnP - Pick and Place Optimization Tool

Reads a BOM CSV and produces:
  1. A feeder assignment CSV  (<prefix>_feeders.csv)
  2. A space-separated job sequence file  (<prefix>_sequence.txt)

BOM CSV expected columns:
  refdes, value, X, Y, A, package, feeder_width, feeder_row, nozzle_type, name

feeder_row must be FRONT or REAR.
feeder_width is the tape width in mm (must exist in feeder_table.csv).

Usage:
  python claudepnp.py --bom my_board.csv [options]

  --multi-reel          Suggest duplicate reels for high-frequency components
                        to enable simultaneous picking. Reels are placed at
                        slots spaced NOZZLE_PITCH_SLOTS apart.
  --multi-reel-threshold N
                        Minimum placement count to trigger an extra reel
                        (default: 20).
"""

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ─── Machine constants ────────────────────────────────────────────────────────

SLOT_WIDTH_MM       = 8    # Physical width of one 8 mm slot position
NOZZLE_PITCH_SLOTS  = 3    # Nozzle spacing in slots for simultaneous pick
MAX_SIMULTANEOUS    = 4    # Max components picked in one head descent
MAX_NOZZLES         = 8    # Total nozzles on the pick head

FRONT_SLOT_FIRST    = 1
FRONT_SLOT_LAST     = 38
REAR_SLOT_FIRST     = 39
REAR_SLOT_LAST      = 70

FRONT_SLOTS = set(range(FRONT_SLOT_FIRST, FRONT_SLOT_LAST + 1))
REAR_SLOTS  = set(range(REAR_SLOT_FIRST,  REAR_SLOT_LAST  + 1))

# Physical centre of each rack, expressed in slot-number space.
# The PCB work area sits at the machine centre so feeders closest to the
# centre minimise head travel on the pick↔place round trip.
#
# Front (slots 1-38, left→right, 8 mm/slot):
#   machine centre ≈ 148 mm → slot 19.5  (between slots 19 and 20)
#
# Rear  (slots 39-70, numbered right→left, 8 mm/slot):
#   same physical machine centre at 148 mm.
#   physical_x(n) = (70-n)*8  →  (70-n)*8 = 148  →  n = 51.5
#   (between rear slots 51 and 52)
FRONT_CENTER: float = 19.5
REAR_CENTER:  float = 51.5

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FeederSpec:
    width_mm: int
    slots_consumed: int
    description: str


@dataclass
class Placement:
    """One component instance to be placed on the PCB."""
    refdes:       str
    value:        str
    x:            float
    y:            float
    angle:        float
    package:      str

    # Feeder fields — populated either from the BOM or from the component CSV
    feeder_width: Optional[int] = None
    feeder_row:   Optional[str] = None   # 'FRONT' or 'REAR'
    nozzle_type:  str           = ''
    name:         str           = ''     # MPN / generic name

    # Set during slot assignment
    feeder_slot: Optional[int] = field(default=None, repr=False)
    reel_index:  int           = field(default=0,    repr=False)


@dataclass
class ComponentType:
    """A unique component type that maps to one (or more) feeder reels."""
    value:        str
    package:      str
    feeder_width: Optional[int] = None
    feeder_row:   Optional[str] = None
    nozzle_type:  str           = ''
    name:         str           = ''
    placements:   list          = field(default_factory=list)
    matched_by:   str           = ''   # which package rule matched, or 'BOM'

    @property
    def count(self) -> int:
        return len(self.placements)


@dataclass
class FeederAssignment:
    """One reel assigned to one or more consecutive slots."""
    slot:           int
    slots_consumed: int
    reel_index:     int
    component:      ComponentType

    @property
    def physical_x_mm(self) -> float:
        return _slot_physical_x(self.slot)

    @property
    def row(self) -> str:
        return self.component.feeder_row


# ─── Slot geometry ────────────────────────────────────────────────────────────

def _slot_physical_x(slot: int) -> float:
    """
    Return the physical X position (mm) of the leftmost edge of a slot.

    Front row  (1-38):  slot 1 is leftmost  → x = (slot-1) * 8
    Rear  row (39-70):  slot 39 is rightmost → x = (70-slot) * 8
    Both rows share the same left/right orientation when viewed from the front.
    """
    if slot in FRONT_SLOTS:
        return (slot - 1) * SLOT_WIDTH_MM
    else:
        return (REAR_SLOT_LAST - slot) * SLOT_WIDTH_MM


def _slot_mod_group(slot: int) -> int:
    """
    Return which simultaneous-pick alignment group (0, 1, or 2) a slot belongs to.
    Slots in the same group are spaced NOZZLE_PITCH_SLOTS apart and can be picked
    simultaneously.
    Front:  group = (slot - 1)               % NOZZLE_PITCH_SLOTS
    Rear:   group = (REAR_SLOT_LAST - slot)  % NOZZLE_PITCH_SLOTS
    """
    if slot in FRONT_SLOTS:
        return (slot - 1) % NOZZLE_PITCH_SLOTS
    else:
        return (REAR_SLOT_LAST - slot) % NOZZLE_PITCH_SLOTS


def _row_valid_slots(row: str) -> set[int]:
    """Return the set of valid slot numbers for a row."""
    return FRONT_SLOTS if row == 'FRONT' else REAR_SLOTS


def _center_out_order(row: str) -> list[int]:
    """
    Return all slot numbers for a row sorted by distance from the rack centre,
    alternating left/right of centre so the closest slots are tried first.

    This drives feeder assignment so that the highest-volume components land
    nearest the centre of the machine (shortest head travel to the PCB).

    Ties are broken by preferring the slot just below the centre (i.e. towards
    slot 1 for FRONT, towards slot 70 for REAR) so multi-slot wider feeders
    have room to expand rightward / leftward without immediately hitting the
    boundary.
    """
    if row == 'FRONT':
        slots  = list(range(FRONT_SLOT_FIRST, FRONT_SLOT_LAST + 1))
        center = FRONT_CENTER
    else:
        slots  = list(range(REAR_SLOT_FIRST, REAR_SLOT_LAST + 1))
        center = REAR_CENTER
    # Sort by distance from centre; for equal distance prefer the lower slot number
    return sorted(slots, key=lambda s: (abs(s - center), s))


# ─── Loaders ──────────────────────────────────────────────────────────────────

# Accepted column name aliases → canonical internal name.
# The lookup is case-sensitive; add variants as needed.
_COL_ALIASES: dict[str, str] = {
    # refdes
    'Designator': 'refdes', 'designator': 'refdes',
    'RefDes':     'refdes', 'refdes':     'refdes',
    'Ref':        'refdes', 'ref':        'refdes',
    # value
    'Value':  'value',  'value':  'value',
    'Val':    'value',  'val':    'value',
    # package / footprint
    'Package':   'package', 'package':   'package',
    'Footprint': 'package', 'footprint': 'package',
    # X coordinate
    'PosX': 'X', 'posx': 'X', 'pos_x': 'X', 'X': 'X', 'x': 'X',
    # Y coordinate
    'PosY': 'Y', 'posy': 'Y', 'pos_y': 'Y', 'Y': 'Y', 'y': 'Y',
    # rotation / angle
    'Angle':    'A', 'angle':    'A',
    'Rotation': 'A', 'rotation': 'A',
    'A':        'A', 'a':        'A',
    # optional extended columns (pass through unchanged)
    'feeder_width': 'feeder_width',
    'feeder_row':   'feeder_row',
    'nozzle_type':  'nozzle_type',
    'name':         'name',
    # Side / layer — informational only, not used for placement logic
    'Side': 'side', 'side': 'side', 'Layer': 'side', 'layer': 'side',
}

# Component values that indicate "do not place".
_DNM_VALUES: frozenset[str] = frozenset({
    'DNM', 'DNP', 'DNF', 'DNFIT',
    'DO_NOT_MOUNT', 'DO_NOT_POPULATE', 'DO_NOT_FIT',
    'DO NOT MOUNT', 'DO NOT POPULATE', 'DO NOT FIT',
})

# Default feeder width (mm) used when the column is absent from the file.
_DEFAULT_FEEDER_WIDTH = 8


def _normalize_row(raw: dict[str, str]) -> dict[str, str]:
    """Remap raw column names to canonical names using _COL_ALIASES."""
    return {_COL_ALIASES.get(k, k): v for k, v in raw.items()}


def load_feeder_table(path: Path) -> dict[int, FeederSpec]:
    specs: dict[int, FeederSpec] = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            w = int(row['width_mm'])
            specs[w] = FeederSpec(
                width_mm=w,
                slots_consumed=int(row['slots_consumed']),
                description=row.get('description', '').strip(),
            )
    return specs


def _detect_csv_delimiter(path: Path) -> str:
    """
    Detect whether a CSV file uses comma or semicolon as its field delimiter
    by inspecting the first non-empty line (the header).

    Semicolons are common in European-locale Excel exports where the comma is
    reserved as the decimal separator.
    """
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                return ';' if line.count(';') > line.count(',') else ','
    return ','


def _clean_field(value: str) -> str:
    """
    Sanitize a free-text field (value, MPN/name) so it is safe for the
    pick and place machine job file parser.

    Two-pass approach:
      1. Convert European decimal commas (digit,digit) to decimal points
         so that 4,7uF becomes 4.7uF rather than 4_7uF.
      2. Replace any remaining commas and semicolons with underscores —
         these include commas in part numbers (74AVC4TD245BQ,115) and any
         stray semicolons from locale-specific exports.

    Examples:
      4,7uF              → 4.7uF
      0,1uF              → 0.1uF
      74AVC4TD245BQ,115  → 74AVC4TD245BQ_115
      some;value         → some_value
    """
    import re
    # Pass 1: decimal commas → decimal points
    value = re.sub(r'(\d),(\d)', r'\1.\2', value)
    # Pass 2: any remaining commas or semicolons → underscore
    value = value.replace(',', '_').replace(';', '_')
    return value


def load_bom(path: Path, include_dnm: bool = False) -> list[Placement]:
    """
    Load a BOM file and return a list of Placement objects.

    File format is inferred from the extension:
      .csv  → comma- or semicolon-separated (delimiter auto-detected)
      .txt  → whitespace-separated (any run of spaces/tabs)

    Column names are normalised via _COL_ALIASES so both the canonical CSV
    format (refdes, value, X, Y, A, package, …) and the space-separated
    export format (Designator, Value, PosX, PosY, Angle, Package, …) are
    accepted transparently.

    European decimal commas in value fields (e.g. 4,7uF) are converted to
    decimal points (4.7uF) during loading so they never reach any output file.

    Optional columns (feeder_width, feeder_row, nozzle_type, name) are left
    as None when absent; the package-rules / component-CSV phase fills them in.
    """
    suffix = path.suffix.lower()

    placements: list[Placement] = []
    skipped_dnm: list[str] = []

    with open(path, newline='', encoding='utf-8') as f:
        if suffix == '.csv':
            delimiter = _detect_csv_delimiter(path)
            reader = csv.DictReader(f, delimiter=delimiter)
            rows = list(reader)
        else:
            # Space/tab-separated: split each line on any whitespace run.
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
            if not lines:
                return []
            headers = lines[0].split()
            rows = [
                dict(zip(headers, line.split()))
                for line in lines[1:]
                if line.strip()
            ]

    for lineno, raw in enumerate(rows, start=2):
        row = _normalize_row(raw)

        # ── Required fields ──────────────────────────────────────────────
        try:
            refdes  = row['refdes'].strip()
            value   = _clean_field(row['value'].strip())
            x       = float(row['X'].replace(',', '.'))
            y       = float(row['Y'].replace(',', '.'))
            angle   = float(row['A'].replace(',', '.'))
            package = row['package'].strip()
        except (KeyError, ValueError) as e:
            print(f"WARNING: BOM line {lineno} skipped — {e}", file=sys.stderr)
            continue

        # ── DNM / DNP filter ─────────────────────────────────────────────
        if value.upper() in _DNM_VALUES:
            if not include_dnm:
                skipped_dnm.append(refdes)
                continue

        # ── Optional feeder columns — left as None if absent; ─────────────
        # the package-rules / component-CSV phase fills them in later.
        raw_fw = row.get('feeder_width', '').strip()
        try:
            feeder_width: Optional[int] = int(raw_fw) if raw_fw else None
        except ValueError:
            feeder_width = None

        raw_fr     = row.get('feeder_row', '').strip().upper()
        feeder_row: Optional[str] = raw_fr if raw_fr in ('FRONT', 'REAR') else None

        nozzle_type = row.get('nozzle_type', '').strip()
        name        = _clean_field(row.get('name', '').strip())

        placements.append(Placement(
            refdes       = refdes,
            value        = value,
            x            = x,
            y            = y,
            angle        = angle,
            package      = package,
            feeder_width = feeder_width,
            feeder_row   = feeder_row,
            nozzle_type  = nozzle_type,
            name         = name,
        ))

    if skipped_dnm:
        print(
            f"  INFO: {len(skipped_dnm)} DNM/DNP component(s) skipped: "
            f"{', '.join(skipped_dnm[:10])}"
            + (' …' if len(skipped_dnm) > 10 else ''),
            file=sys.stderr,
        )

    return placements


# ─── Package rules ────────────────────────────────────────────────────────────

import re as _re

@dataclass
class PackageRule:
    pattern:      str
    match_type:   str   # exact | prefix | contains | regex
    feeder_width: Optional[int]
    feeder_row:   Optional[str]
    nozzle_type:  str
    notes:        str


def load_package_rules(path: Path) -> list[PackageRule]:
    """
    Load package rules from a CSV file.  Lines starting with # are comments
    and are silently ignored, so the file can contain section headers.
    """
    rules: list[PackageRule] = []
    with open(path, newline='', encoding='utf-8') as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) < 4:
                print(f"WARNING: package_rules line {lineno} malformed, skipped",
                      file=sys.stderr)
                continue
            # Skip the header row
            if parts[0].strip().lower() == 'pattern':
                continue
            pattern    = parts[0].strip()
            match_type = parts[1].strip().lower()
            raw_width  = parts[2].strip()
            raw_row    = parts[3].strip().upper()
            nozzle     = parts[4].strip() if len(parts) > 4 else ''
            notes      = parts[5].strip() if len(parts) > 5 else ''

            feeder_width = int(raw_width) if raw_width else None
            feeder_row   = raw_row if raw_row in ('FRONT', 'REAR') else None

            if match_type not in ('exact', 'prefix', 'contains', 'regex'):
                print(f"WARNING: package_rules line {lineno}: unknown match_type "
                      f"'{match_type}', skipped", file=sys.stderr)
                continue

            rules.append(PackageRule(
                pattern      = pattern,
                match_type   = match_type,
                feeder_width = feeder_width,
                feeder_row   = feeder_row,
                nozzle_type  = nozzle,
                notes        = notes,
            ))
    return rules


def _match_rule(package: str, rule: PackageRule) -> bool:
    """Return True if the package name matches the rule (case-insensitive)."""
    pkg = package.upper()
    pat = rule.pattern.upper()
    if rule.match_type == 'exact':
        return pkg == pat
    elif rule.match_type == 'prefix':
        return pkg.startswith(pat)
    elif rule.match_type == 'contains':
        return pat in pkg
    elif rule.match_type == 'regex':
        return bool(_re.search(rule.pattern, package, _re.IGNORECASE))
    return False


def apply_package_rules(
    components: list['ComponentType'],
    rules:      list[PackageRule],
) -> None:
    """
    Apply package rules in order to fill in feeder_width, feeder_row, and
    nozzle_type for any component where those fields are not already set.
    Sets comp.matched_by to a description of the matching rule.
    Only the first matching rule is applied (top-to-bottom priority).
    """
    for comp in components:
        if comp.matched_by:          # already resolved (e.g. came from BOM)
            continue
        for rule in rules:
            if not _match_rule(comp.package, rule):
                continue
            tag = f"{rule.match_type}:{rule.pattern}"
            if comp.feeder_width is None and rule.feeder_width is not None:
                comp.feeder_width = rule.feeder_width
            if not comp.feeder_row and rule.feeder_row:
                comp.feeder_row = rule.feeder_row
            if not comp.nozzle_type and rule.nozzle_type:
                comp.nozzle_type = rule.nozzle_type
            comp.matched_by = tag
            break   # first match wins


# ─── Component CSV (intermediate file) ────────────────────────────────────────

_COMP_CSV_FIELDS = [
    'value', 'package', 'count',
    'feeder_width', 'feeder_row', 'nozzle_type', 'name',
    'matched_by', 'status',
]

_REQUIRED_COMP_FIELDS = ('feeder_width', 'feeder_row')


def _component_status(comp: 'ComponentType') -> str:
    missing = [f for f in _REQUIRED_COMP_FIELDS
               if not getattr(comp, f, None)]
    return 'INCOMPLETE' if missing else 'OK'


def write_components_csv(components: list['ComponentType'], path: Path) -> None:
    """Write one row per unique component type to the intermediate CSV."""
    # Sort: INCOMPLETE first (easiest for user to find), then by package
    sorted_comps = sorted(
        components,
        key=lambda c: (_component_status(c) != 'INCOMPLETE', c.package, c.value),
    )
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=_COMP_CSV_FIELDS)
        writer.writeheader()
        for comp in sorted_comps:
            writer.writerow({
                'value':        comp.value,
                'package':      comp.package,
                'count':        comp.count,
                'feeder_width': comp.feeder_width if comp.feeder_width else '',
                'feeder_row':   comp.feeder_row   if comp.feeder_row   else '',
                'nozzle_type':  comp.nozzle_type,
                'name':         comp.name,
                'matched_by':   comp.matched_by,
                'status':       _component_status(comp),
            })


def load_components_csv(path: Path) -> dict[tuple, dict]:
    """
    Load the intermediate component CSV.
    Returns a dict keyed by (value, package) → config dict.
    """
    configs: dict[tuple, dict] = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            key = (row['value'].strip(), row['package'].strip())
            configs[key] = {
                'feeder_width': int(row['feeder_width']) if row['feeder_width'].strip() else None,
                'feeder_row':   row['feeder_row'].strip().upper() or None,
                'nozzle_type':  row.get('nozzle_type', '').strip(),
                'name':         _clean_field(row.get('name', '').strip()),
                'matched_by':   row.get('matched_by', '').strip(),
            }
    return configs


def merge_component_configs(
    placements: list[Placement],
    configs:    dict[tuple, dict],
) -> list[str]:
    """
    Merge feeder info from the component config dict into each placement.
    Returns a list of (value, package) keys that were not found in configs
    (indicates BOM has changed since the component file was generated).
    """
    missing_keys: list[str] = []
    for p in placements:
        key = (p.value, p.package)
        cfg = configs.get(key)
        if cfg is None:
            missing_keys.append(f"{p.value} / {p.package}")
            continue
        p.feeder_width = cfg['feeder_width'] or _DEFAULT_FEEDER_WIDTH
        p.feeder_row   = cfg['feeder_row']   or 'FRONT'
        p.nozzle_type  = cfg['nozzle_type']
        p.name         = cfg['name']
    return list(dict.fromkeys(missing_keys))   # deduplicated, order preserved


def incomplete_configs(configs: dict[tuple, dict]) -> list[tuple]:
    """Return keys whose required fields are still blank."""
    return [
        key for key, cfg in configs.items()
        if not cfg['feeder_width'] or not cfg['feeder_row']
    ]


# ─── Component grouping ───────────────────────────────────────────────────────

def _component_key(p: Placement) -> tuple:
    return (p.value, p.package, p.feeder_width, p.feeder_row, p.nozzle_type, p.name)


def group_components(placements: list[Placement]) -> list[ComponentType]:
    # Key only on value+package so that components from a BOM without feeder
    # columns still group correctly (feeder info comes from the component CSV).
    groups: dict[tuple, ComponentType] = {}
    for p in placements:
        key = (p.value, p.package)
        if key not in groups:
            has_feeder_info = p.feeder_width is not None and p.feeder_row is not None
            groups[key] = ComponentType(
                value        = p.value,
                package      = p.package,
                feeder_width = p.feeder_width,
                feeder_row   = p.feeder_row,
                nozzle_type  = p.nozzle_type,
                name         = p.name,
                matched_by   = 'BOM' if has_feeder_info else '',
            )
        groups[key].placements.append(p)
    return list(groups.values())


# ─── Feeder slot assignment ────────────────────────────────────────────────────

def assign_slots(
    components:             list[ComponentType],
    feeder_specs:           dict[int, FeederSpec],
    multi_reel:             bool,
    multi_reel_threshold:   int,
) -> list[FeederAssignment]:
    """
    Assign feeder slots to all component types.

    Strategy
    --------
    1. Sort by placement count descending within each row (most-used →
       earliest assignment, landing nearest the rack centre).
    2. Search in centre-out order; prefer mod-group-0 slots for simultaneous
       picking, falling back to any free slot in the same row.
    3. If a component's preferred row is full, it is automatically moved to
       the other row rather than skipped.  A summary of all such moves is
       printed after assignment.  The component's feeder_row and all its
       placements are updated to reflect the actual row assigned.
    4. With --multi-reel, extra reels are placed in the same mod group.
    5. Placements are distributed round-robin across reels.
    """
    # Single occupancy map covering all 70 slots
    slot_free: dict[int, bool] = {s: True for s in range(1, REAR_SLOT_LAST + 1)}

    def _find_free_run_in_row(
        row: str,
        slots_needed: int,
        preferred_mod: Optional[int] = None,
    ) -> Optional[int]:
        valid = _row_valid_slots(row)
        for start in _center_out_order(row):
            if preferred_mod is not None and _slot_mod_group(start) != preferred_mod:
                continue
            run = list(range(start, start + slots_needed))
            if all(s in valid and slot_free.get(s, False) for s in run):
                return start
        return None

    def _occupy(start_slot: int, slots_needed: int) -> None:
        for s in range(start_slot, start_slot + slots_needed):
            slot_free[s] = False

    assignments:    list[FeederAssignment]              = []
    overflow_moves: list[tuple[ComponentType, str, str]] = []  # (comp, from, to)

    for preferred_row in ('FRONT', 'REAR'):
        row_components = [c for c in components if c.feeder_row == preferred_row]
        row_components.sort(key=lambda c: -c.count)

        for comp in row_components:
            spec = feeder_specs.get(comp.feeder_width)
            if spec is None:
                print(
                    f"WARNING: No feeder spec for {comp.feeder_width}mm — "
                    f"skipping {comp.value} {comp.package}",
                    file=sys.stderr,
                )
                continue

            slots_needed = spec.slots_consumed

            num_reels = 1
            if multi_reel and comp.count >= multi_reel_threshold:
                num_reels = min(MAX_SIMULTANEOUS, 1 + comp.count // multi_reel_threshold)

            reel_start_slots: list[int] = []
            actual_row = preferred_row

            for reel_idx in range(num_reels):
                # Try preferred row (mod-aligned, then any free slot)
                start = _find_free_run_in_row(actual_row, slots_needed, preferred_mod=0)
                if start is None:
                    start = _find_free_run_in_row(actual_row, slots_needed)

                # Preferred row full — fall back to the other row
                if start is None:
                    other_row = 'REAR' if actual_row == 'FRONT' else 'FRONT'
                    start = _find_free_run_in_row(other_row, slots_needed, preferred_mod=0)
                    if start is None:
                        start = _find_free_run_in_row(other_row, slots_needed)
                    if start is not None:
                        if reel_idx == 0:
                            overflow_moves.append((comp, preferred_row, other_row))
                        actual_row = other_row

                if start is None:
                    print(
                        f"ERROR: No free slots for {comp.value} ({comp.package}) "
                        f"in either row — component will be unplaced.",
                        file=sys.stderr,
                    )
                    break

                _occupy(start, slots_needed)
                reel_start_slots.append(start)
                assignments.append(FeederAssignment(
                    slot           = start,
                    slots_consumed = slots_needed,
                    reel_index     = reel_idx,
                    component      = comp,
                ))

            if not reel_start_slots:
                continue

            # If the component ended up in a different row, update it and
            # all its placements so sequences are built correctly.
            if actual_row != preferred_row:
                comp.feeder_row = actual_row
                for p in comp.placements:
                    p.feeder_row = actual_row

            # Distribute placements round-robin across reels
            for i, placement in enumerate(comp.placements):
                placement.feeder_slot = reel_start_slots[i % len(reel_start_slots)]
                placement.reel_index  = i % len(reel_start_slots)

    if overflow_moves:
        print(f"\n  NOTE: {len(overflow_moves)} component(s) moved to alternate row"
              f" due to FRONT overflow:")
        for comp, from_row, to_row in overflow_moves:
            print(f"    {comp.value:<20} {comp.package:<30} {from_row} → {to_row}")
        print(f"  Consider splitting the job across two machines for optimal cycle time.")

    return assignments


# ─── Pick sequence generation ─────────────────────────────────────────────────

def _pcb_distance(a: Placement, b: Placement) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _feeder_distance(a: Placement, b: Placement) -> float:
    """Approximate head travel between two feeder positions."""
    xa = _slot_physical_x(a.feeder_slot) if a.feeder_slot else 0.0
    xb = _slot_physical_x(b.feeder_slot) if b.feeder_slot else 0.0
    return abs(xa - xb)


def _sequence_cost(seq: list[Placement]) -> float:
    """
    Rough cost of a sequence: sum of feeder travel during picking
    + sum of PCB travel during placing.
    """
    if len(seq) < 2:
        return 0.0
    pick_cost  = sum(_feeder_distance(seq[i], seq[i+1]) for i in range(len(seq)-1))
    place_cost = sum(_pcb_distance(seq[i], seq[i+1])    for i in range(len(seq)-1))
    return pick_cost + place_cost


def _nearest_neighbor_tour(placements: list[Placement]) -> list[Placement]:
    """Greedy nearest-neighbor ordering by PCB position."""
    if not placements:
        return []
    remaining = list(placements)
    # Start from the placement closest to the board origin (0,0)
    start = min(remaining, key=lambda p: math.hypot(p.x, p.y))
    tour = [start]
    remaining.remove(start)
    while remaining:
        last = tour[-1]
        nearest = min(remaining, key=lambda p: _pcb_distance(last, p))
        tour.append(nearest)
        remaining.remove(nearest)
    return tour


def build_sequences(placements: list[Placement]) -> list[list[Placement]]:
    """
    Build optimised pick sequences.

    Rules
    -----
    - FRONT and REAR placements are handled in separate passes (mixing rows
      slows the machine considerably).
    - Each sequence holds at most MAX_NOZZLES (8) placements.
    - Within a row pass, placements are ordered by nearest-neighbour PCB tour,
      then packed into sequences of 8 in that order.
    - The PCB tour ordering ensures that consecutive sequences are spatially
      close on the board, minimising head travel between sequences.
    """
    sequences: list[list[Placement]] = []

    for row in ('FRONT', 'REAR'):
        row_placements = [p for p in placements
                          if p.feeder_slot is not None and p.feeder_slot in
                          (FRONT_SLOTS if row == 'FRONT' else REAR_SLOTS)]

        if not row_placements:
            continue

        ordered = _nearest_neighbor_tour(row_placements)

        # Chunk into sequences of MAX_NOZZLES
        for i in range(0, len(ordered), MAX_NOZZLES):
            seq = ordered[i: i + MAX_NOZZLES]
            sequences.append(seq)

    return sequences


def simultaneous_pick_groups(seq: list[Placement]) -> list[list[Placement]]:
    """
    For a single sequence, return the groups that can be picked simultaneously
    (up to MAX_SIMULTANEOUS components per descend, all in same mod group,
    same row).
    Returns a list of sub-groups; placements not fitting any group are singletons.
    """
    by_mod: dict[int, list[Placement]] = defaultdict(list)
    for p in seq:
        if p.feeder_slot is not None:
            by_mod[_slot_mod_group(p.feeder_slot)].append(p)

    groups: list[list[Placement]] = []
    assigned = set()
    # Largest mod groups first
    for mod in sorted(by_mod, key=lambda m: -len(by_mod[m])):
        chunk = [p for p in by_mod[mod] if id(p) not in assigned]
        while chunk:
            grp = chunk[:MAX_SIMULTANEOUS]
            groups.append(grp)
            for p in grp:
                assigned.add(id(p))
            chunk = chunk[MAX_SIMULTANEOUS:]

    # Any unassigned (shouldn't happen, but safety net)
    for p in seq:
        if id(p) not in assigned:
            groups.append([p])
    return groups


# ─── Capacity reporting ───────────────────────────────────────────────────────

def capacity_report(
    components:   list[ComponentType],
    feeder_specs: dict[int, FeederSpec],
) -> None:
    for row, first, last in (('FRONT', FRONT_SLOT_FIRST, FRONT_SLOT_LAST),
                              ('REAR',  REAR_SLOT_FIRST,  REAR_SLOT_LAST)):
        total_slots  = last - first + 1
        row_comps    = [c for c in components if c.feeder_row == row]
        slots_needed = sum(
            feeder_specs.get(c.feeder_width, FeederSpec(c.feeder_width, 1, '')).slots_consumed
            for c in row_comps
        )
        pct = slots_needed / total_slots * 100
        status = "OK" if slots_needed <= total_slots else "OVERFLOW"
        print(f"  {row:5s}  {slots_needed:3d}/{total_slots} slots  "
              f"({pct:5.1f}%)  {len(row_comps)} component types  [{status}]")


# ─── Output writers ───────────────────────────────────────────────────────────

def write_feeder_csv(assignments: list[FeederAssignment], path: Path) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            'slot', 'slots_consumed', 'physical_x_mm', 'row', 'reel_index',
            'value', 'package', 'feeder_width_mm', 'nozzle_type',
            'name_mpn', 'total_placements', 'mod_group',
        ])
        for a in sorted(assignments, key=lambda x: x.slot):
            w.writerow([
                a.slot,
                a.slots_consumed,
                f"{a.physical_x_mm:.1f}",
                a.row,
                a.reel_index,
                a.component.value,
                a.component.package,
                a.component.feeder_width,
                a.component.nozzle_type,
                a.component.name,
                a.component.count,
                _slot_mod_group(a.slot),
            ])


def _sanitize(s: str) -> str:
    """Remove characters that would break the space-delimited job file."""
    return s.replace(' ', '_').replace(',', '').replace(';', '').replace('\t', '_') or 'N/A'


def write_job_file(sequences: list[list[Placement]], path: Path) -> None:
    """
    Write the machine job file.

    Format (space-separated, no commas or semicolons):
        refdes  value  X  Y  A  footprint  name

    Sequences are separated by comment lines starting with #.
    The pick sub-group analysis is printed as comments for operator reference.
    """
    total_placements = sum(len(s) for s in sequences)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"# ClaudePnP job file\n")
        f.write(f"# Sequences: {len(sequences)}  "
                f"Total placements: {total_placements}\n")
        f.write("#\n")

        for seq_idx, seq in enumerate(sequences, start=1):
            row = seq[0].feeder_slot
            row_label = 'FRONT' if row in FRONT_SLOTS else 'REAR'
            pick_groups = simultaneous_pick_groups(seq)
            num_descends = len(pick_groups)

            f.write(
                f"# SEQ {seq_idx:04d}  row={row_label}  "
                f"placements={len(seq)}  pick_descends={num_descends}\n"
            )

            # Show simultaneous pick grouping as a comment
            for gi, grp in enumerate(pick_groups, start=1):
                slots_str = ' '.join(str(p.feeder_slot) for p in grp)
                refs_str  = ' '.join(p.refdes for p in grp)
                f.write(f"#   pick {gi}: slots [{slots_str}] refs [{refs_str}]\n")

            for p in seq:
                line = (
                    f"{_sanitize(p.refdes)} "
                    f"{_sanitize(p.value)} "
                    f"{p.x:.4f} "
                    f"{p.y:.4f} "
                    f"{p.angle:.2f} "
                    f"{_sanitize(p.package)} "
                    f"{_sanitize(p.name)}"
                )
                f.write(line + '\n')
            f.write('\n')


# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary(sequences: list[list[Placement]]) -> None:
    total = sum(len(s) for s in sequences)
    total_descends = 0
    for seq in sequences:
        total_descends += len(simultaneous_pick_groups(seq))

    print(f"\n  Sequences         : {len(sequences)}")
    print(f"  Total placements  : {total}")
    print(f"  Pick descends     : {total_descends}  "
          f"(ideal minimum: {math.ceil(total / MAX_SIMULTANEOUS)})")

    max_sim = 0
    for seq in sequences:
        for grp in simultaneous_pick_groups(seq):
            max_sim = max(max_sim, len(grp))
    print(f"  Best simultaneous : {max_sim} components per descend")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='ClaudePnP — SMT Pick and Place Optimization Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--bom', '-b', required=True,
                        help='Input BOM file (.csv comma-separated, .txt space-separated)')
    parser.add_argument('--feeder-table', '-f', default='feeder_table.csv',
                        help='Feeder specification CSV (default: feeder_table.csv)')
    parser.add_argument('--package-rules', default='package_rules.csv',
                        help='Package rules CSV (default: package_rules.csv)')
    parser.add_argument('--output-dir', '-o', default='.',
                        help='Output directory (default: current directory)')
    parser.add_argument('--job-prefix', '-p', default=None,
                        help='Prefix for output file names (default: BOM filename stem)')
    parser.add_argument('--multi-reel', '-m', action='store_true',
                        help='Suggest duplicate reels for high-frequency components')
    parser.add_argument('--multi-reel-threshold', '-t', type=int, default=20,
                        metavar='N',
                        help='Placements per reel threshold (default: 20)')
    parser.add_argument('--include-dnm', action='store_true',
                        help='Include DNM/DNP components instead of skipping them')
    args = parser.parse_args()

    bom_path          = Path(args.bom)
    feeder_table_path = Path(args.feeder_table)
    pkg_rules_path    = Path(args.package_rules)
    output_dir        = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    job_prefix        = args.job_prefix or bom_path.stem
    components_path   = output_dir / f"{job_prefix}_components.csv"

    # ── Always load the BOM first (needed for X/Y/A in both phases) ───────────
    print(f"Loading feeder table : {feeder_table_path}")
    feeder_specs = load_feeder_table(feeder_table_path)
    print(f"  {len(feeder_specs)} feeder widths: {sorted(feeder_specs)}")

    print(f"Loading BOM          : {bom_path}")
    placements = load_bom(bom_path, include_dnm=args.include_dnm)
    if not placements:
        print("ERROR: No placements loaded. Exiting.", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(placements)} placements loaded")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1 — Build (or re-build) the component CSV
    #
    # Always regenerate from the BOM + package rules to ensure the file stays
    # in sync with the BOM.  If the previous run left a components file that
    # contains manual edits, those are preserved for any row whose (value,
    # package) key still exists in the new BOM.
    # ═════════════════════════════════════════════════════════════════════════
    print(f"\nPhase 1 — Component enrichment")

    # Load any existing manual edits so we can preserve them
    existing_configs: dict[tuple, dict] = {}
    if components_path.exists():
        existing_configs = load_components_csv(components_path)
        print(f"  Existing component file found — preserving manual entries")

    # Group BOM into component types and apply package rules
    components = group_components(placements)
    print(f"  {len(components)} unique component types")

    # Restore manual edits from a previous run before applying rules
    for comp in components:
        key = (comp.value, comp.package)
        if key in existing_configs:
            prev = existing_configs[key]
            # Only restore if the user had actually filled something in
            if prev.get('matched_by') == 'MANUAL' or prev.get('feeder_width'):
                comp.feeder_width = prev['feeder_width'] or comp.feeder_width
                comp.feeder_row   = prev['feeder_row']   or comp.feeder_row
                comp.nozzle_type  = prev['nozzle_type']  or comp.nozzle_type
                comp.name         = prev['name']         or comp.name
                comp.matched_by   = prev['matched_by']

    # Apply package rules to anything still unresolved
    if pkg_rules_path.exists():
        pkg_rules = load_package_rules(pkg_rules_path)
        print(f"  Loaded {len(pkg_rules)} package rules from {pkg_rules_path}")
        apply_package_rules(components, pkg_rules)
    else:
        print(f"  WARNING: {pkg_rules_path} not found — skipping auto-match",
              file=sys.stderr)

    # Write (or overwrite) the component CSV
    write_components_csv(components, components_path)

    # Report match results
    ok       = [c for c in components if _component_status(c) == 'OK']
    incomplete = [c for c in components if _component_status(c) == 'INCOMPLETE']

    print(f"  Matched  : {len(ok)} component type(s)")
    if incomplete:
        print(f"  INCOMPLETE: {len(incomplete)} component type(s) need manual input:\n")
        print(f"  {'Value':<20} {'Package':<30} {'Missing fields'}")
        print(f"  {'-'*20} {'-'*30} {'-'*20}")
        for c in incomplete:
            missing = [f for f in _REQUIRED_COMP_FIELDS if not getattr(c, f)]
            print(f"  {c.value:<20} {c.package:<30} {', '.join(missing)}")
        print(f"\n  Component file written: {components_path}")
        print(f"  Fill in the INCOMPLETE rows and re-run to continue.\n")
        sys.exit(0)

    print(f"  All component types resolved — proceeding to slot assignment")
    print(f"  Component file written: {components_path}")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 2 — Merge component config into placements, assign slots, build
    #           sequences, write outputs
    # ═════════════════════════════════════════════════════════════════════════
    print(f"\nPhase 2 — Slot assignment and sequence generation")

    # Merge feeder info from the component table back into each placement
    configs = load_components_csv(components_path)
    orphans = merge_component_configs(placements, configs)
    if orphans:
        print(f"  WARNING: {len(orphans)} component type(s) in BOM not found in "
              f"component file (BOM may have changed):", file=sys.stderr)
        for o in orphans:
            print(f"    {o}", file=sys.stderr)
        print(f"  Delete {components_path} and re-run to regenerate it.",
              file=sys.stderr)
        sys.exit(1)

    # Re-group with feeder info now populated
    components = group_components(placements)

    print("\nSlot capacity:")
    capacity_report(components, feeder_specs)

    print("\nAssigning feeder slots...")
    assignments = assign_slots(
        components,
        feeder_specs,
        multi_reel=args.multi_reel,
        multi_reel_threshold=args.multi_reel_threshold,
    )
    print(f"  {len(assignments)} feeder reels assigned")
    if args.multi_reel:
        print(f"  Multi-reel ON (threshold: {args.multi_reel_threshold} placements/reel)")

    print("\nBuilding pick sequences...")
    sequences = build_sequences(placements)
    print_summary(sequences)

    # ── Write outputs ─────────────────────────────────────────────────────────
    feeder_csv = output_dir / f"{job_prefix}_feeders.csv"
    job_txt    = output_dir / f"{job_prefix}_sequence.txt"

    write_feeder_csv(assignments, feeder_csv)
    write_job_file(sequences, job_txt)

    print(f"\nOutputs written:")
    print(f"  {components_path}")
    print(f"  {feeder_csv}")
    print(f"  {job_txt}")


if __name__ == '__main__':
    main()
