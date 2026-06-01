"""Tests for slot geometry and placement timing."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from optimizer import PnPOptimizer, TimingConfig

OPT = PnPOptimizer   # class-level access to constants and classmethods


# ── slot_physical_x ───────────────────────────────────────────────────────────

class TestSlotPhysicalX:
    def test_front_slot_1_is_zero(self):
        assert OPT.slot_physical_x(1) == 0.0

    def test_front_slot_38_is_296mm(self):
        assert OPT.slot_physical_x(38) == 296.0

    def test_front_increases_left_to_right(self):
        xs = [OPT.slot_physical_x(s) for s in range(1, 39)]
        assert xs == sorted(xs)

    def test_front_step_is_8mm_per_slot(self):
        for slot in range(1, 38):
            assert OPT.slot_physical_x(slot + 1) - OPT.slot_physical_x(slot) == pytest.approx(8.0)

    def test_rear_slot_70_is_zero(self):
        assert OPT.slot_physical_x(70) == 0.0

    def test_rear_slot_39_is_248mm(self):
        assert OPT.slot_physical_x(39) == 248.0

    def test_rear_decreases_left_to_right(self):
        xs = [OPT.slot_physical_x(s) for s in range(39, 71)]
        assert xs == sorted(xs, reverse=True)

    def test_both_rows_share_same_centre(self):
        front_left  = OPT.slot_physical_x(19)
        front_right = OPT.slot_physical_x(20)
        rear_left   = OPT.slot_physical_x(52)
        rear_right  = OPT.slot_physical_x(51)
        assert front_left == rear_left
        assert front_right == rear_right
        assert (front_left + front_right) / 2 == pytest.approx(148.0)


# ── slot_mod_group ────────────────────────────────────────────────────────────

class TestSlotModGroup:
    def test_front_slot_1_is_group_0(self):
        assert OPT.slot_mod_group(1) == 0

    def test_front_slot_2_is_group_1(self):
        assert OPT.slot_mod_group(2) == 1

    def test_front_slot_3_is_group_2(self):
        assert OPT.slot_mod_group(3) == 2

    def test_front_pattern_repeats_every_3(self):
        for slot in range(1, 36):
            assert OPT.slot_mod_group(slot) == OPT.slot_mod_group(slot + 3)

    def test_rear_slot_70_is_group_0(self):
        assert OPT.slot_mod_group(70) == 0

    def test_rear_slot_69_is_group_1(self):
        assert OPT.slot_mod_group(69) == 1

    def test_rear_pattern_repeats_every_3(self):
        for slot in range(39, 68):
            assert OPT.slot_mod_group(slot) == OPT.slot_mod_group(slot + 3)

    def test_all_front_slots_in_valid_group(self):
        for slot in range(1, 39):
            assert OPT.slot_mod_group(slot) in (0, 1, 2)

    def test_all_rear_slots_in_valid_group(self):
        for slot in range(39, 71):
            assert OPT.slot_mod_group(slot) in (0, 1, 2)


# ── center_out_order ──────────────────────────────────────────────────────────

class TestCenterOutOrder:
    def test_front_contains_all_slots(self):
        assert sorted(OPT.center_out_order("FRONT")) == list(range(1, 39))

    def test_rear_contains_all_slots(self):
        assert sorted(OPT.center_out_order("REAR")) == list(range(39, 71))

    def test_front_first_slot_nearest_centre(self):
        assert OPT.center_out_order("FRONT")[0] in (19, 20)

    def test_rear_first_slot_nearest_centre(self):
        assert OPT.center_out_order("REAR")[0] in (51, 52)

    def test_front_distance_non_decreasing(self):
        order = OPT.center_out_order("FRONT")
        dists = [abs(OPT.slot_physical_x(s) - OPT.MACHINE_CENTER_MM) for s in order]
        for i in range(len(dists) - 1):
            assert dists[i] <= dists[i + 1] + 0.001


# ── placement_time ────────────────────────────────────────────────────────────

class TestPlacementTime:
    def test_front_centre_slot_near_minimum(self, default_timing):
        opt = PnPOptimizer(timing=default_timing)
        assert opt.placement_time(19) < 0.52

    def test_front_edge_slot_gives_maximum(self, default_timing):
        opt = PnPOptimizer(timing=default_timing)
        assert opt.placement_time(1)  == pytest.approx(default_timing.front_time_max, abs=1e-9)
        assert opt.placement_time(38) == pytest.approx(default_timing.front_time_max, abs=1e-9)

    def test_rear_far_slot_at_max(self, default_timing):
        opt = PnPOptimizer(timing=default_timing)
        assert opt.placement_time(70) == pytest.approx(default_timing.rear_time_max, abs=1e-9)

    def test_front_always_faster_than_rear(self, default_timing):
        opt = PnPOptimizer(timing=default_timing)
        for fs in range(1, 39):
            for rs in range(39, 71):
                assert opt.placement_time(fs) < opt.placement_time(rs)

    def test_custom_timing_respected(self):
        fast = TimingConfig(front_time_min=0.1, front_time_max=0.2,
                            rear_time_min=0.3,  rear_time_max=0.4)
        opt = PnPOptimizer(timing=fast)
        assert 0.1 <= opt.placement_time(19) <= 0.2
        assert 0.3 <= opt.placement_time(51) <= 0.4

    def test_time_clamped_within_bounds(self, default_timing):
        opt = PnPOptimizer(timing=default_timing)
        assert opt.placement_time(1) <= default_timing.front_time_max + 1e-9
