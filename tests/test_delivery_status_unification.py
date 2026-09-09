# -*- coding: utf-8 -*-
"""P0.1 交付状态统一回归测试。

锁定两件事：
1. r_bom_length_match 按物理杆聚合——四面展开的镜像实例（F/B/L/R 同 stem）
   只算 1 根，不再 4 倍重复计数（2026-08-31 前旧口径 319 超差里 4/5 是
   镜像重复）。
2. deliver 状态链 stage_status + failure_reasons/review_reasons 结构化——
   「门禁通过但 status=failed」必须有可解释的 code/stage/message。
"""

import unittest
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _make_model_with_bars(bars_spec, dims=None, nodes_spec=None, props_extra=None):
    """构造带 BOM 长度的模型。bars_spec: [(cid, bar_id, length, face)]

    nodes_spec: {node_id: (x,y,z)} —— tower_node 坐标（链 span 口径用）。
    props_extra: {cid: {k: v}} —— 追加杆属性（root_bar_id 等）。
    """
    from traceability.model import Component, EngineeringModel, SourceRef, SourceType

    m = EngineeringModel(name="test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf"),
        properties={},
    ))
    for nid, (x, y, z) in (nodes_spec or {}).items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={"x": x, "y": y, "z": z},
        ))
    for i, (cid, bid, length, face) in enumerate(bars_spec):
        props = {
            "bar_id": bid,
            "length_mm_3d": length,
            "face": face,
            "geometry_class": "recognized",
        }
        props.update((props_extra or {}).get(cid) or {})
        m.add_component(Component(
            id=cid, name=cid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties=props,
        ))
        if bid is not None:
            m.dimensions[f"dim_bom_length_{bid}"] = None  # placeholder 覆盖
    for bid, blen in (dims or {}).items():
        from traceability.model import Dimension
        m.dimensions[f"dim_bom_length_{bid}"] = Dimension(
            id=f"dim_bom_length_{bid}", name=f"BOM 长度 {bid}",
            value=blen, unit="mm")
    return m


