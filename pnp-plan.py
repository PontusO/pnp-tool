#!/usr/bin/env python3
"""
ClaudePnP - Pick and Place Optimization Tool

Reads a BOM CSV and produces:
  1. A feeder assignment CSV  (<prefix>_feeders.csv)
  2. A space-separated job sequence file  (<prefix>_sequence.txt)
  3. A nozzle head configuration CSV  (<prefix>_nozzle_config.csv)

Usage:
  python claudepnp.py --bom my_board.csv [options]

  --machines N          Split the job across N machines (default: 1).
  --heads N             Physical PnP heads per machine (default: 8).
  --machine1-skew PCT   Bias small-nozzle (#500–#503) components to machine 1.
  --multi-reel          Duplicate reels for high-frequency components.
  --multi-reel-threshold N
                        Minimum placements to trigger an extra reel (default: 20).
"""

import argparse
import csv
import math
import re
import sys
from pathlib import Path
from typing import Optional
from collections import defaultdict

from optimizer import (
    FeederSpec, Placement, ComponentType, FeederAssignment,
    PackageRule, TimingConfig,
    PnPOptimizer, component_status, group_components,
    REQUIRED_COMP_FIELDS,
)


# ─── BOM loading ──────────────────────────────────────────────────────────────

_COL_ALIASES: dict[str, str] = {
    'Designator': 'refdes', 'designator': 'refdes',
    'RefDes':     'refdes', 'refdes':     'refdes',
    'Ref':        'refdes', 'ref':        'refdes',
    'Value':  'value',  'value':  'value',
    'Val':    'value',  'val':    'value',
    'Package':   'package', 'package':   'package',
    'Footprint': 'package', 'footprint': 'package',
    'PosX': 'X', 'posx': 'X', 'pos_x': 'X', 'X': 'X', 'x': 'X',
    'PosY': 'Y', 'posy': 'Y', 'pos_y': 'Y', 'Y': 'Y', 'y': 'Y',
    'Angle':    'A', 'angle':    'A',
    'Rotation': 'A', 'rotation': 'A',
    'A':        'A', 'a':        'A',
    'feeder_width': 'feeder_width',
    'feeder_row':   'feeder_row',
    'nozzle_type':  'nozzle_type',
    'name':         'name',
    'Side': 'side', 'side': 'side', 'Layer': 'side', 'layer': 'side',
}

_DNM_VALUES: frozenset[str] = frozenset({
    'DNM', 'DNP', 'DNF', 'DNFIT',
    'DO_NOT_MOUNT', 'DO_NOT_POPULATE', 'DO_NOT_FIT',
    'DO NOT MOUNT', 'DO NOT POPULATE', 'DO NOT FIT',
})

_DEFAULT_FEEDER_WIDTH = 8


def _normalize_row(raw: dict[str, str]) -> dict[str, str]:
    return {_COL_ALIASES.get(k, k): v for k, v in raw.items()}


def _detect_csv_delimiter(path: Path) -> str:
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                return ';' if line.count(';') > line.count(',') else ','
    return ','


def _clean_field(value: str) -> str:
    """Convert European decimal commas and strip characters unsafe in job files."""
    value = re.sub(r'(\d),(\d)', r'\1.\2', value)
    return value.replace(',', '_').replace(';', '_')


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


def load_bom(path: Path, include_dnm: bool = False) -> list[Placement]:
    """Load a BOM (.csv or space-separated .txt) and return Placement objects."""
    suffix = path.suffix.lower()
    placements: list[Placement] = []
    skipped_dnm: list[str] = []

    with open(path, newline='', encoding='utf-8') as f:
        if suffix == '.csv':
            delimiter = _detect_csv_delimiter(path)
            reader = csv.DictReader(f, delimiter=delimiter)
            rows = list(reader)
        else:
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
            if not lines:
                return []
            headers = lines[0].split()
            rows = [dict(zip(headers, line.split())) for line in lines[1:] if line.strip()]

    for lineno, raw in enumerate(rows, start=2):
        row = _normalize_row(raw)
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

        if value.upper() in _DNM_VALUES:
            if not include_dnm:
                skipped_dnm.append(refdes)
                continue

        raw_fw = row.get('feeder_width', '').strip()
        try:
            feeder_width: Optional[int] = int(raw_fw) if raw_fw else None
        except ValueError:
            feeder_width = None

        raw_fr     = row.get('feeder_row', '').strip().upper()
        feeder_row = raw_fr if raw_fr in ('FRONT', 'REAR') else None

        placements.append(Placement(
            refdes=refdes, value=value, x=x, y=y, angle=angle, package=package,
            feeder_width=feeder_width, feeder_row=feeder_row,
            nozzle_type=row.get('nozzle_type', '').strip(),
            name=_clean_field(row.get('name', '').strip()),
        ))

    if skipped_dnm:
        print(
            f"  INFO: {len(skipped_dnm)} DNM/DNP component(s) skipped: "
            f"{', '.join(skipped_dnm[:10])}" + (' …' if len(skipped_dnm) > 10 else ''),
            file=sys.stderr,
        )
    return placements


