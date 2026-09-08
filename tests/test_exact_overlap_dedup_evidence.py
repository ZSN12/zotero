# -*- coding: utf-8 -*-
"""exact_overlap_dedup 证据优先层判据方向回归测试（ZC1 S11d 吞杆 bug）。

背景（2026-09-08 P0 审计定位）：证据优先层删「识别杆的严格非识别
孪生」，旧判据 ``ov >= 0.9 * min(l1, l2)`` 以两杆较短者为基准——
「识别杆 ⊂ 补全杆」的包含关系也判重（识别杆 05 册画线止 26464 被
legspan 19223→27400 覆盖 88.8%…split 链叠加满足 90%），把 legspan
整条删除，[26464,27400] 补全增量段随之丢失（ZC1 dual-recon 266→251
的真凶之一）。修正：删除候选 b 仅当 b 的几何被识别杆 a 完整覆盖
（``ov >= 0.9 * len(b)``）——b 是 a 的副本才可删；a 比 b 短时 b 是
互补外推段，保留。
"""
import unittest

from traceability.solve.tower_geometry import exact_overlap_dedup


def _nodes():
    # 同一棱线（x=y=z 方向单位化后共线）：真实主腿角钢轴 z 0→10000
    return {
        "nA": (0.0, 0.0, 0.0),
        "nB": (0.0, 0.0, 10000.0),
    }


def _bar(bid, fn, tn, origin):
    return {"id": bid, "from": fn, "to": tn, "geometry_origin": origin}


class EvidencePreferredDirectionTest(unittest.TestCase):
    def test_true_copy_removed(self):
        """真副本：模板杆与识别杆错位同长（中点同 cell）→ 删模板杆。"""
        nodes = {"a": (0.0, 0.0, 0.0), "b": (0.0, 0.0, 10000.0),
                 "c": (0.0, 0.0, 10.0), "d": (0.0, 0.0, 10010.0)}
        bars = [
            _bar("rec", "a", "b", "dxf_geom"),
            _bar("tpl", "c", "d", "terminal_pair_gen"),
        ]
        out, rep = exact_overlap_dedup(nodes, bars)
        ids = {b_["id"] for b_ in out}
        self.assertEqual(ids, {"rec"}, "识别杆完整覆盖模板杆 → 删模板杆")
        self.assertEqual(rep.get("removed_evidence_preferred"), 1)

    def test_extension_kept(self):
        """外推段保留：识别杆（短）只覆盖补全杆（长）的一部分 → 都保留。

        ZC1 S11d 实测形态：05 册 dxf 腿杆 z 19223→26464（7262mm）与
        legspan 补全杆 19223→27400（8177mm）共线——识别杆是图纸截断
        的证据，补全杆是参数化外推，删补全杆会丢 [26464,27400] 段。
        """
        nodes = {"a": (0.0, 0.0, 19223.0), "b": (0.0, 0.0, 26464.0),
                 "c": (0.0, 0.0, 27400.0), "m": (0.0, 0.0, 22800.0)}
        bars = [
            _bar("dxf", "a", "b", "dxf_geom"),
            _bar("legspan", "m", "c", "leg_span_completion"),
        ]
        out, _rep = exact_overlap_dedup(nodes, bars)
        ids = {b_["id"] for b_ in out}
        self.assertEqual(ids, {"dxf", "legspan"},
                         "识别杆 ⊂ 补全杆 → 补全杆不是副本，保留")

    def test_near_equal_still_deduped(self):
        """等长严格重复（真正两份放置）依旧删除——修复不弱化原语义。"""
        nodes = {"a": (0.0, 0.0, 0.0), "b": (0.0, 0.0, 8000.0),
                 "c": (0.0, 0.0, 10.0), "d": (0.0, 0.0, 8010.0)}
        bars = [
            _bar("rec", "a", "b", "dxf_geom"),
            _bar("tpl", "c", "d", "terminal_pair_gen"),
        ]
        out, _rep = exact_overlap_dedup(nodes, bars)
        ids = {b_["id"] for b_ in out}
        self.assertEqual(ids, {"rec"})


if __name__ == "__main__":
    unittest.main()
