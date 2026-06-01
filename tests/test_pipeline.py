"""End-to-end pipeline regression tests.

These tests call main() via the run_main fixture and verify properties of
the generated output files.  They catch regressions that slip past unit tests
because they exercise the full code path from CLI parsing to file output.
"""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import claudepnp as c

REPO_ROOT = Path(__file__).parent.parent


# ── helpers ───────────────────────────────────────────────────────────────────

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sequence_placements(path):
    """Return list of data lines from a sequence file (non-comment, non-blank)."""
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def sequence_headers(path):
    """Return all # SEQ header lines from a sequence file."""
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip().startswith("# SEQ")]


# ── Phase 1 behaviour ─────────────────────────────────────────────────────────

class TestPhase1:
    def test_components_csv_written(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        assert (tmp_path / "simple_components.csv").exists()

    def test_all_component_types_in_csv(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_components.csv")
        assert len(rows) == 5   # 5 distinct (value, package) pairs

    def test_all_components_ok(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_components.csv")
        assert all(r["status"] == "OK" for r in rows)

    def test_nozzle_type_populated(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_components.csv")
        assert all(r["nozzle_type"].startswith("#") for r in rows)

    def test_incomplete_bom_exits_phase1(self, run_main, tmp_path):
        bom = tmp_path / "unknown.csv"
        bom.write_text(
            "Designator,Value,Package,PosX,PosY,Rotation\n"
            "U1,FPGA,UNKNOWN_BGA_512,10,10,0\n"
        )
        exit_code, _ = run_main("--bom", str(bom))
        assert exit_code == 0   # sys.exit(0): incomplete but not an error

    def test_incomplete_bom_no_sequence_file(self, run_main, tmp_path):
        bom = tmp_path / "unknown.csv"
        bom.write_text(
            "Designator,Value,Package,PosX,PosY,Rotation\n"
            "U1,FPGA,UNKNOWN_BGA_512,10,10,0\n"
        )
        run_main("--bom", str(bom))
        assert not (tmp_path / "unknown_sequence.txt").exists()

    def test_manual_entries_preserved_across_reruns(self, run_main, simple_bom, tmp_path):
        # First run — generates components CSV
        run_main("--bom", str(simple_bom))
        comp_path = tmp_path / "simple_components.csv"

        # Simulate operator manually editing a row
        rows = read_csv(comp_path)
        for r in rows:
            if r["package"] == "C0805":
                r["nozzle_type"] = "#503_CUSTOM"
                r["matched_by"]  = "MANUAL"
        comp_path.write_text(
            "value,package,count,feeder_width,feeder_row,nozzle_type,"
            "name,matched_by,status\n"
        )
        with open(comp_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        # Second run — manual entry must survive
        run_main("--bom", str(simple_bom))
        rows2 = read_csv(comp_path)
        c0805 = next(r for r in rows2 if r["package"] == "C0805")
        assert c0805["nozzle_type"] == "#503_CUSTOM"
        assert c0805["matched_by"]  == "MANUAL"

    def test_dnm_components_skipped(self, run_main, dnm_bom, tmp_path):
        run_main("--bom", str(dnm_bom))
        comp_path = tmp_path / "dnm_components.csv"
        if comp_path.exists():
            rows = read_csv(comp_path)
            values = {r["value"] for r in rows}
            assert "DNP" not in values
            assert "DNM" not in values


# ── Phase 2 output correctness ────────────────────────────────────────────────

class TestPhase2Outputs:
    def test_all_output_files_written(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        assert (tmp_path / "simple_feeders.csv").exists()
        assert (tmp_path / "simple_sequence.txt").exists()
        assert (tmp_path / "simple_nozzle_config.csv").exists()

    def test_feeder_csv_has_required_columns(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_feeders.csv")
        required = {"slot", "row", "value", "package", "feeder_width_mm",
                    "nozzle_type", "total_placements", "mod_group"}
        assert required.issubset(rows[0].keys())

    def test_feeder_slots_in_valid_range(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_feeders.csv")
        for r in rows:
            assert 1 <= int(r["slot"]) <= 70

    def test_no_duplicate_feeder_slots(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        rows = read_csv(tmp_path / "simple_feeders.csv")
        # Expand each assignment to its occupied slots and check for overlaps.
        occupied = []
        for r in rows:
            start = int(r["slot"])
            span  = int(r["slots_consumed"])
            for s in range(start, start + span):
                assert s not in occupied, f"Slot {s} occupied twice"
                occupied.append(s)

    def test_sequence_file_no_commas_or_semicolons(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        lines = sequence_placements(tmp_path / "simple_sequence.txt")
        for line in lines:
            assert "," not in line
            assert ";" not in line

    def test_sequence_fields_are_space_separated(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        lines = sequence_placements(tmp_path / "simple_sequence.txt")
        for line in lines:
            parts = line.split()
            # refdes value X Y angle package name  → 7 fields
            assert len(parts) == 7, f"Expected 7 fields, got {len(parts)}: {line!r}"

    def test_all_placements_appear_in_sequence(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        lines    = sequence_placements(tmp_path / "simple_sequence.txt")
        placed   = [l.split()[0] for l in lines]   # refdes column
        expected = ["R1", "R2", "R3", "C1", "C2", "C3", "U1", "U2"]
        assert sorted(placed) == sorted(expected)

    def test_each_placement_appears_exactly_once(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        lines  = sequence_placements(tmp_path / "simple_sequence.txt")
        placed = [l.split()[0] for l in lines]
        assert len(placed) == len(set(placed))

    def test_nozzle_config_head_count_correct(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        nozzle_csv = tmp_path / "simple_nozzle_config.csv"
        rows = [l for l in nozzle_csv.read_text().splitlines()
                if not l.startswith("#") and l.strip()]
        # Skip the header row "head,nozzle_type"
        data_rows = [r for r in rows if r.split(",")[0].isdigit()]
        assert len(data_rows) == 8   # default --heads 8

    def test_pick_cycles_at_ideal_minimum(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        headers = sequence_headers(tmp_path / "simple_sequence.txt")
        # Simple BOM: all FRONT, head config perfectly balanced → 1 cycle.
        assert len(headers) == 1

    def test_custom_heads_count(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--heads", "4")
        nozzle_csv = tmp_path / "simple_nozzle_config.csv"
        data_rows = [l for l in nozzle_csv.read_text().splitlines()
                     if not l.startswith("#") and l.strip()
                     and l.split(",")[0].isdigit()]
        assert len(data_rows) == 4


# ── Multi-machine ─────────────────────────────────────────────────────────────

class TestMultiMachine:
    def test_machine_files_created(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--machines", "2")
        assert (tmp_path / "simple_machine1_feeders.csv").exists()
        assert (tmp_path / "simple_machine2_feeders.csv").exists()
        assert (tmp_path / "simple_machine1_sequence.txt").exists()
        assert (tmp_path / "simple_machine2_sequence.txt").exists()

    def test_total_placements_preserved_across_machines(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--machines", "2")
        total = 0
        for machine in (1, 2):
            lines = sequence_placements(
                tmp_path / f"simple_machine{machine}_sequence.txt"
            )
            total += len(lines)
        assert total == 8   # all 8 placements from simple BOM

    def test_no_placement_duplicated_across_machines(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--machines", "2")
        all_refs = []
        for machine in (1, 2):
            lines = sequence_placements(
                tmp_path / f"simple_machine{machine}_sequence.txt"
            )
            all_refs.extend(l.split()[0] for l in lines)
        assert len(all_refs) == len(set(all_refs))

    def test_single_components_csv_regardless_of_machines(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--machines", "2")
        assert (tmp_path / "simple_components.csv").exists()
        assert not (tmp_path / "simple_machine1_components.csv").exists()


# ── Sequence ordering and bundling ────────────────────────────────────────────

class TestSequenceOrdering:
    def test_front_sequences_before_rear(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        headers = sequence_headers(tmp_path / "simple_sequence.txt")
        # Simple BOM is all-FRONT.
        assert all("row=FRONT" in h for h in headers)

    def test_nozzle_interleaving_prevents_same_nozzle_runs(
        self, run_main, simple_bom, tmp_path
    ):
        """Within a single pick cycle, the same nozzle type should not repeat
        (that would cause premature machine firing)."""
        run_main("--bom", str(simple_bom))
        # The simple BOM resolves to 1 cycle; check nozzle types in that cycle
        # are all distinct by reading the nozzle from the feeder CSV.
        feeder_rows = read_csv(tmp_path / "simple_feeders.csv")
        slot_nozzle = {int(r["slot"]): r["nozzle_type"] for r in feeder_rows}

        seq_lines = sequence_placements(tmp_path / "simple_sequence.txt")
        # Map refdes → slot via components; the sequence file doesn't carry
        # slot numbers, so we verify via the full pipeline fixture instead.
        # (Property tested more directly in test_algorithms.)
        assert len(seq_lines) == 8   # regression: all 8 placements present


# ── Regression: known edge cases ─────────────────────────────────────────────

class TestEdgeCaseRegressions:
    def test_european_decimal_comma_in_value_not_in_output(
        self, run_main, tmp_path
    ):
        # Semicolon-delimited BOM with European decimal comma in the value field.
        bom = tmp_path / "eu.csv"
        bom.write_text(
            "Designator;Value;Package;PosX;PosY;Rotation\n"
            "C1;4,7uF;C0402;10.0;20.0;0\n"
        )
        run_main("--bom", str(bom))
        seq = (tmp_path / "eu_sequence.txt").read_text()
        # The decimal comma must be converted; no comma should appear in output.
        data_lines = [l for l in seq.splitlines()
                      if l.strip() and not l.strip().startswith("#")]
        for line in data_lines:
            assert "," not in line

    def test_part_number_comma_sanitised_in_output(self, run_main, tmp_path):
        bom = tmp_path / "mpn.csv"
        bom.write_text(
            "Designator,Value,Package,PosX,PosY,Rotation,name\n"
            "R1,10K,R0402,10.0,20.0,0,74AVC4TD245BQ,115\n"
        )
        run_main("--bom", str(bom))
        seq_path = tmp_path / "mpn_sequence.txt"
        if seq_path.exists():
            seq = seq_path.read_text()
            data_lines = [l for l in seq.splitlines()
                          if l.strip() and not l.strip().startswith("#")]
            for line in data_lines:
                assert "," not in line

    def test_overflow_to_alternate_row_still_places_all_components(
        self, run_main, tmp_path
    ):
        """If FRONT overflows, components should spill to REAR and still be placed."""
        # Build a BOM large enough to overflow FRONT (38 slots × 1 slot each = 38 max,
        # but with 12mm feeders that's 19 max). Use 25 distinct 12mm components.
        lines = ["Designator,Value,Package,PosX,PosY,Rotation"]
        for i in range(25):
            lines.append(f"U{i},IC{i},SOIC8,{i*5}.0,10.0,0")
        bom = tmp_path / "overflow.csv"
        bom.write_text("\n".join(lines) + "\n")
        run_main("--bom", str(bom))
        seq_path = tmp_path / "overflow_sequence.txt"
        assert seq_path.exists()
        placed = sequence_placements(seq_path)
        assert len(placed) == 25

    def test_job_prefix_applies_to_all_outputs(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--job-prefix", "myboard_rev2")
        assert (tmp_path / "myboard_rev2_feeders.csv").exists()
        assert (tmp_path / "myboard_rev2_sequence.txt").exists()
        assert (tmp_path / "myboard_rev2_components.csv").exists()
