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


def _make_model_with_bars(bars_spec, dims=None):
    """构造带 BOM 长度的模型。bars_spec: [(cid, bar_id, length, face)]"""
    from traceability.model import Component, EngineeringModel, SourceRef, SourceType

    m = EngineeringModel(name="test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf"),
        properties={},
    ))
    for i, (cid, bid, length, face) in enumerate(bars_spec):
        m.add_component(Component(
            id=cid, name=cid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={
                "bar_id": bid,
                "length_mm_3d": length,
                "face": face,
                "geometry_class": "recognized",
            },
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
        """split 段（__splitN）是不同物理段，各自独立核验。"""
        from traceability.harness.tower_validators import validate_bom_length_match
        from traceability.model import ValidationStatus

        bars = [
            # 6224mm 原杆拆成三段（互斥），只有 909 段匹配 BOM 913
            ("4f_bar_x_front_F", "112", 3105.0, "f"),
            ("4f_bar_x_front__split1_F", "112", 909.0, "f"),
            ("4f_bar_x_front__split1__split2_F", "112", 2209.0, "f"),
        ]
        m = _make_model_with_bars(bars, dims={"112": 913.0})
        res = validate_bom_length_match(m, "r_bom_length_match")
        self.assertEqual(res.status, ValidationStatus.FAILED)
        # 3 段里 2 段超差（3105 和 2209），909 段匹配
        self.assertIn("2 根物理杆", res.message)

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


if __name__ == "__main__":
    unittest.main()
