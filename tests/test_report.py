"""Tests for PDF report generation."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Skip the whole module cleanly if reportlab isn't installed.
reportlab = pytest.importorskip("reportlab")

REPO_ROOT = Path(__file__).parent.parent


def _pdf_page_count(path: Path) -> int:
    """Count pages by scanning for /Type /Page tokens (no extra deps)."""
    data = path.read_bytes()
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages")


class TestReportGeneration:
    def test_report_file_created(self, run_main, simple_bom, tmp_path):
        exit_code, _ = run_main("--bom", str(simple_bom), "--report")
        assert exit_code is None
        assert (tmp_path / "simple_report.pdf").exists()

    def test_report_is_valid_pdf(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--report")
        pdf = tmp_path / "simple_report.pdf"
        assert pdf.read_bytes()[:5] == b"%PDF-"

    def test_report_has_pages(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--report")
        assert _pdf_page_count(tmp_path / "simple_report.pdf") >= 1

    def test_no_report_without_flag(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom))
        assert not (tmp_path / "simple_report.pdf").exists()

    def test_multi_machine_report_created(self, run_main, simple_bom, tmp_path):
        run_main("--bom", str(simple_bom), "--machines", "2", "--report")
        assert (tmp_path / "simple_report.pdf").exists()

    def test_report_not_created_when_phase1_incomplete(self, run_main, tmp_path):
        bom = tmp_path / "unknown.csv"
        bom.write_text(
            "Designator,Value,Package,PosX,PosY,Rotation\n"
            "U1,FPGA,UNKNOWN_BGA_512,10,10,0\n"
        )
        run_main("--bom", str(bom), "--report")
        # Phase 1 exits before any PDF is generated.
        assert not (tmp_path / "unknown_report.pdf").exists()


class TestReportModule:
    """Direct tests of report.write_pdf_report with synthetic inputs."""

    def test_write_pdf_report_minimal(self, tmp_path):
        import report as report_mod

        # Build the minimal CSV inputs the report reads.
        feeders = tmp_path / "f.csv"
        feeders.write_text(
            "slot,slots_consumed,physical_x_mm,row,reel_index,value,package,"
            "feeder_width_mm,nozzle_type,name_mpn,total_placements,mod_group\n"
            "19,1,144.0,FRONT,0,10K,R0402,8,#501,,2,0\n"
        )
        seq = tmp_path / "s.txt"
        seq.write_text(
            "# ClaudePnP job file\n"
            "# SEQ 0001  row=FRONT  placements=2  pick_descends=1\n"
            "R1 10K 1.0 2.0 0.00 R0402 N/A\n"
        )
        nozzle = tmp_path / "n.csv"
        nozzle.write_text(
            "# Optimised nozzle head configuration\n"
            "head,nozzle_type\n1,#501\n"
        )
        components = tmp_path / "c.csv"
        components.write_text(
            "value,package,count,feeder_width,feeder_row,nozzle_type,name,"
            "matched_by,status\n"
            "10K,R0402,2,8,FRONT,#501,,prefix:R0402,OK\n"
        )

        out = tmp_path / "report.pdf"
        report_mod.write_pdf_report(
            output_path=out,
            job_name="testjob",
            machines=[{
                "label":        "",
                "stats":        {"cycles": 1, "ideal_min": 1, "utilisation_pct": 25.0,
                                 "total_descends": 1, "max_simultaneous": 2,
                                 "total_placements": 2},
                "board_time":   (1.0, 1.0, 1.4),
                "head_config":  {"#501": 1},
                "feeders_csv":  feeders,
                "sequence_txt": seq,
                "nozzle_csv":   nozzle,
            }],
            components_csv=components,
            n_heads=8,
        )
        assert out.exists()
        assert out.read_bytes()[:5] == b"%PDF-"
