from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from traceability.solve.tower_geometry import angle_steel_orientation


def test_corner_bisector_points_radially_outward():
    for x, y in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
        pa = (x * 1000, y * 1000, 0)
        pb = (x * 1000, y * 1000, 1000)
        m = angle_steel_orientation(pa, pb, "LEG")
        # The outer corner is local (0, 0); relative to section centroid its
        # bisector is negative in both local axes.
        corner = (m[:3, :3] @ np.array([-1, -1, 0.0])) / np.sqrt(2)
        radial = np.array([x, y, 0.0]) / np.sqrt(2)
        assert np.degrees(np.arccos(np.clip(corner @ radial, -1, 1))) < 1e-5


def test_orientation_is_deterministic_and_axis_aligned():
    m1 = angle_steel_orientation((100, 0, 0), (-100, 0, 1000), "DIAG")
    m2 = angle_steel_orientation((100, 0, 0), (-100, 0, 1000), "DIAG")
    assert np.array_equal(m1, m2)
    assert np.allclose(m1[:3, 2], np.array([-200, 0, 1000]) / np.linalg.norm([200, 0, 1000]))
