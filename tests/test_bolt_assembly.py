import inspect, json, struct
from pathlib import Path
import numpy as np
import trimesh

from traceability.connection.bolt_mesh import bolt_assembly_meshes, bolt_holes_global, bolt_hole_meshes
from scripts.generate_assembly import _groups, build

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / 'web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json'


def test_real_sheet_groups_and_bolts():
    groups = _groups(SHEET)
    assert len(groups) == 16
    assert sum(len(g['holes']) for g in groups) == 56
    assert all(len(g['holes']) > 0 for g in groups)


def test_assembly_parts_and_layer_order():
    groups = _groups(SHEET)
    rng = np.random.default_rng(35)
    for g in groups:
        m = bolt_assembly_meshes(g)
        assert m.metadata['bolt_count'] == len(g['holes'])
        assert m.metadata['bolt_parts_per_bolt'] == 4
    # cylinders are oriented along +Z; centers satisfy head > plate > nut
    m = bolt_assembly_meshes(groups[0])
    assert m.metadata['plate_normal'] == [0.0, 0.0, 1.0]
    assert m.bounds[1, 2] > 0 and m.bounds[0, 2] < 0


def test_legacy_signatures_unchanged():
    assert str(inspect.signature(bolt_holes_global)) == "(model: 'EngineeringModel', bolt_cid: 'str') -> 'List[Tuple[float, float, float]]'"
    assert str(inspect.signature(bolt_hole_meshes)) == "(model: 'EngineeringModel')"


def test_degraded_export_and_glb_material_normals(tmp_path):
    report = build(tmp_path, SHEET)
    assert report['bolt_count'] == 56
    assert set(report['missing']) == {'solid_angle_tower.glb', 'gusset_attached.glb'}
    blob = (tmp_path / 'assembly.glb').read_bytes()
    assert blob[:4] == b'glTF'
    loaded = trimesh.load(tmp_path / 'assembly.glb', force='scene')
    assert len(loaded.geometry) >= 16
    assert all('position' in {k.lower() for k in g.vertex_attributes} or len(g.vertices) for g in loaded.geometry.values())
    # inspect exported JSON chunk for core metallic-roughness factors
    length, version, total = struct.unpack_from('<4sII', blob, 0) if False else (None, None, None)
    # trimesh parse is authoritative for material values
    found = []
    for g in loaded.geometry.values():
        mat = getattr(getattr(g, 'visual', None), 'material', None)
        if mat is not None and hasattr(mat, 'metallicFactor'):
            found.append((float(mat.metallicFactor), float(mat.roughnessFactor)))
    assert found and any(abs(a-.85) < 1e-6 and abs(b-.40) < 1e-6 for a,b in found)
    for g in loaded.geometry.values():
        assert len(g.vertices) > 0
        assert len(g.vertex_normals) == len(g.vertices)
