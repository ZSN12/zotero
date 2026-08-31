from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from traceability.solve.tower_solver import _parse_section, _angle_steel_mesh


def test_section_specs_and_role_fallbacks():
    assert _parse_section("L140X10") == (140.0, 10.0)
    assert _parse_section("Q345L100×7") == (100.0, 7.0)
    assert _parse_section("∠75*6") == (75.0, 6.0)
    assert _parse_section("bad", "LEG") == (100.0, 7.0)
    assert _parse_section(None, "DIAG") == (75.0, 6.0)
    assert _parse_section("?", "HORIZ") == (56.0, 4.0)
    assert _parse_section("?", "CROSS") == (75.0, 6.0)


def test_angle_mesh_is_watertight_and_has_requested_bbox():
    mesh = _angle_steel_mesh("L140X10", 1000.0)
    assert mesh.is_watertight
    ext = mesh.bounds[1] - mesh.bounds[0]
    assert np.allclose(sorted(ext), sorted((140.0, 140.0, 1000.0)), atol=1e-6)
    assert len(mesh.vertices) == 12


def test_fallback_mesh_is_watertight():
    mesh = _angle_steel_mesh(None, 500.0, role="HORIZ")
    assert mesh.is_watertight
    assert np.allclose(mesh.bounds[1] - mesh.bounds[0], (56, 56, 500), atol=1e-6)
