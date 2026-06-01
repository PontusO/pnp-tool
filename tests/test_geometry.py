"""Tests for slot geometry helper functions."""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import claudepnp as c


# ── _slot_physical_x ──────────────────────────────────────────────────────────

class TestSlotPhysicalX:
    def test_front_slot_1_is_zero(self):
        assert c._slot_physical_x(1) == 0.0

    def test_front_slot_38_is_296mm(self):
        assert c._slot_physical_x(38) == 296.0

    def test_front_increases_left_to_right(self):
        xs = [c._slot_physical_x(s) for s in range(1, 39)]
        assert xs == sorted(xs)

    def test_front_step_is_8mm_per_slot(self):
        for slot in range(1, 38):
            assert c._slot_physical_x(slot + 1) - c._slot_physical_x(slot) == pytest.approx(8.0)

    def test_rear_slot_70_is_zero(self):
        assert c._slot_physical_x(70) == 0.0

    def test_rear_slot_39_is_248mm(self):
        assert c._slot_physical_x(39) == 248.0

    def test_rear_decreases_left_to_right(self):
        # Rear slots are numbered right-to-left: slot 39 is rightmost (248 mm),
        # slot 70 is leftmost (0 mm).
        xs = [c._slot_physical_x(s) for s in range(39, 71)]
        assert xs == sorted(xs, reverse=True)

    def test_both_rows_share_same_centre(self):
        # Centre for FRONT is between slots 19 and 20; centre for REAR is
        # between slots 51 and 52. Both should be at 148 mm.
        front_left  = c._slot_physical_x(19)   # 144 mm
        front_right = c._slot_physical_x(20)   # 152 mm
        rear_left   = c._slot_physical_x(52)   # 144 mm
        rear_right  = c._slot_physical_x(51)   # 152 mm
        assert front_left == rear_left
        assert front_right == rear_right
        assert (front_left + front_right) / 2 == pytest.approx(148.0)


# ── _slot_mod_group ───────────────────────────────────────────────────────────

class TestSlotModGroup:
    def test_front_slot_1_is_group_0(self):
        assert c._slot_mod_group(1) == 0

    def test_front_slot_2_is_group_1(self):
        assert c._slot_mod_group(2) == 1

    def test_front_slot_3_is_group_2(self):
        assert c._slot_mod_group(3) == 2

    def test_front_pattern_repeats_every_3(self):
        for slot in range(1, 36):
            assert c._slot_mod_group(slot) == c._slot_mod_group(slot + 3)

    def test_rear_slot_70_is_group_0(self):
        assert c._slot_mod_group(70) == 0

    def test_rear_slot_69_is_group_1(self):
        assert c._slot_mod_group(69) == 1

    def test_rear_pattern_repeats_every_3(self):
        for slot in range(39, 68):
            assert c._slot_mod_group(slot) == c._slot_mod_group(slot + 3)

    def test_all_front_slots_in_valid_group(self):
        for slot in range(1, 39):
            assert c._slot_mod_group(slot) in (0, 1, 2)

    def test_all_rear_slots_in_valid_group(self):
        for slot in range(39, 71):
            assert c._slot_mod_group(slot) in (0, 1, 2)


# ── _center_out_order ─────────────────────────────────────────────────────────

class TestCenterOutOrder:
    def test_front_contains_all_slots(self):
        order = c._center_out_order("FRONT")
        assert sorted(order) == list(range(1, 39))

    def test_rear_contains_all_slots(self):
        order = c._center_out_order("REAR")
        assert sorted(order) == list(range(39, 71))

    def test_front_first_slot_nearest_centre(self):
        order = c._center_out_order("FRONT")
        first = order[0]
        # Slot 19 is at 144 mm, slot 20 at 152 mm — both 4 mm from 148 mm.
        assert first in (19, 20)

    def test_rear_first_slot_nearest_centre(self):
        order = c._center_out_order("REAR")
        first = order[0]
        # Slots 51 and 52 are closest to centre on the rear rack.
        assert first in (51, 52)

    def test_front_distance_non_decreasing(self):
        order = c._center_out_order("FRONT")
        distances = [abs(c._slot_physical_x(s) - c.MACHINE_CENTER_MM) for s in order]
        # Each step should be >= previous (allowed to stay equal for paired slots).
        for i in range(len(distances) - 1):
            assert distances[i] <= distances[i + 1] + 0.001


# ── _placement_time ───────────────────────────────────────────────────────────

class TestPlacementTime:
    def test_front_centre_slot_gives_minimum(self, default_timing):
        # Slot 19 physical_x = 144 mm, distance from 148 = 4 mm ≈ nearly min.
        # Slot 20 physical_x = 152 mm, distance = 4 mm as well.
        t19 = c._placement_time(19, default_timing)
        t20 = c._placement_time(20, default_timing)
        assert t19 == pytest.approx(t20)
        assert t19 < 0.52   # close to the 0.5 s minimum

    def test_front_edge_slot_gives_maximum(self, default_timing):
        t1  = c._placement_time(1,  default_timing)
        t38 = c._placement_time(38, default_timing)
        assert t1  == pytest.approx(default_timing.front_time_max, abs=1e-9)
        assert t38 == pytest.approx(default_timing.front_time_max, abs=1e-9)

    def test_rear_near_centre_below_max(self, default_timing):
        t = c._placement_time(51, default_timing)
        assert default_timing.rear_time_min <= t <= default_timing.rear_time_max

    def test_rear_far_slot_at_max(self, default_timing):
        # Slot 70 is at 0 mm — furthest from centre on either rack.
        t = c._placement_time(70, default_timing)
        assert t == pytest.approx(default_timing.rear_time_max, abs=1e-9)

    def test_front_always_faster_than_rear(self, default_timing):
        for front_slot in range(1, 39):
            for rear_slot in range(39, 71):
                assert c._placement_time(front_slot, default_timing) < \
                       c._placement_time(rear_slot,  default_timing)

    def test_custom_timing_respected(self):
        fast = c.TimingConfig(front_time_min=0.1, front_time_max=0.2,
                              rear_time_min=0.3,  rear_time_max=0.4)
        t_front = c._placement_time(19, fast)
        t_rear  = c._placement_time(51, fast)
        assert 0.1 <= t_front <= 0.2
        assert 0.3 <= t_rear  <= 0.4

    def test_time_clamped_at_1(self, default_timing):
        # Even a hypothetical slot at the very edge should not exceed max.
        t = c._placement_time(1, default_timing)
        assert t <= default_timing.front_time_max + 1e-9
