"""Shared fixtures for the ClaudePnP test suite."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# pnp-plan.py has a hyphen so it can't be imported with a plain import statement.
# Load it via importlib and register it in sys.modules so test files can still
# do `import claudepnp as cli` without change.
_spec = importlib.util.spec_from_file_location("claudepnp", REPO_ROOT / "pnp-plan.py")
cli = importlib.util.module_from_spec(_spec)
sys.modules["claudepnp"] = cli
_spec.loader.exec_module(cli)
from optimizer import (
    PnPOptimizer, TimingConfig, Placement, ComponentType,
    group_components, component_status,
)

# ---------------------------------------------------------------------------
# Synthetic BOMs
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

MIXED_BOM_CSV = """\
Designator,Value,Package,PosX,PosY,Rotation
R1,10K,R0402,10.0,20.0,0
R2,10K,R0402,15.0,20.0,0
C1,100nF,C0402,10.0,30.0,0
C2,100nF,C0402,15.0,30.0,0
C3,100nF,C0402,20.0,30.0,0
U1,LDO_3V3,SOT23-5,50.0,50.0,0
"""

DNM_BOM_CSV = """\
Designator,Value,Package,PosX,PosY,Rotation
R1,10K,R0402,10.0,20.0,0
R2,DNP,R0402,15.0,20.0,0
C1,DNM,C0402,10.0,30.0,0
"""


@pytest.fixture
def feeder_specs():
    return cli.load_feeder_table(REPO_ROOT / "feeder_table.csv")


@pytest.fixture
def pkg_rules():
    return cli.load_package_rules(REPO_ROOT / "package_rules.csv")


@pytest.fixture
def timing():
    return cli.load_timing_config(REPO_ROOT / "timing_config.csv")


@pytest.fixture
def default_timing():
    return TimingConfig()


@pytest.fixture
def opt(timing):
    return PnPOptimizer(n_heads=8, timing=timing)


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
    """Run claudepnp.main() with given args, output to tmp_path."""
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
            cli.main()
            return None, tmp_path
        except SystemExit as e:
            return e.code, tmp_path
    return _run


@pytest.fixture
def fully_resolved_pipeline(tmp_path, feeder_specs, pkg_rules, opt):
    """Full Phase 1 + Phase 2 pipeline on SIMPLE_BOM_CSV."""
    bom_path  = tmp_path / "simple.csv"
    comp_path = tmp_path / "simple_components.csv"
    bom_path.write_text(SIMPLE_BOM_CSV)

    placements = cli.load_bom(bom_path)
    components = group_components(placements)
    opt.apply_package_rules(components, pkg_rules)
    cli.write_components_csv(components, comp_path)

    configs = cli.load_components_csv(comp_path)
    cli.merge_component_configs(placements, configs)
    components = group_components(placements)
    assignments = opt.assign_slots(components, feeder_specs, False, 20)
    head_config = opt.optimize_head_config(components)
    machine_placements = [p for c in components for p in c.placements]
    sequences = opt.build_sequences(machine_placements, head_config)

    return {
        "placements":          placements,
        "components":          components,
        "assignments":         assignments,
        "head_config":         head_config,
        "sequences":           sequences,
        "machine_placements":  machine_placements,
        "opt":                 opt,
    }
