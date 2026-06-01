"""Tests for the core optimisation algorithms in PnPOptimizer."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from optimizer import (
    PnPOptimizer, ComponentType, Placement,
    component_status, REQUIRED_COMP_FIELDS,
)


# ── _greedy_head_alloc ────────────────────────────────────────────────────────

class TestGreedyHeadAlloc:
    def _opt(self, n=8):
        return PnPOptimizer(n_heads=n)

    def test_single_nozzle_gets_all_heads(self):
        assert self._opt()._greedy_head_alloc({"#501": 50}) == {"#501": 8}

    def test_sum_equals_n_heads(self):
        h = self._opt()._greedy_head_alloc({"#501": 60, "#502": 10, "#503": 5, "#504": 15})
        assert sum(h.values()) == 8

    def test_each_type_gets_at_least_one(self):
        h = self._opt()._greedy_head_alloc({"#501": 60, "#502": 1, "#503": 1})
        assert all(v >= 1 for v in h.values())

    def test_bottleneck_minimised(self):
        counts = {"#501": 60, "#504": 10}
        h = self._opt()._greedy_head_alloc(counts)
        bottleneck = max(math.ceil(counts[nz] / h[nz]) for nz in counts)
        for h1 in range(1, 8):
            h4 = 8 - h1
            if h4 < 1:
                continue
            alt = max(math.ceil(60 / h1), math.ceil(10 / h4))
            assert bottleneck <= alt

    def test_equal_counts_distributed_evenly(self):
        h = self._opt()._greedy_head_alloc({"#501": 10, "#502": 10})
        assert h["#501"] == h["#502"] == 4

    def test_empty_counts_returns_empty(self):
        assert self._opt()._greedy_head_alloc({}) == {}

    def test_raises_when_types_exceed_heads(self):
        counts = {f"#{500+i}": 1 for i in range(9)}
        with pytest.raises(ValueError, match="heads"):
            self._opt()._greedy_head_alloc(counts)

    def test_exact_fit_one_head_each(self):
        counts = {f"#{500+i}": 1 for i in range(8)}
        h = self._opt()._greedy_head_alloc(counts)
        assert all(v == 1 for v in h.values())


# ── optimize_head_config ──────────────────────────────────────────────────────

class TestOptimizeHeadConfig:
    def _comp(self, nozzle, count):
        comp = ComponentType(value="X", package="X", nozzle_type=nozzle)
        comp.placements = [object()] * count
        return comp

    def test_sums_to_n_heads(self):
        opt   = PnPOptimizer(n_heads=8)
        comps = [self._comp("#501", 50), self._comp("#504", 10)]
        assert sum(opt.optimize_head_config(comps).values()) == 8

    def test_components_without_nozzle_ignored(self):
        opt   = PnPOptimizer(n_heads=8)
        comps = [self._comp("", 10), self._comp("#501", 20)]
        h = opt.optimize_head_config(comps)
        assert "" not in h
        assert sum(h.values()) == 8


# ── component_status ──────────────────────────────────────────────────────────

class TestComponentStatus:
    def _comp(self, fw=8, fr="FRONT", nz="#501"):
        comp = ComponentType(value="V", package="P")
        comp.feeder_width = fw
        comp.feeder_row   = fr
        comp.nozzle_type  = nz
        return comp

    def test_ok_when_all_fields_set(self):
        assert component_status(self._comp()) == "OK"

    def test_incomplete_when_feeder_width_missing(self):
        assert component_status(self._comp(fw=None)) == "INCOMPLETE"

    def test_incomplete_when_feeder_row_missing(self):
        assert component_status(self._comp(fr="")) == "INCOMPLETE"

    def test_incomplete_when_nozzle_type_missing(self):
        assert component_status(self._comp(nz="")) == "INCOMPLETE"

    def test_incomplete_when_all_missing(self):
        assert component_status(ComponentType(value="V", package="P")) == "INCOMPLETE"

    def test_required_fields_are_the_three_expected(self):
        assert set(REQUIRED_COMP_FIELDS) == {'feeder_width', 'feeder_row', 'nozzle_type'}


# ── split_components_across_machines ─────────────────────────────────────────

class TestSplitAcrossMachines:
    def _comp(self, nozzle, count):
        comp = ComponentType(value="V", package="P", nozzle_type=nozzle)
        comp.placements = [object()] * count
        return comp

    def _opt(self):
        return PnPOptimizer(n_heads=8)

    def test_single_machine_returns_all(self):
        comps = [self._comp("#501", 10), self._comp("#504", 5)]
        parts = self._opt().split_components_across_machines(comps, 1)
        assert len(parts) == 1 and len(parts[0]) == 2

    def test_two_machine_total_matches(self):
        comps = [self._comp("#501", 10), self._comp("#504", 5), self._comp("#502", 8)]
        parts = self._opt().split_components_across_machines(comps, 2)
        assert sum(len(p) for p in parts) == 3

    def test_total_placements_preserved(self):
        comps = [self._comp("#501", 50), self._comp("#502", 30), self._comp("#503", 10)]
        parts = self._opt().split_components_across_machines(comps, 2)
        total = sum(sum(c.count for c in part) for part in parts)
        assert total == 90

    def test_skew_biases_small_nozzle_to_machine1(self):
        comps = [self._comp("#501", 40), self._comp("#504", 40)]
        parts = self._opt().split_components_across_machines(comps, 2, machine1_skew=100)
        m1_small = sum(c.count for c in parts[0] if c.nozzle_type in PnPOptimizer._SMALL_NOZZLES)
        assert m1_small == 40

    def test_zero_skew_approximately_balanced(self):
        comps = [self._comp("#501", 50), self._comp("#504", 50)]
        parts = self._opt().split_components_across_machines(comps, 2, machine1_skew=0)
        totals = [sum(c.count for c in part) for part in parts]
        assert abs(totals[0] - totals[1]) <= 50


# ── simultaneous_pick_groups ──────────────────────────────────────────────────

class TestSimultaneousPickGroups:
    def _pl(self, slot, nozzle):
        p = Placement(refdes="R1", value="10K", x=0, y=0, angle=0, package="R0402")
        p.feeder_slot = slot
        p.nozzle_type = nozzle
        return p

    def _opt(self):
        return PnPOptimizer(n_heads=8)

    def test_same_mod_group_can_be_simultaneous(self):
        seq = [self._pl(1, "#501"), self._pl(4, "#502"), self._pl(7, "#503")]
        groups = self._opt().simultaneous_pick_groups(seq)
        assert sorted(len(g) for g in groups) == [3]

    def test_same_nozzle_type_cannot_share_group(self):
        seq = [self._pl(1, "#501"), self._pl(4, "#501")]
        groups = self._opt().simultaneous_pick_groups(seq)
        assert all(len(g) == 1 for g in groups)

    def test_different_mod_groups_are_separate(self):
        seq = [self._pl(1, "#501"), self._pl(2, "#502")]
        groups = self._opt().simultaneous_pick_groups(seq)
        assert all(len(g) == 1 for g in groups)

    def test_max_simultaneous_respected(self):
        seq = [self._pl(1 + i * 3, f"#{501+i}") for i in range(5)]
        groups = self._opt().simultaneous_pick_groups(seq)
        assert all(len(g) <= PnPOptimizer.MAX_SIMULTANEOUS for g in groups)

    def test_all_placements_assigned(self):
        seq = [self._pl(1 + i * 3, f"#{501+i}") for i in range(6)]
        groups = self._opt().simultaneous_pick_groups(seq)
        assert sum(len(g) for g in groups) == len(seq)


# ── assign_slots ─────────────────────────────────────────────────────────────

class TestAssignSlots:
    def test_all_placements_get_slots(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert p.feeder_slot is not None

    def test_slots_in_valid_range(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert 1 <= p.feeder_slot <= 70

    def test_no_slot_double_assigned(self, fully_resolved_pipeline):
        occupied = set()
        for a in fully_resolved_pipeline["assignments"]:
            for s in range(a.slot, a.slot + a.slots_consumed):
                assert s not in occupied, f"Slot {s} double-assigned"
                occupied.add(s)

    def test_front_components_in_front_slots(self, fully_resolved_pipeline):
        for p in fully_resolved_pipeline["machine_placements"]:
            assert p.feeder_slot in PnPOptimizer.FRONT_SLOTS

    def test_high_volume_component_near_centre(self, fully_resolved_pipeline):
        assignments = fully_resolved_pipeline["assignments"]
        by_comp = {a.component.value: a.slot for a in assignments}
        dist_10k  = abs(PnPOptimizer.slot_physical_x(by_comp["10K"])  - PnPOptimizer.MACHINE_CENTER_MM)
        dist_10uF = abs(PnPOptimizer.slot_physical_x(by_comp["10uF"]) - PnPOptimizer.MACHINE_CENTER_MM)
        assert dist_10k <= dist_10uF