# ─── Package rules ────────────────────────────────────────────────────────────

def load_package_rules(path: Path) -> list[PackageRule]:
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
                pattern=pattern, match_type=match_type,
                feeder_width=feeder_width, feeder_row=feeder_row,
                nozzle_type=nozzle, notes=notes,
            ))
    return rules


# ─── Timing config ────────────────────────────────────────────────────────────

def load_timing_config(path: Path) -> TimingConfig:
    cfg = TimingConfig()
    if not path.exists():
        return cfg
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            param = row.get('parameter', '').strip()
            raw   = row.get('value', '').strip()
            if not param or not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                print(f"WARNING: timing_config: invalid value for '{param}': {raw!r}",
                      file=sys.stderr)
                continue
            if   param == 'front_time_min': cfg.front_time_min = val
            elif param == 'front_time_max': cfg.front_time_max = val
            elif param == 'rear_time_min':  cfg.rear_time_min  = val
            elif param == 'rear_time_max':  cfg.rear_time_max  = val
            else:
                print(f"WARNING: timing_config: unknown parameter '{param}' ignored",
                      file=sys.stderr)
    return cfg


# ─── Component CSV ────────────────────────────────────────────────────────────

_COMP_CSV_FIELDS = [
    'value', 'package', 'count',
    'feeder_width', 'feeder_row', 'nozzle_type', 'name',
    'matched_by', 'status',
]


def write_components_csv(components: list[ComponentType], path: Path) -> None:
    sorted_comps = sorted(
        components,
        key=lambda c: (component_status(c) != 'INCOMPLETE', c.package, c.value),
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
                'status':       component_status(comp),
            })


def load_components_csv(path: Path) -> dict[tuple, dict]:
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
    return list(dict.fromkeys(missing_keys))


def update_package_rules_from_manual(
    components:     list[ComponentType],
    rules:          list[PackageRule],
    pkg_rules_path: Path,
) -> int:
    """Append package rules for any MANUAL component not yet covered by existing rules."""
    new_lines: list[str] = []
    for comp in components:
        if comp.matched_by != 'MANUAL':
            continue
        if component_status(comp) != 'OK':
            continue
        if any(PnPOptimizer.match_rule(comp.package, r) for r in rules):
            continue
        fw = str(comp.feeder_width) if comp.feeder_width else ''
        fr = comp.feeder_row or ''
        nt = comp.nozzle_type or ''
        new_lines.append(f"{comp.package},exact,{fw},{fr},{nt},auto-learned")

    if new_lines:
        existing = pkg_rules_path.read_text(encoding='utf-8')
        with open(pkg_rules_path, 'a', encoding='utf-8') as f:
            if '# Auto-learned' not in existing:
                f.write('#\n# Auto-learned from manual component entries\n')
            for line in new_lines:
                f.write(line + '\n')
    return len(new_lines)


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
                a.slot, a.slots_consumed, f"{a.physical_x_mm:.1f}", a.row,
                a.reel_index, a.component.value, a.component.package,
                a.component.feeder_width, a.component.nozzle_type,
                a.component.name, a.component.count,
                PnPOptimizer.slot_mod_group(a.slot),
            ])


def _sanitize(s: str) -> str:
    return s.replace(' ', '_').replace(',', '').replace(';', '').replace('\t', '_') or 'N/A'


