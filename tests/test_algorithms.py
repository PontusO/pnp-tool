"""Tests for the core optimisation algorithms."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import claudepnp as c


# ── _greedy_head_alloc ────────────────────────────────────────────────────────

class TestGreedyHeadAlloc:
    def test_single_nozzle_gets_all_heads(self):
        h = c._greedy_head_alloc({"#501": 50}, 8)
        assert h == {"#501": 8}

    def test_sum_equals_n_heads(self):
        counts = {"#501": 60, "#502": 10, "#503": 5, "#504": 15}
        h = c._greedy_head_alloc(counts, 8)
        assert sum(h.values()) == 8

    def test_each_type_gets_at_least_one(self):
        counts = {"#501": 60, "#502": 1, "#503": 1}
        h = c._greedy_head_alloc(counts, 8)
        for nozzle in counts:
            assert h[nozzle] >= 1

    def test_bottleneck_minimised(self):
        counts = {"#501": 60, "#504": 10}
        h = c._greedy_head_alloc(counts, 8)
        cycles_501 = math.ceil(60 / h["#501"])
        cycles_504 = math.ceil(10 / h["#504"])
        bottleneck = max(cycles_501, cycles_504)
        # Brute-force verify no better split exists
        for h1 in range(1, 8):
            h4 = 8 - h1
            if h4 < 1:
                continue
            alt = max(math.ceil(60 / h1), math.ceil(10 / h4))
            assert bottleneck <= alt

    def test_equal_counts_distributed_evenly(self):
        counts = {"#501": 10, "#502": 10}
        h = c._greedy_head_alloc(counts, 8)
        assert h["#501"] == h["#502"] == 4

    def test_empty_counts_returns_empty(self):
        assert c._greedy_head_alloc({}, 8) == {}

    def test_raises_when_types_exceed_heads(self):
        counts = {f"#{500+i}": 1 for i in range(9)}  # 9 types
        with pytest.raises(ValueError, match="heads"):
            c._greedy_head_alloc(counts, 8)

    def test_exact_fit_one_head_each(self):
        counts = {"#501": 1, "#502": 1, "#503": 1, "#504": 1,
                  "#505": 1, "#506": 1, "#507": 1, "#508": 1}
        h = c._greedy_head_alloc(counts, 8)
        assert all(v == 1 for v in h.values())


# ── optimize_head_config ──────────────────────────────────────────────────────

class TestOptimizeHeadConfig:
    def _make_comp(self, nozzle, count):
        comp = c.ComponentType(value="X", package="X", nozzle_type=nozzle)
        comp.placements = [object()] * count   # lightweight stand-ins
        return comp

    def test_sums_to_n_heads(self):
        comps = [self._make_comp("#501", 50), self._make_comp("#504", 10)]
        h = c.optimize_head_config(comps, n_heads=8)
        assert sum(h.values()) == 8

    def test_components_without_nozzle_ignored(self):
        comps = [self._make_comp("", 10), self._make_comp("#501", 20)]
        h = c.optimize_head_config(comps, n_heads=8)
        assert "" not in h
        assert sum(h.values()) == 8


# ── _component_status ─────────────────────────────────────────────────────────

class TestComponentStatus:
    def _comp(self, fw=8, fr="FRONT", nz="#501"):
        comp = c.ComponentType(value="V", package="P")
        comp.feeder_width = fw
        comp.feeder_row   = fr
        comp.nozzle_type  = nz
        return comp

    def test_ok_when_all_fields_set(self):
        assert c._component_status(self._comp()) == "OK"

    def test_incomplete_when_feeder_width_missing(self):
        assert c._component_status(self._comp(fw=None)) == "INCOMPLETE"

    def test_incomplete_when_feeder_row_missing(self):
        assert c._component_status(self._comp(fr="")) == "INCOMPLETE"

    def test_incomplete_when_nozzle_type_missing(self):
        assert c._component_status(self._comp(nz="")) == "INCOMPLETE"

    def test_incomplete_when_all_missing(self):
        comp = c.ComponentType(value="V", package="P")
        assert c._component_status(comp) == "INCOMPLETE"


# ── split_components_across_machines ─────────────────────────────────────────

class TestSplitAcrossMachines:
    def _make_comp(self, nozzle, count):
        comp = c.ComponentType(value="V", package="P", nozzle_type=nozzle)
        comp.placements = [object()] * count
        return comp

    def test_single_machine_returns_all(self):
        comps = [self._make_comp("#501", 10), self._make_comp("#504", 5)]
        parts = c.split_components_across_machines(comps, 1)
        assert len(parts) == 1
        assert len(parts[0]) == 2

    def test_two_machine_total_matches(self):
        comps = [self._make_comp("#501", 10), self._make_comp("#504", 5),
                 self._make_comp("#502", 8)]
        parts = c.split_components_across_machines(comps, 2)
        assert sum(len(p) for p in parts) == 3

    def test_total_placements_preserved(self):
        comps = [self._make_comp("#501", 50), self._make_comp("#502", 30),
                 self._make_comp("#503", 10)]
        parts = c.split_components_across_machines(comps, 2)
        total = sum(sum(c.count for c in part) for part in parts)
        assert total == 90

    def test_skew_biases_small_nozzle_to_machine1(self):
        # With heavy skew, #501 (small) should all go to machine 1.
        comps = [self._make_comp("#501", 40), self._make_comp("#504", 40)]
        parts = c.split_components_across_machines(comps, 2, machine1_skew=100)
        m1_small = sum(comp.count for comp in parts[0]
                       if comp.nozzle_type in c._SMALL_NOZZLES)
        assert m1_small == 40   # all passives on machine 1

    def test_zero_skew_approximately_balanced(self):
        comps = [self._make_comp("#501", 50), self._make_comp("#504", 50)]
        parts = c.split_components_across_machines(comps, 2, machine1_skew=0)
        totals = [sum(comp.count for comp in part) for part in parts]
        # Allow up to one full component's worth of imbalance.
        assert abs(totals[0] - totals[1]) <= 50


# ── simultaneous_pick_groups ──────────────────────────────────────────────────

class TestSimultaneousPickGroups:
    def _placement(self, slot, nozzle):
        p = c.Placement(refdes="R1", value="10K", x=0, y=0, angle=0, package="R0402")
        p.feeder_slot  = slot
        p.nozzle_type  = nozzle
        return p

    def test_same_mod_group_can_be_simultaneous(self):
        # Slots 1, 4, 7 are all mod-group 0 in FRONT.
        seq = [
            self._placement(1,  "#501"),
            self._placement(4,  "#502"),
            self._placement(7,  "#503"),
        ]
        groups = c.simultaneous_pick_groups(seq)
        # All three can go in one group (same mod group, distinct nozzles).
        sizes = sorted(len(g) for g in groups)
        assert sizes == [3]

    def test_same_nozzle_type_cannot_share_group(self):
        # Two #501 components: even if slots are aligned they need different heads.
        seq = [
            self._placement(1, "#501"),
            self._placement(4, "#501"),
        ]
        groups = c.simultaneous_pick_groups(seq)
        # Must be in separate groups since they'd reuse the same head.
        assert all(len(g) == 1 for g in groups)

    def test_different_mod_groups_are_separate(self):
        # Slots 1 (mod 0) and 2 (mod 1) cannot be picked simultaneously.
        seq = [
            self._placement(1, "#501"),
            self._placement(2, "#502"),
        ]
        groups = c.simultaneous_pick_groups(seq)
        assert all(len(g) == 1 for g in groups)

    def test_max_simultaneous_respected(self):
        # MAX_SIMULTANEOUS = 4; a mod-group with 5 distinct-nozzle slots
        # should be split into groups of ≤ 4.
        seq = [self._placement(1 + i * 3, f"#{501+i}") for i in range(5)]
        groups = c.simultaneous_pick_groups(seq)
        assert all(len(g) <= c.MAX_SIMULTANEOUS for g in groups)

    def test_all_placements_assigned(self):
        seq = [self._placement(1 + i * 3, f"#{501+i}") for i in range(6)]
        groups = c.simultaneous_pick_groups(seq)
        total_assigned = sum(len(g) for g in groups)
        assert total_assigned == len(seq)


# ── assign_slots ─────────────────────────────────────────────────────────────

class TestAssignSlots:
    def test_all_placements_get_slots(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert p.feeder_slot is not None

    def test_slots_in_valid_range(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert 1 <= p.feeder_slot <= 70

    def test_no_slot_double_assigned(self, fully_resolved_pipeline):
        # Each 8mm slot can only be occupied by one component.
        # (Wider feeders occupy consecutive slots; check no two assignments
        # overlap.)
        assignments = fully_resolved_pipeline["assignments"]
        occupied = set()
        for a in assignments:
            for s in range(a.slot, a.slot + a.slots_consumed):
                assert s not in occupied, f"Slot {s} double-assigned"
                occupied.add(s)

    def test_front_components_in_front_slots(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert p.feeder_slot in c.FRONT_SLOTS, \
                f"{p.refdes} expected in FRONT but got slot {p.feeder_slot}"

    def test_high_volume_component_near_centre(self, fully_resolved_pipeline):
        # The component with the most placements (10K R0402, count=2) should
        # land closer to centre than the single-placement component (C0805).
        assignments = fully_resolved_pipeline["assignments"]
        by_comp = {a.component.value: a.slot for a in assignments}
        dist_10k = abs(c._slot_physical_x(by_comp["10K"]) - c.MACHINE_CENTER_MM)
        dist_10uF = abs(c._slot_physical_x(by_comp["10uF"]) - c.MACHINE_CENTER_MM)
        assert dist_10k <= dist_10uF
