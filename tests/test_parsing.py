"""Tests for BOM loading, field cleaning, delimiter detection, and package rules."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import claudepnp as c

REPO_ROOT = Path(__file__).parent.parent


# ── _clean_field ──────────────────────────────────────────────────────────────

class TestCleanField:
    def test_european_decimal_comma_converted(self):
        assert c._clean_field("4,7uF") == "4.7uF"

    def test_leading_decimal_comma_converted(self):
        assert c._clean_field("0,1uF") == "0.1uF"

    def test_part_number_comma_replaced_with_underscore(self):
        assert c._clean_field("74AVC4TD245BQ,115") == "74AVC4TD245BQ_115"

    def test_semicolon_replaced_with_underscore(self):
        assert c._clean_field("some;value") == "some_value"

    def test_plain_value_unchanged(self):
        assert c._clean_field("100nF") == "100nF"

    def test_empty_string_unchanged(self):
        assert c._clean_field("") == ""

    def test_multiple_decimal_commas(self):
        # Only digit,digit sequences become decimal points.
        result = c._clean_field("1,2,3")
        assert "," not in result


# ── _detect_csv_delimiter ─────────────────────────────────────────────────────

class TestDetectCsvDelimiter:
    def test_comma_detected(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_text("a,b,c\n1,2,3\n")
        assert c._detect_csv_delimiter(f) == ","

    def test_semicolon_detected(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_text("a;b;c\n1;2;3\n")
        assert c._detect_csv_delimiter(f) == ";"

    def test_empty_file_defaults_to_comma(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_text("")
        assert c._detect_csv_delimiter(f) == ","


# ── load_bom ─────────────────────────────────────────────────────────────────

class TestLoadBom:
    def test_csv_loads_correct_count(self, simple_bom):
        placements = c.load_bom(simple_bom)
        assert len(placements) == 8

    def test_dnm_components_skipped_by_default(self, dnm_bom):
        placements = c.load_bom(dnm_bom)
        refdes = [p.refdes for p in placements]
        assert "R1" in refdes
        assert "R2" not in refdes   # value=DNP
        assert "C1" not in refdes   # value=DNM

    def test_dnm_components_included_with_flag(self, dnm_bom):
        placements = c.load_bom(dnm_bom, include_dnm=True)
        assert len(placements) == 3

    def test_space_separated_txt_loads(self):
        placements = c.load_bom(REPO_ROOT / "nr52-top.txt")
        assert len(placements) > 0

    def test_column_aliases_recognised(self, tmp_path):
        # 'Designator' / 'PosX' / 'Rotation' are aliases for refdes / X / A.
        bom = tmp_path / "alias.csv"
        bom.write_text(
            "Designator,Value,Package,PosX,PosY,Rotation\n"
            "R1,10K,R0402,1.0,2.0,90\n"
        )
        placements = c.load_bom(bom)
        assert placements[0].refdes == "R1"
        assert placements[0].x == pytest.approx(1.0)
        assert placements[0].angle == pytest.approx(90.0)

    def test_semicolon_bom_loads(self, tmp_path):
        bom = tmp_path / "semi.csv"
        bom.write_text("Designator;Value;Package;PosX;PosY;Rotation\nR1;10K;R0402;1.0;2.0;0\n")
        placements = c.load_bom(bom)
        assert len(placements) == 1
        assert placements[0].refdes == "R1"

    def test_european_decimal_in_value_cleaned(self, tmp_path):
        # European-locale BOMs use semicolons as field separators and commas
        # as decimal points — "4,7uF" appears inside a semicolon-delimited file.
        bom = tmp_path / "eu.csv"
        bom.write_text("Designator;Value;Package;PosX;PosY;Rotation\nC1;4,7uF;C0402;1.0;2.0;0\n")
        placements = c.load_bom(bom)
        assert placements[0].value == "4.7uF"


# ── _match_rule ───────────────────────────────────────────────────────────────

class TestMatchRule:
    def _rule(self, pattern, match_type):
        return c.PackageRule(
            pattern=pattern, match_type=match_type,
            feeder_width=8, feeder_row="FRONT", nozzle_type="#501", notes=""
        )

    def test_exact_match(self):
        assert c._match_rule("R0402", self._rule("R0402", "exact"))

    def test_exact_no_partial(self):
        assert not c._match_rule("R0402X", self._rule("R0402", "exact"))

    def test_exact_case_insensitive(self):
        assert c._match_rule("r0402", self._rule("R0402", "exact"))

    def test_prefix_match(self):
        assert c._match_rule("C0402X55", self._rule("C0402", "prefix"))

    def test_prefix_no_wrong_start(self):
        assert not c._match_rule("XC0402", self._rule("C0402", "prefix"))

    def test_contains_match(self):
        assert c._match_rule("BGA_64", self._rule("BGA", "contains"))

    def test_contains_no_wrong_string(self):
        assert not c._match_rule("ABCD", self._rule("BGA", "contains"))

    def test_regex_match(self):
        assert c._match_rule("QFN50P200X200X100-8N", self._rule(r"QFN\d+P\d+", "regex"))

    def test_regex_no_match(self):
        assert not c._match_rule("SOIC8", self._rule(r"QFN\d+", "regex"))


# ── load_package_rules ────────────────────────────────────────────────────────

class TestLoadPackageRules:
    def test_loads_without_error(self):
        rules = c.load_package_rules(REPO_ROOT / "package_rules.csv")
        assert len(rules) > 0

    def test_comment_lines_skipped(self):
        rules = c.load_package_rules(REPO_ROOT / "package_rules.csv")
        for rule in rules:
            assert not rule.pattern.startswith("#")

    def test_known_rule_present(self):
        rules = c.load_package_rules(REPO_ROOT / "package_rules.csv")
        r0402_rules = [r for r in rules if r.pattern == "R0402"]
        assert len(r0402_rules) > 0
        r = r0402_rules[0]
        assert r.nozzle_type == "#501"
        assert r.feeder_row == "FRONT"

    def test_juki_nozzle_format(self):
        rules = c.load_package_rules(REPO_ROOT / "package_rules.csv")
        for rule in rules:
            if rule.nozzle_type:
                assert rule.nozzle_type.startswith("#"), \
                    f"Unexpected nozzle format: {rule.nozzle_type!r}"


# ── load_timing_config ────────────────────────────────────────────────────────

class TestLoadTimingConfig:
    def test_loads_from_file(self):
        cfg = c.load_timing_config(REPO_ROOT / "timing_config.csv")
        assert cfg.front_time_min == pytest.approx(0.5)
        assert cfg.front_time_max == pytest.approx(0.7)
        assert cfg.rear_time_min  == pytest.approx(1.0)
        assert cfg.rear_time_max  == pytest.approx(1.2)

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = c.load_timing_config(tmp_path / "nonexistent.csv")
        assert cfg.front_time_min == pytest.approx(0.5)
        assert cfg.rear_time_max  == pytest.approx(1.2)

    def test_partial_override(self, tmp_path):
        f = tmp_path / "timing.csv"
        f.write_text("parameter,value,description\nfront_time_max,0.9,test\n")
        cfg = c.load_timing_config(f)
        assert cfg.front_time_max == pytest.approx(0.9)
        assert cfg.front_time_min == pytest.approx(0.5)   # default unchanged

    def test_unknown_parameter_ignored(self, tmp_path):
        f = tmp_path / "timing.csv"
        f.write_text("parameter,value,description\nrandom_key,99,ignored\n")
        cfg = c.load_timing_config(f)
        assert cfg.front_time_min == pytest.approx(0.5)   # defaults intact