def write_job_file(
    sequences: list[list[Placement]],
    path:      Path,
    opt:       PnPOptimizer,
) -> None:
    total_placements = sum(len(s) for s in sequences)
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# ClaudePnP job file\n")
        f.write(f"# Sequences: {len(sequences)}  Total placements: {total_placements}\n")
        f.write("#\n")
        for seq_idx, seq in enumerate(sequences, start=1):
            row_label    = 'FRONT' if seq[0].feeder_slot in PnPOptimizer.FRONT_SLOTS else 'REAR'
            pick_groups  = opt.simultaneous_pick_groups(seq)
            f.write(f"# SEQ {seq_idx:04d}  row={row_label}  "
                    f"placements={len(seq)}  pick_descends={len(pick_groups)}\n")
            for gi, grp in enumerate(pick_groups, start=1):
                f.write(f"#   pick {gi}: slots [{' '.join(str(p.feeder_slot) for p in grp)}]"
                        f" refs [{' '.join(p.refdes for p in grp)}]\n")
            for p in seq:
                f.write(f"{_sanitize(p.refdes)} {_sanitize(p.value)} "
                        f"{p.x:.4f} {p.y:.4f} {p.angle:.2f} "
                        f"{_sanitize(p.package)} {_sanitize(p.name)}\n")
            f.write('\n')


def write_nozzle_config_csv(
    head_config: dict[str, int],
    components:  list[ComponentType],
    path:        Path,
) -> None:
    counts: dict[str, int] = defaultdict(int)
    for comp in components:
        if comp.nozzle_type:
            counts[comp.nozzle_type] += comp.count

    est_cycles_per = {
        nz: math.ceil(counts[nz] / h)
        for nz, h in head_config.items() if counts.get(nz, 0) > 0
    }
    est_cycles       = max(est_cycles_per.values()) if est_cycles_per else 0
    total_placements = sum(counts[nz] for nz in head_config if counts.get(nz, 0) > 0)
    total_heads      = sum(head_config.values())
    utilisation      = (total_placements / (total_heads * est_cycles) * 100
                        if est_cycles > 0 else 0.0)

    with open(path, 'w', newline='', encoding='utf-8') as f:
        f.write('# Optimised nozzle head configuration\n')
        f.write('# nozzle_type,heads_assigned,placements,est_cycles\n')
        for nz in sorted(head_config):
            f.write(f'# {nz},{head_config[nz]},{counts.get(nz, 0)},{est_cycles_per.get(nz, 0)}\n')
        f.write(f'# Estimated pick cycles: {est_cycles}'
                f'  (head utilisation: {utilisation:.0f}%)\n')
        f.write('head,nozzle_type\n')
        head_num = 1
        for nz in sorted(head_config):
            for _ in range(head_config[nz]):
                f.write(f'{head_num},{nz}\n')
                head_num += 1


# ─── Reporting ────────────────────────────────────────────────────────────────

def capacity_report(
    components:   list[ComponentType],
    feeder_specs: dict[int, FeederSpec],
) -> None:
    OPT = PnPOptimizer
    for row, first, last in (('FRONT', OPT.FRONT_SLOT_FIRST, OPT.FRONT_SLOT_LAST),
                              ('REAR',  OPT.REAR_SLOT_FIRST,  OPT.REAR_SLOT_LAST)):
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