class BomLengthPhysicalAggregationTest(unittest.TestCase):
    """四面镜像实例按物理杆去重核验。"""

    def test_four_face_instances_counted_once(self):
        """同一物理杆的 F/B/L/R 四实例只算 1 根超差（旧口径算 4）。

        2026-09-02 V1 语义对齐：欠识别（段长 < BOM 母杆）是召回缺口，
        PENDING 不拦交付（与 r_project_bom_master 的 under_identified
        同语义）；本例 500 < 1000 → PENDING。超长（件号错挂）才 FAILED，
        见 test_overlength_bar_fails。
        """
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            # stem=b1 的四面镜像，长度 500 vs BOM 1000（欠识别 50%）
            ("4f_b1_F", "b1", 500.0, "f"),
            ("4f_b1_B", "b1", 500.0, "b"),
            ("4f_b1_L", "b1", 500.0, "l"),
            ("4f_b1_R", "b1", 500.0, "r"),
            # b2 正常
            ("4f_b2_F", "b2", 980.0, "f"),
        ]
        m = _make_model_with_bars(bars, dims={"b1": 1000.0, "b2": 1000.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertIsNotNone(res)
        self.assertEqual(res.status, ValidationStatus.PENDING)
        # 物理杆去重口径：1 根欠识别（四镜像合并），旧实例口径 5
        self.assertIn("1 根欠识别", res.message)
        self.assertIn("旧实例口径 5", res.message)

    def test_overlength_bar_fails(self):
        """超长（段长 > BOM，件号错挂/重复）→ FAILED（数据矛盾拦交付）。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 2000.0, "f"),   # 2000 vs BOM 1000，+100%
            ("4f_b2_F", "b2", 980.0, "f"),   # 正常
        ]
        m = _make_model_with_bars(bars, dims={"b1": 1000.0, "b2": 1000.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertIsNotNone(res)
        self.assertEqual(res.status, ValidationStatus.FAILED)
        self.assertIn("1 根物理杆长度超差", res.message)

    def test_split_segments_checked_independently(self):
        """split 段无共享 root_bar_id 时是独立物理杆，各自核验。

        P2 链口径（2026-09-08）后：同 root_bar_id 的段合并为链核验
        （见 SplitChainSumCaliberTest）；无 root 标记的旧式 id 分段
        仍各自独立。
        """
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            # 无 root_bar_id → 各自独立链（stem 不同）
            ("4f_bar_x_front_F", "112", 3105.0, "f"),
            ("4f_bar_y_front_F", "112", 909.0, "f"),
        ]
        m = _make_model_with_bars(bars, dims={"112": 913.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.FAILED)
        # 3105 超差（+240%），909 匹配
        self.assertIn("1 根物理杆", res.message)

    def test_front_face_preferred_as_representative(self):
        """同 stem 多面实例时 front 代表优先（B 长度不同不用 B）。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_B", "b1", 2000.0, "b"),   # B 先入（错长度）
            ("4f_b1_F", "b1", 1000.0, "f"),   # F 后入覆盖
        ]
        m = _make_model_with_bars(bars, dims={"b1": 1000.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.PASSED)

    def test_pass_message_reports_both_counts(self):
        """通过消息同时带物理杆数与展开实例数。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1000.0, "f"),
            ("4f_b1_B", "b1", 1000.0, "b"),
        ]
        m = _make_model_with_bars(bars, dims={"b1": 1000.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.PASSED)
        self.assertIn("1 根物理杆", res.message)
        self.assertIn("展开实例 2", res.message)


class PhysicalStemTest(unittest.TestCase):
    """_physical_stem 的 id 归一。"""

    def test_stem_extraction(self):
        from traceability.harness.tower_validators import _physical_stem
        self.assertEqual(_physical_stem("4f_bar_x_front_F"), "bar_x_front")
        self.assertEqual(_physical_stem("4f_bar_x_front__split1_B"), "bar_x_front__split1")
        self.assertEqual(_physical_stem("plain_id"), "plain_id")
        self.assertEqual(_physical_stem("4f_b_R"), "b")


class SplitChainSumCaliberTest(unittest.TestCase):
    """P2 split 链长度口径（2026-09-08）。

    图纸分段画线的 BOM 母杆以链为单位核验：单段链用段长，多段链用
    端到端 span（不叠加——T 接重叠段加和会重复计量，606 实证 9348
    vs span 2337）。
    """

    def _nodes_colinear(self):
        # 0..3000 共线节点：链 0→1000→2000→3000
        return {f"n{i}": (0.0, 0.0, float(i * 1000)) for i in range(4)}

    def test_chain_span_matches_bom(self):
        """一根 2880 母杆画成三段（同 root）→ span 核验通过。

        旧口径三段各自 1000 vs 2880 全欠识别；链口径 span=3000，3% 内。
        """
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1000.0, "f"),
            ("4f_b1__split1_F", "b1", 1000.0, "f"),
            ("4f_b1__split2_F", "b1", 1000.0, "f"),
        ]
        nodes = self._nodes_colinear()
        props = {"4f_b1_F": {"from_node": "n0", "to_node": "n1",
                             "root_bar_id": "b1_root"},
                 "4f_b1__split1_F": {"from_node": "n1", "to_node": "n2",
                                     "root_bar_id": "b1_root"},
                 "4f_b1__split2_F": {"from_node": "n2", "to_node": "n3",
                                     "root_bar_id": "b1_root"}}
        m = _make_model_with_bars(bars, dims={"b1": 2950.0},
                                  nodes_spec=nodes, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.PASSED)
        self.assertIn("1 根物理杆", res.message)

    def test_chain_span_not_sum(self):
        """T 接重叠段不叠加：6 段同棱互相 T 接，span 而非加和。

        606 实证语义：加和 9348 vs span 2337。此处用简化几何——段长
        400×6 加和 2400，但端点都在 0..2000 内 → span=2000。
        """
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [(f"4f_b1__s{i}_F", "b1", 400.0, "f") for i in range(6)]
        nodes = {"n0": (0.0, 0.0, 0.0), "n1": (0.0, 0.0, 500.0),
                 "n2": (0.0, 0.0, 1000.0), "n3": (0.0, 0.0, 1500.0),
                 "n4": (0.0, 0.0, 2000.0)}
        props = {"4f_b1__s0_F": {"from_node": "n0", "to_node": "n2"},
                 "4f_b1__s1_F": {"from_node": "n1", "to_node": "n3"},
                 "4f_b1__s2_F": {"from_node": "n2", "to_node": "n4"},
                 "4f_b1__s3_F": {"from_node": "n0", "to_node": "n1"},
                 "4f_b1__s4_F": {"from_node": "n1", "to_node": "n2"},
                 "4f_b1__s5_F": {"from_node": "n3", "to_node": "n4"}}
        for cid in [b[0] for b in bars]:
            props.setdefault(cid, {})["root_bar_id"] = "b1_root"
        m = _make_model_with_bars(bars, dims={"b1": 2000.0},
                                  nodes_spec=nodes, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        # span=2000 = BOM → PASSED（加和口径会是 2400，+20% FAILED）
        self.assertEqual(res.status, ValidationStatus.PASSED)

    def test_chain_span_over_bom_without_flags_fails(self):
        """链 span 超 BOM 且无 suspect/dup 标记 → FAILED（件号错挂）。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1116.0, "f"),
            ("4f_b1__split1_F", "b1", 996.0, "f"),
        ]
        nodes = {"n0": (0.0, 0.0, 0.0), "n1": (0.0, 0.0, 2112.5)}
        props = {"4f_b1_F": {"from_node": "n0", "to_node": "n1",
                             "root_bar_id": "b1_root"},
                 "4f_b1__split1_F": {"from_node": "n1", "to_node": "n0",
                                     "root_bar_id": "b1_root"}}
        # 两段同端点（重叠链）——span 用节点最远距 2112.5
        m = _make_model_with_bars(bars, dims={"b1": 1204.0},
                                  nodes_spec=nodes, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.FAILED)

    def test_chain_over_bom_with_dup_flag_goes_review(self):
        """链 span 超 BOM 但 bar_id_dup（重复标注次实例）→ suspect 队列。

        326 实证：2 段链 span 2112 vs BOM 1204，段上 bar_id_dup=True
        （同视图重复件号非 primary）→ PENDING review，不 FAILED。
        """
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1116.0, "f"),
            ("4f_b1__split1_F", "b1", 996.0, "f"),
        ]
        nodes = {"n0": (0.0, 0.0, 0.0), "n1": (0.0, 0.0, 2112.5)}
        props = {"4f_b1_F": {"from_node": "n0", "to_node": "n1",
                             "root_bar_id": "b1_root", "bar_id_dup": True},
                 "4f_b1__split1_F": {"from_node": "n1", "to_node": "n0",
                                     "root_bar_id": "b1_root"}}
        m = _make_model_with_bars(bars, dims={"b1": 1204.0},
                                  nodes_spec=nodes, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.PENDING)
        self.assertIn("口径存疑", res.message)

    def test_chain_missing_nodes_fall_back_to_sum(self):
        """节点坐标缺失 → 退化加和（链段长加和 vs BOM）。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1000.0, "f"),
            ("4f_b1__split1_F", "b1", 1000.0, "f"),
            ("4f_b1__split2_F", "b1", 1000.0, "f"),
        ]
        props = {cid: {"root_bar_id": "b1_root"} for cid, _, _, _ in bars}
        m = _make_model_with_bars(bars, dims={"b1": 2950.0}, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        # 加和 3000 vs 2950 → 1.7% 通过
        self.assertEqual(res.status, ValidationStatus.PASSED)

    def test_back_face_chain_not_double_counted(self):
        """front 链存在时 B/L/R 镜像链不再核验（物理杆去重保持）。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            ("4f_b1_F", "b1", 1000.0, "f"),
            ("4f_b1__split1_F", "b1", 1000.0, "f"),
            ("4f_b1_B", "b1", 500.0, "b"),   # 镜像实例长度故意不同
        ]
        nodes = {"n0": (0.0, 0.0, 0.0), "n1": (0.0, 0.0, 1000.0),
                 "n2": (0.0, 0.0, 2000.0)}
        props = {"4f_b1_F": {"from_node": "n0", "to_node": "n1",
                             "root_bar_id": "r1"},
                 "4f_b1__split1_F": {"from_node": "n1", "to_node": "n2",
                                     "root_bar_id": "r1"},
                 "4f_b1_B": {"from_node": "n0", "to_node": "n1",
                             "root_bar_id": "r1"}}
        m = _make_model_with_bars(bars, dims={"b1": 2000.0},
                                  nodes_spec=nodes, props_extra=props)
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.PASSED)
        self.assertIn("1 根物理杆", res.message)
        self.assertIn("展开实例 3", res.message)


if __name__ == "__main__":
    unittest.main()
