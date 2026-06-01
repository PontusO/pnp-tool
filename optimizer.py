"""
PnP optimisation engine — machine-geometry constants, data classes, and the
PnPOptimizer class that owns all algorithmic logic.

claudepnp.py handles file I/O and CLI orchestration; this module has no
file I/O and no dependency on claudepnp.
"""

import math
import re as _re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FeederSpec:
    width_mm:       int
    slots_consumed: int
    description:    str


@dataclass
class TimingConfig:
    """Per-machine placement-time calibration (seconds per component)."""
    front_time_min: float = 0.5
    front_time_max: float = 0.7
    rear_time_min:  float = 1.0
    rear_time_max:  float = 1.2


@dataclass
class Placement:
    """One component instance to be placed on the PCB."""
    refdes:       str
    value:        str
    x:            float
    y:            float
    angle:        float
    package:      str
    feeder_width: Optional[int] = None
    feeder_row:   Optional[str] = None
    nozzle_type:  str           = ''
    name:         str           = ''
    feeder_slot:  Optional[int] = field(default=None, repr=False)
    reel_index:   int           = field(default=0,    repr=False)


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
    matched_by:   str           = ''

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
        return PnPOptimizer.slot_physical_x(self.slot)

    @property
    def row(self) -> str:
        return self.component.feeder_row


@dataclass
class PackageRule:
    pattern:      str
    match_type:   str
    feeder_width: Optional[int]
    feeder_row:   Optional[str]
    nozzle_type:  str
    notes:        str


# Required fields that must be non-empty for a component to be production-ready.
REQUIRED_COMP_FIELDS = ('feeder_width', 'feeder_row', 'nozzle_type')


def component_status(comp: ComponentType) -> str:
    missing = [f for f in REQUIRED_COMP_FIELDS if not getattr(comp, f, None)]
    return 'INCOMPLETE' if missing else 'OK'


def group_components(placements: list[Placement]) -> list[ComponentType]:
    """Group Placement objects into ComponentType objects keyed on (value, package)."""
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


# ─── PnPOptimizer ─────────────────────────────────────────────────────────────