def print_summary(
    sequences:   list[list[Placement]],
    head_config: dict[str, int],
    opt:         PnPOptimizer,
) -> None:
    def _fmt(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    stats = opt.cycle_stats(sequences, head_config)
    est, lo, hi = opt.board_time_estimate(sequences)

    print(f"\n  Pick cycles       : {stats['cycles']}"
          f"  (ideal minimum: {stats['ideal_min']})")
    print(f"  Total placements  : {stats['total_placements']}")
    print(f"  Head utilisation  : {stats['utilisation_pct']:.0f}%")
    print(f"  Pick descends     : {stats['total_descends']}")
    print(f"  Best simultaneous : {stats['max_simultaneous']} components per descend")
    print(f"  Est. board time   : {_fmt(est)}  (range {_fmt(lo)} – {_fmt(hi)})")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='ClaudePnP — SMT Pick and Place Optimization Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--bom', '-b', required=True,
                        help='Input BOM file (.csv or .txt)')
    parser.add_argument('--feeder-table', '-f', default='feeder_table.csv')
    parser.add_argument('--package-rules',       default='package_rules.csv')
    parser.add_argument('--output-dir', '-o',    default='.')
    parser.add_argument('--job-prefix', '-p',    default=None)
    parser.add_argument('--multi-reel', '-m',    action='store_true')
    parser.add_argument('--multi-reel-threshold', '-t', type=int, default=20, metavar='N')
    parser.add_argument('--include-dnm',         action='store_true')
    parser.add_argument('--machines',    type=int,   default=1,   metavar='N')
    parser.add_argument('--timing-config',           default='timing_config.csv')
    parser.add_argument('--heads',       type=int,   default=8,   metavar='N')
    parser.add_argument('--machine1-skew', type=float, default=0.0, metavar='PCT')
    parser.add_argument('--report', action='store_true',
                        help='Generate a PDF documentation report '
                             '(<prefix>_report.pdf) with all statistics and tables')
    args = parser.parse_args()

    bom_path          = Path(args.bom)
    feeder_table_path = Path(args.feeder_table)
    pkg_rules_path    = Path(args.package_rules)
    output_dir        = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    job_prefix        = args.job_prefix or bom_path.stem
    components_path   = output_dir / f"{job_prefix}_components.csv"

    timing_path = Path(args.timing_config)
    timing = load_timing_config(timing_path)
    if timing_path.exists():
        print(f"Loading timing config : {timing_path}")
    else:
        print(f"Timing config        : {timing_path} not found — using defaults"
              f" ({timing.front_time_min}–{timing.front_time_max}s FRONT,"
              f" {timing.rear_time_min}–{timing.rear_time_max}s REAR)")

    opt = PnPOptimizer(n_heads=args.heads, timing=timing)

    print(f"Loading feeder table : {feeder_table_path}")
    feeder_specs = load_feeder_table(feeder_table_path)
    print(f"  {len(feeder_specs)} feeder widths: {sorted(feeder_specs)}")

    print(f"Loading BOM          : {bom_path}")
    placements = load_bom(bom_path, include_dnm=args.include_dnm)
    if not placements:
        print("ERROR: No placements loaded. Exiting.", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(placements)} placements loaded")

    # ── Phase 1 — Component enrichment ────────────────────────────────────────
    print(f"\nPhase 1 — Component enrichment")

    existing_configs: dict[tuple, dict] = {}
    if components_path.exists():
        existing_configs = load_components_csv(components_path)
        print(f"  Existing component file found — preserving manual entries")

    components = group_components(placements)
    print(f"  {len(components)} unique component types")

    for comp in components:
        key = (comp.value, comp.package)
        if key in existing_configs:
            prev = existing_configs[key]
            if prev.get('matched_by') == 'MANUAL' or prev.get('feeder_width'):
                comp.feeder_width = prev['feeder_width'] or comp.feeder_width
                comp.feeder_row   = prev['feeder_row']   or comp.feeder_row
                comp.nozzle_type  = prev['nozzle_type']  or comp.nozzle_type
                comp.name         = prev['name']         or comp.name
                comp.matched_by   = prev['matched_by']

    pkg_rules: list[PackageRule] = []
    if pkg_rules_path.exists():
        pkg_rules = load_package_rules(pkg_rules_path)
        print(f"  Loaded {len(pkg_rules)} package rules from {pkg_rules_path}")
        opt.apply_package_rules(components, pkg_rules)
    else:
        print(f"  WARNING: {pkg_rules_path} not found — skipping auto-match",
              file=sys.stderr)

    write_components_csv(components, components_path)

    if pkg_rules_path.exists():
        n_learned = update_package_rules_from_manual(components, pkg_rules, pkg_rules_path)
        if n_learned:
            print(f"  Learned {n_learned} new package rule(s) from manual entries"
                  f" → {pkg_rules_path}")

    ok         = [c for c in components if component_status(c) == 'OK']
    incomplete = [c for c in components if component_status(c) == 'INCOMPLETE']
    print(f"  Matched  : {len(ok)} component type(s)")
    if incomplete:
        print(f"  INCOMPLETE: {len(incomplete)} component type(s) need manual input:\n")
        print(f"  {'Value':<20} {'Package':<30} {'Missing fields'}")
        print(f"  {'-'*20} {'-'*30} {'-'*20}")
        for c in incomplete:
            missing = [f for f in REQUIRED_COMP_FIELDS if not getattr(c, f)]
            print(f"  {c.value:<20} {c.package:<30} {', '.join(missing)}")
        print(f"\n  Component file written: {components_path}")
        print(f"  Fill in the INCOMPLETE rows and re-run to continue.\n")
        sys.exit(0)

    print(f"  All component types resolved — proceeding to slot assignment")
    print(f"  Component file written: {components_path}")

    # ── Phase 2 — Slot assignment and sequence generation ─────────────────────
    n_machines = args.machines
    print(f"\nPhase 2 — Slot assignment and sequence generation"
          + (f" ({n_machines} machines)" if n_machines > 1 else ""))

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

    components = group_components(placements)
    partitions = opt.split_components_across_machines(
        components, n_machines, machine1_skew=args.machine1_skew
    )

    output_files: list[Path] = [components_path]
    report_machines: list[dict] = []

    for machine_idx, machine_components in enumerate(partitions, start=1):
        machine_label      = f"_machine{machine_idx}" if n_machines > 1 else ""
        machine_placements = [p for c in machine_components for p in c.placements]

        if n_machines > 1:
            total_p = sum(c.count for c in machine_components)
            small_p = sum(c.count for c in machine_components
                          if c.nozzle_type in PnPOptimizer._SMALL_NOZZLES)
            print(f"\n── Machine {machine_idx}  ({total_p} placements,"
                  f" {len(machine_components)} component types) ──")
            print(f"   Nozzle mix: small (#500–#503) {small_p}"
                  f"  /  large (#504+) {total_p - small_p}")

        print("\nSlot capacity:")
        capacity_report(machine_components, feeder_specs)

        print("\nAssigning feeder slots...")
        assignments = opt.assign_slots(
            machine_components, feeder_specs,
            multi_reel=args.multi_reel,
            multi_reel_threshold=args.multi_reel_threshold,
        )
        print(f"  {len(assignments)} feeder reels assigned")
        if args.multi_reel:
            print(f"  Multi-reel ON (threshold: {args.multi_reel_threshold} placements/reel)")

        print("\nOptimising nozzle head configuration...")
        try:
            head_config = opt.optimize_head_config(machine_components)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        for nz in sorted(head_config):
            print(f"  {head_config[nz]} × {nz}")

        print("\nBuilding pick sequences...")
        sequences = opt.build_sequences(machine_placements, head_config)
        print_summary(sequences, head_config, opt)

        feeder_csv  = output_dir / f"{job_prefix}{machine_label}_feeders.csv"
        job_txt     = output_dir / f"{job_prefix}{machine_label}_sequence.txt"
        nozzle_csv  = output_dir / f"{job_prefix}{machine_label}_nozzle_config.csv"

        write_feeder_csv(assignments, feeder_csv)
        write_job_file(sequences, job_txt, opt)
        write_nozzle_config_csv(head_config, machine_components, nozzle_csv)

        output_files.extend([feeder_csv, job_txt, nozzle_csv])

        if args.report:
            report_machines.append({
                'label':        f"Machine {machine_idx}" if n_machines > 1 else "",
                'stats':        opt.cycle_stats(sequences, head_config),
                'board_time':   opt.board_time_estimate(sequences),
                'head_config':  head_config,
                'feeders_csv':  feeder_csv,
                'sequence_txt': job_txt,
                'nozzle_csv':   nozzle_csv,
            })

    if args.report:
        import report as report_mod
        report_pdf = output_dir / f"{job_prefix}_report.pdf"
        report_mod.write_pdf_report(
            output_path=report_pdf,
            job_name=job_prefix,
            machines=report_machines,
            components_csv=components_path,
            n_heads=args.heads,
        )
        output_files.append(report_pdf)

    print(f"\nOutputs written:")
    for out_path in output_files:
        print(f"  {out_path}")


if __name__ == '__main__':
    main()
