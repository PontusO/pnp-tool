"""Shared fixtures for the ClaudePnP test suite."""
import sys
from pathlib import Path

import pytest

# Make the repo root importable
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claudepnp as c

# ---------------------------------------------------------------------------
# Minimal synthetic BOM — all packages resolve via package_rules.csv so the
# tool runs to completion without any manual editing step.
# ---------------------------------------------------------------------------
SIMPLE_BOM_CSV = """\
Designator,Value,Package,PosX,PosY,Rotation
R1,10K,R0402,10.0,20.0,0
R2,10K,R0402,15.0,20.0,90
R3,4K7,R0402,20.0,20.0,0
C1,100nF,C0402,10.0,30.0,0
C2,100nF,C0402,15.0,30.0,0
C3,10uF,C0805,10.0,40.0,0
U1,LDO_3V3,SOT23-5,50.0,50.0,0
U2,LDO_3V3,SOT23-5,55.0,50.0,270
"""

# A BOM that mixes FRONT and REAR components to exercise row-separation and
# sparse-REAR bundling logic.
MIXED_BOM_CSV = """\
Designator,Value,Package,PosX,PosY,Rotation
R1,10K,R0402,10.0,20.0,0
R2,10K,R0402,15.0,20.0,0
C1,100nF,C0402,10.0,30.0,0
C2,100nF,C0402,15.0,30.0,0
C3,100nF,C0402,20.0,30.0,0
U1,LDO_3V3,SOT23-5,50.0,50.0,0
"""

# A BOM that has a DNM component to exercise the skip logic.
DNM_BOM_CSV = """\
Designator,Value,Package,PosX,PosY,Rotation
R1,10K,R0402,10.0,20.0,0
R2,DNP,R0402,15.0,20.0,0
C1,DNM,C0402,10.0,30.0,0
"""


@pytest.fixture
def feeder_specs():
    return c.load_feeder_table(REPO_ROOT / "feeder_table.csv")


@pytest.fixture
def pkg_rules():
    return c.load_package_rules(REPO_ROOT / "package_rules.csv")


@pytest.fixture
def timing():
    return c.load_timing_config(REPO_ROOT / "timing_config.csv")


@pytest.fixture
def default_timing():
    return c.TimingConfig()


@pytest.fixture
def simple_bom(tmp_path):
    p = tmp_path / "simple.csv"
    p.write_text(SIMPLE_BOM_CSV)
    return p


@pytest.fixture
def mixed_bom(tmp_path):
    p = tmp_path / "mixed.csv"
    p.write_text(MIXED_BOM_CSV)
    return p


@pytest.fixture
def dnm_bom(tmp_path):
    p = tmp_path / "dnm.csv"
    p.write_text(DNM_BOM_CSV)
    return p


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Return a helper that runs claudepnp.main() with given CLI args.

    Output is directed to tmp_path.  Returns (exit_code, tmp_path) so tests
    can inspect generated files.  exit_code is None if main() returned
    normally; an int if it called sys.exit().
    """
    def _run(*extra_args):
        args = [
            "claudepnp.py",
            "--feeder-table", str(REPO_ROOT / "feeder_table.csv"),
            "--package-rules", str(REPO_ROOT / "package_rules.csv"),
            "--timing-config",  str(REPO_ROOT / "timing_config.csv"),
            "--output-dir", str(tmp_path),
            *extra_args,
        ]
        monkeypatch.setattr(sys, "argv", args)
        try:
            c.main()
            return None, tmp_path
        except SystemExit as e:
            return e.code, tmp_path

    return _run


@pytest.fixture
def fully_resolved_pipeline(tmp_path, feeder_specs, pkg_rules):
    """Run Phase 1 + Phase 2 on SIMPLE_BOM_CSV and return the live objects.

    Returns a dict with keys: placements, components, assignments,
    head_config, sequences.
    """
    bom_path = tmp_path / "simple.csv"
    bom_path.write_text(SIMPLE_BOM_CSV)
    comp_path = tmp_path / "simple_components.csv"

    # Phase 1
    placements = c.load_bom(bom_path)
    components = c.group_components(placements)
    c.apply_package_rules(components, pkg_rules)
    c.write_components_csv(components, comp_path)

    # Phase 2
    configs = c.load_components_csv(comp_path)
    c.merge_component_configs(placements, configs)
    components = c.group_components(placements)
    assignments = c.assign_slots(components, feeder_specs, False, 20)
    head_config = c.optimize_head_config(components, n_heads=8)
    machine_placements = [p for comp in components for p in comp.placements]
    sequences = c.build_sequences(machine_placements, head_config)

    return {
        "placements": placements,
        "components": components,
        "assignments": assignments,
        "head_config": head_config,
        "sequences": sequences,
        "machine_placements": machine_placements,
    }