class PnPOptimizer:
    """
    Optimises feeder slot assignment and pick sequences for a Juki PnP machine.

    Machine-geometry constants reflect the physical layout of the target machine
    family (70-slot dual-row feeder bank, 8-nozzle head, mod-3 simultaneous
    pick).  Operator-configurable values (number of heads, placement timing) are
    set via the constructor.
    """

    # ── Machine geometry (physical constants) ─────────────────────────────────
    SLOT_WIDTH_MM      = 8
    NOZZLE_PITCH_SLOTS = 3
    MAX_SIMULTANEOUS   = 4

    FRONT_SLOT_FIRST = 1
    FRONT_SLOT_LAST  = 38
    REAR_SLOT_FIRST  = 39
    REAR_SLOT_LAST   = 70

    FRONT_SLOTS = frozenset(range(1,  39))
    REAR_SLOTS  = frozenset(range(39, 71))

    FRONT_CENTER         = 19.5
    REAR_CENTER          = 51.5
    MACHINE_CENTER_MM    = 148.0
    MAX_RACK_DISTANCE_MM = 148.0

    _SMALL_NOZZLES = frozenset({'#500', '#501', '#502', '#503'})

    def __init__(self, n_heads: int = 8, timing: Optional[TimingConfig] = None):
        self.n_heads = n_heads
        self.timing  = timing if timing is not None else TimingConfig()

    # ── Slot geometry ─────────────────────────────────────────────────────────

    @classmethod
    def slot_physical_x(cls, slot: int) -> float:
        """Physical X position (mm) of the leftmost edge of a slot."""
        if slot in cls.FRONT_SLOTS:
            return (slot - 1) * cls.SLOT_WIDTH_MM
        return (cls.REAR_SLOT_LAST - slot) * cls.SLOT_WIDTH_MM

    @classmethod
    def slot_mod_group(cls, slot: int) -> int:
        """Simultaneous-pick alignment group (0, 1, or 2)."""
        if slot in cls.FRONT_SLOTS:
            return (slot - 1) % cls.NOZZLE_PITCH_SLOTS
        return (cls.REAR_SLOT_LAST - slot) % cls.NOZZLE_PITCH_SLOTS

    @classmethod
    def row_valid_slots(cls, row: str) -> frozenset:
        return cls.FRONT_SLOTS if row == 'FRONT' else cls.REAR_SLOTS

    @classmethod
    def center_out_order(cls, row: str) -> list[int]:
        """Slot numbers for a row sorted by distance from the rack centre."""
        if row == 'FRONT':
            slots, center = list(range(cls.FRONT_SLOT_FIRST, cls.FRONT_SLOT_LAST + 1)), cls.FRONT_CENTER
        else:
            slots, center = list(range(cls.REAR_SLOT_FIRST,  cls.REAR_SLOT_LAST  + 1)), cls.REAR_CENTER
        return sorted(slots, key=lambda s: (abs(s - center), s))

    # ── Placement timing ──────────────────────────────────────────────────────

    def placement_time(self, slot: int) -> float:
        """Estimated seconds to pick and place one component from this slot."""
        dist_frac = min(1.0, abs(self.slot_physical_x(slot) - self.MACHINE_CENTER_MM)
                         / self.MAX_RACK_DISTANCE_MM)
        t = self.timing
        if slot in self.FRONT_SLOTS:
            return t.front_time_min + dist_frac * (t.front_time_max - t.front_time_min)
        return t.rear_time_min + dist_frac * (t.rear_time_max - t.rear_time_min)

    # ── Spatial helpers ───────────────────────────────────────────────────────

    @staticmethod
    def pcb_distance(a: Placement, b: Placement) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    @classmethod
    def feeder_distance(cls, a: Placement, b: Placement) -> float:
        xa = cls.slot_physical_x(a.feeder_slot) if a.feeder_slot else 0.0
        xb = cls.slot_physical_x(b.feeder_slot) if b.feeder_slot else 0.0
        return abs(xa - xb)

    @staticmethod
    def nearest_neighbor_tour(placements: list[Placement]) -> list[Placement]:
        """Greedy nearest-neighbour ordering by PCB position."""
        if not placements:
            return []
        remaining = list(placements)
        start = min(remaining, key=lambda p: math.hypot(p.x, p.y))
        tour = [start]
        remaining.remove(start)
        while remaining:
            last    = tour[-1]
            nearest = min(remaining, key=lambda p: math.hypot(last.x - p.x, last.y - p.y))
            tour.append(nearest)
            remaining.remove(nearest)
        return tour

    # ── Package-rule matching ─────────────────────────────────────────────────

    @staticmethod
    def match_rule(package: str, rule: PackageRule) -> bool:
        """True if the package name satisfies the rule (case-insensitive)."""
        pkg = package.upper()
        pat = rule.pattern.upper()
        if rule.match_type == 'exact':
            return pkg == pat
        if rule.match_type == 'prefix':
            return pkg.startswith(pat)
        if rule.match_type == 'contains':
            return pat in pkg
        if rule.match_type == 'regex':
            return bool(_re.search(rule.pattern, package, _re.IGNORECASE))
        return False

    def apply_package_rules(
        self,
        components: list[ComponentType],
        rules:      list[PackageRule],
    ) -> None:
        """
        Fill feeder_width, feeder_row, and nozzle_type for unresolved
        components.  Only the first matching rule applies (top-down priority).
        Sets matched_by on each component that was resolved.
        """
        for comp in components:
            if comp.matched_by:
                continue
            for rule in rules:
                if not self.match_rule(comp.package, rule):
                    continue
                tag = f"{rule.match_type}:{rule.pattern}"
                if comp.feeder_width is None and rule.feeder_width is not None:
                    comp.feeder_width = rule.feeder_width
                if not comp.feeder_row and rule.feeder_row:
                    comp.feeder_row = rule.feeder_row
                if not comp.nozzle_type and rule.nozzle_type:
                    comp.nozzle_type = rule.nozzle_type
                comp.matched_by = tag
                break

    # ── Head configuration optimisation ──────────────────────────────────────

    def _greedy_head_alloc(self, counts: dict[str, int]) -> dict[str, int]:
        """
        Allocate self.n_heads across nozzle types to minimise
        max_i(ceil(P_i / h_i)).  Greedy bottleneck-reduction algorithm.
        """
        active = sorted(counts)
        k      = len(active)
        if k == 0:
            return {}
        if k > self.n_heads:
            raise ValueError(
                f"{k} distinct nozzle types but only {self.n_heads} heads available. "
                f"Increase --heads or reduce nozzle types on this machine."
            )
        h: dict[str, int] = {nz: 1 for nz in active}
        for _ in range(self.n_heads - k):
            worst = max(active, key=lambda nz: counts[nz] / h[nz])
            h[worst] += 1
        return h

    def optimize_head_config(self, components: list[ComponentType]) -> dict[str, int]:
        """Return the optimal nozzle-to-head allocation for these components."""
        counts: dict[str, int] = defaultdict(int)
        for comp in components:
            if comp.nozzle_type:
                counts[comp.nozzle_type] += comp.count
        return self._greedy_head_alloc(dict(counts))

    # ── Feeder slot assignment ────────────────────────────────────────────────

    def assign_slots(
        self,
        components:           list[ComponentType],
        feeder_specs:         dict[int, FeederSpec],
        multi_reel:           bool,
        multi_reel_threshold: int,
    ) -> list[FeederAssignment]:
        """
        Assign feeder slots to all component types.

        Strategy: centre-out order, mod-group-0 preferred for simultaneous
        pick, overflow from preferred row to the other row if full.
        Placements are distributed round-robin across duplicate reels.
        """
        slot_free: dict[int, bool] = {s: True for s in range(1, self.REAR_SLOT_LAST + 1)}

        def _find_free_run(row: str, slots_needed: int, preferred_mod: Optional[int] = None) -> Optional[int]:
            valid = self.row_valid_slots(row)
            for start in self.center_out_order(row):
                if preferred_mod is not None and self.slot_mod_group(start) != preferred_mod:
                    continue
                run = range(start, start + slots_needed)
                if all(s in valid and slot_free.get(s, False) for s in run):
                    return start
            return None

        def _occupy(start: int, slots_needed: int) -> None:
            for s in range(start, start + slots_needed):
                slot_free[s] = False

        assignments:    list[FeederAssignment]              = []
        overflow_moves: list[tuple[ComponentType, str, str]] = []

        for preferred_row in ('FRONT', 'REAR'):
            row_comps = sorted(
                [c for c in components if c.feeder_row == preferred_row],
                key=lambda c: -c.count,
            )
            for comp in row_comps:
                spec = feeder_specs.get(comp.feeder_width)
                if spec is None:
                    import sys
                    print(
                        f"WARNING: No feeder spec for {comp.feeder_width}mm — "
                        f"skipping {comp.value} {comp.package}",
                        file=sys.stderr,
                    )
                    continue

                slots_needed = spec.slots_consumed
                num_reels = 1
                if multi_reel and comp.count >= multi_reel_threshold:
                    num_reels = min(self.MAX_SIMULTANEOUS,
                                    1 + comp.count // multi_reel_threshold)

                reel_start_slots: list[int] = []
                actual_row = preferred_row

                for reel_idx in range(num_reels):
                    start = _find_free_run(actual_row, slots_needed, preferred_mod=0)
                    if start is None:
                        start = _find_free_run(actual_row, slots_needed)
                    if start is None:
                        other_row = 'REAR' if actual_row == 'FRONT' else 'FRONT'
                        start = _find_free_run(other_row, slots_needed, preferred_mod=0)
                        if start is None:
                            start = _find_free_run(other_row, slots_needed)
                        if start is not None:
                            if reel_idx == 0:
                                overflow_moves.append((comp, preferred_row, other_row))
                            actual_row = other_row
                    if start is None:
                        import sys
                        print(
                            f"ERROR: No free slots for {comp.value} ({comp.package}) "
                            f"in either row — component will be unplaced.",
                            file=sys.stderr,
                        )
                        break
                    _occupy(start, slots_needed)
                    reel_start_slots.append(start)
                    assignments.append(FeederAssignment(
                        slot=start, slots_consumed=slots_needed,
                        reel_index=reel_idx, component=comp,
                    ))

                if not reel_start_slots:
                    continue
                if actual_row != preferred_row:
                    comp.feeder_row = actual_row
                    for p in comp.placements:
                        p.feeder_row = actual_row
                for i, placement in enumerate(comp.placements):
                    placement.feeder_slot = reel_start_slots[i % len(reel_start_slots)]
                    placement.reel_index  = i % len(reel_start_slots)

        if overflow_moves:
            import sys
            print(f"\n  NOTE: {len(overflow_moves)} component(s) moved to alternate row"
                  f" due to FRONT overflow:")
            for comp, from_row, to_row in overflow_moves:
                print(f"    {comp.value:<20} {comp.package:<30} {from_row} → {to_row}")
            print(f"  Consider splitting the job across two machines for optimal cycle time.")

        return assignments

    # ── Machine splitting ─────────────────────────────────────────────────────

    def split_components_across_machines(
        self,
        components:    list[ComponentType],
        n_machines:    int,
        machine1_skew: float = 0.0,
    ) -> list[list[ComponentType]]:
        """
        Two-pass nozzle-aware greedy split.

        Pass 1 — small-nozzle (#500–#503) components fill machine 1 first,
        up to its budget of fair_share × (1 + machine1_skew/100) placements.
        Pass 2 — large-nozzle components prefer machines 2+ (machine 1 fills
        last via tie-breaking).
        """
        if n_machines == 1:
            return [components]

        total      = sum(c.count for c in components)
        fair_share = total / n_machines
        m1_budget  = fair_share * (1 + machine1_skew / 100)

        group_s = sorted([c for c in components if c.nozzle_type in self._SMALL_NOZZLES],
                         key=lambda c: -c.count)
        group_l = sorted([c for c in components if c.nozzle_type not in self._SMALL_NOZZLES],
                         key=lambda c: -c.count)

        buckets: list[list[ComponentType]] = [[] for _ in range(n_machines)]
        totals:  list[int]                 = [0]  * n_machines

        for comp in group_s:
            if totals[0] + comp.count <= m1_budget:
                idx = 0
            else:
                idx = min(range(n_machines), key=lambda i: (totals[i], i))
            buckets[idx].append(comp)
            totals[idx] += comp.count

        for comp in group_l:
            idx = min(range(n_machines), key=lambda i: (totals[i], -i))
            buckets[idx].append(comp)
            totals[idx] += comp.count

        return buckets

    # ── Sequence building ─────────────────────────────────────────────────────

    def simultaneous_pick_groups(self, seq: list[Placement]) -> list[list[Placement]]:
        """
        Groups within a sequence that can be picked in one head descent.
        Constraints: same row, same mod-group, ≤ MAX_SIMULTANEOUS, unique nozzle types.
        """
        by_mod: dict[int, list[Placement]] = defaultdict(list)
        for p in seq:
            if p.feeder_slot is not None:
                by_mod[self.slot_mod_group(p.feeder_slot)].append(p)

        groups:   list[list[Placement]] = []
        assigned: set[int]              = set()

        for mod in sorted(by_mod, key=lambda m: -len(by_mod[m])):
            remaining = [p for p in by_mod[mod] if id(p) not in assigned]
            while remaining:
                grp:          list[Placement] = []
                seen_nozzles: set[str]        = set()
                leftover:     list[Placement] = []
                for p in remaining:
                    if len(grp) < self.MAX_SIMULTANEOUS and p.nozzle_type not in seen_nozzles:
                        grp.append(p)
                        seen_nozzles.add(p.nozzle_type)
                    else:
                        leftover.append(p)
                if grp:
                    groups.append(grp)
                    for p in grp:
                        assigned.add(id(p))
                remaining = leftover

        for p in seq:
            if id(p) not in assigned:
                groups.append([p])
        return groups

    def _bundle_rear_into_front(
        self,
        front_seqs:  list[list[Placement]],
        rear_seqs:   list[list[Placement]],
        head_config: dict[str, int],
    ) -> tuple[list[list[Placement]], list[list[Placement]]]:
        """
        Absorb sparse REAR placements into FRONT sequences where head capacity
        allows (checked against head_config).  Unabsorbed remainder is
        re-grouped into dedicated REAR cycles.
        """
        rear_queue: list[Placement] = [p for seq in rear_seqs for p in seq]
        new_front = [list(s) for s in front_seqs]

        for seq in new_front:
            if not rear_queue:
                break
            nozzle_used: dict[str, int] = defaultdict(int)
            for p in seq:
                nozzle_used[p.nozzle_type] += 1
            leftover: list[Placement] = []
            for p in rear_queue:
                h_i = head_config.get(p.nozzle_type, 1)
                if nozzle_used[p.nozzle_type] < h_i:
                    seq.append(p)
                    nozzle_used[p.nozzle_type] += 1
                else:
                    leftover.append(p)
            rear_queue = leftover

        new_rear: list[list[Placement]] = []
        while rear_queue:
            cycle:       list[Placement] = []
            nozzle_used: dict[str, int]  = defaultdict(int)
            leftover:    list[Placement] = []
            for p in rear_queue:
                h_i = head_config.get(p.nozzle_type, 1)
                if nozzle_used[p.nozzle_type] < h_i:
                    cycle.append(p)
                    nozzle_used[p.nozzle_type] += 1
                else:
                    leftover.append(p)
            if cycle:
                new_rear.append(cycle)
            rear_queue = leftover

        return new_front, new_rear

    def build_sequences(
        self,
        placements:  list[Placement],
        head_config: dict[str, int],
    ) -> list[list[Placement]]:
        """
        Build pick sequences interleaved by nozzle type to maximise head
        utilisation.  FRONT and REAR rows are sequenced separately; if the
        REAR pass is sparse (avg cycle < n_heads/2) its placements are absorbed
        into FRONT cycles instead.
        """
        front_seqs: list[list[Placement]] = []
        rear_seqs:  list[list[Placement]] = []

        for row in ('FRONT', 'REAR'):
            row_slots      = self.FRONT_SLOTS if row == 'FRONT' else self.REAR_SLOTS
            row_placements = [p for p in placements
                              if p.feeder_slot is not None and p.feeder_slot in row_slots]
            if not row_placements:
                continue

            nozzle_groups: dict[str, list[Placement]] = defaultdict(list)
            for p in row_placements:
                nozzle_groups[p.nozzle_type or ''].append(p)

            nozzle_order = sorted(nozzle_groups)
            tours = {nz: self.nearest_neighbor_tour(grp) for nz, grp in nozzle_groups.items()}
            pointers: dict[str, int] = {nz: 0 for nz in nozzle_order}

            row_seqs: list[list[Placement]] = []
            while any(pointers[nz] < len(tours[nz]) for nz in nozzle_order):
                cycle: list[Placement] = []
                for nz in nozzle_order:
                    h_i   = head_config.get(nz, 1)
                    ptr   = pointers[nz]
                    chunk = tours[nz][ptr: ptr + h_i]
                    cycle.extend(chunk)
                    pointers[nz] = ptr + len(chunk)
                if cycle:
                    row_seqs.append(cycle)

            if row == 'FRONT':
                front_seqs = row_seqs
            else:
                rear_seqs = row_seqs

        if front_seqs and rear_seqs:
            rear_total = sum(len(s) for s in rear_seqs)
            rear_avg   = rear_total / len(rear_seqs)
            if rear_avg < self.n_heads / 2:
                front_seqs, rear_seqs = self._bundle_rear_into_front(
                    front_seqs, rear_seqs, head_config,
                )
                remaining = sum(len(s) for s in rear_seqs)
                absorbed  = rear_total - remaining
                import sys
                print(f"  Sparse REAR: {absorbed}/{rear_total} placements bundled"
                      f" into FRONT cycles"
                      + (f", {remaining} remain as dedicated REAR." if remaining else "."))

        return front_seqs + rear_seqs

    # ── Summary statistics ────────────────────────────────────────────────────

    def board_time_estimate(
        self,
        sequences: list[list[Placement]],
    ) -> tuple[float, float, float]:
        """Return (estimated, minimum, maximum) board time in seconds."""
        placed = [p for seq in sequences for p in seq if p.feeder_slot is not None]
        t = self.timing
        est = sum(self.placement_time(p.feeder_slot) for p in placed)
        lo  = sum(t.front_time_min if p.feeder_slot in self.FRONT_SLOTS
                  else t.rear_time_min for p in placed)
        hi  = sum(t.front_time_max if p.feeder_slot in self.FRONT_SLOTS
                  else t.rear_time_max for p in placed)
        return est, lo, hi

    def cycle_stats(
        self,
        sequences:   list[list[Placement]],
        head_config: dict[str, int],
    ) -> dict:
        """
        Return a dict with cycle count, ideal minimum, utilisation %, pick
        descends, and best simultaneous pick count.
        """
        total      = sum(len(s) for s in sequences)
        est_cycles = len(sequences)

        nozzle_front: dict[str, int] = defaultdict(int)
        nozzle_rear:  dict[str, int] = defaultdict(int)
        for seq in sequences:
            bucket = nozzle_front if (seq and seq[0].feeder_slot in self.FRONT_SLOTS) \
                     else nozzle_rear
            for p in seq:
                bucket[p.nozzle_type or ''] += 1

        front_ideal = max(
            (math.ceil(cnt / head_config.get(nz, 1)) for nz, cnt in nozzle_front.items()),
            default=0,
        )
        rear_ideal = max(
            (math.ceil(cnt / head_config.get(nz, 1)) for nz, cnt in nozzle_rear.items()),
            default=0,
        )
        ideal_min   = front_ideal + rear_ideal
        utilisation = total / (self.n_heads * ideal_min) * 100 if ideal_min > 0 else 0.0

        all_groups    = [g for seq in sequences for g in self.simultaneous_pick_groups(seq)]
        total_descends = len(all_groups)
        max_sim        = max((len(g) for g in all_groups), default=0)

        return {
            'cycles':          est_cycles,
            'ideal_min':       ideal_min,
            'utilisation_pct': utilisation,
            'total_descends':  total_descends,
            'max_simultaneous': max_sim,
            'total_placements': total,
        }
