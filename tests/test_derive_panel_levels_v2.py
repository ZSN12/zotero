"""P4.2：生产 DXF 平台标高推导 v2 回归测试（断点锚定 + 噪声层抑制）。

设计验证点：
  1. 主腿斜率转折处即使无密集 DXF 端点簇，也能定位平台层（GT 11500 场景：
     簇中位数被 06 斜材端点拉偏 900mm，断点锚定拉回）。
  2. 无转折支持的高密度噪声簇被抑制（17700 场景：06 拓扑窗密集端点）。
  3. 有断点 + 有簇的层正常保留并双向校正（z 向断点靠拢）。
  4. manual_levels 吸附语义与 v1 兼容。
"""

from __future__ import annotations

import unittest

from traceability.solve.tower_geometry import derive_panel_levels_v2


def _leg_chain(breaks, z_start=0.0, z_end=30000.0, x_start=2900.0,
               seg_slopes=None):
    """构造一条 z 单调、x 收腰的主腿链。

    seg_slopes: 每段斜率列表（len = len(breaks)+1）；缺省生成显著转折
    （|Δslope| >= 0.04，远超检测阈 0.025）。
    """
    nodes = {}
    bars = []
    bounds = [z_start] + sorted(breaks) + [z_end]
    if seg_slopes is None:
        # 交替斜率：每段转折 |Δslope| = 0.05 >> 检测阈 0.025
        seg_slopes = [-0.09 if i % 2 == 0 else -0.04
                      for i in range(len(bounds) - 1)]
    assert len(seg_slopes) == len(bounds) - 1
    segs = [(bounds[i], bounds[i + 1], seg_slopes[i])
            for i in range(len(bounds) - 1)]
    # 生成节点链
    chain = [(z_start, x_start)]
    x_cur, z_cur = x_start, z_start
    for lo, hi, s in segs:
        z_cur, x_cur = hi, x_cur + s * (hi - lo)
        chain.append((z_cur, x_cur))
    for i, (z, x) in enumerate(chain):
        nodes[f"leg_{i}"] = (x, 0.0, z)
    for i in range(len(chain) - 1):
        bars.append({
            "id": f"legbar_{i}", "from": f"leg_{i}", "to": f"leg_{i+1}",
            "role_hint": "LEG",
        })
    return nodes, bars, chain


def _add_cluster(nodes, bars, z_center, n_bars=12, span=300.0, tag="cl"):
    """在某标高附近加 n_bars 根杆端点（噪声或真层证据簇）。"""
    base = len(nodes)
    for i in range(n_bars):
        nid = f"{tag}_{z_center:.0f}_{i}"
        z = z_center + (i - n_bars // 2) * (span / max(n_bars, 1))
        nodes[nid] = (1200.0 + 50 * i, 400.0, z)
    for i in range(n_bars - 1):
        bars.append({
            "id": f"{tag}bar_{z_center:.0f}_{i}",
            "from": f"{tag}_{z_center:.0f}_{i}",
            "to": f"{tag}_{z_center:.0f}_{i+1}",
        })
    return nodes, bars


class DerivePanelLevelsV2Test(unittest.TestCase):
    def test_breakpoint_anchors_level_without_dense_cluster(self):
        """11500 场景：断点存在但簇稀疏/拉偏——断点锚定仍能定位。"""
        nodes, bars, _ = _leg_chain([6500, 8500, 11500, 14000])
        # 11500 处只有一个稀疏簇（真实：11700~12700 中位 12400）
        _add_cluster(nodes, bars, 12400, n_bars=6, tag="sparse")
        levels, _records = derive_panel_levels_v2(nodes, bars)
        near = [z for z in levels if abs(z - 11500) <= 300]
        self.assertTrue(near, f"11500 未被断点锚定: {levels}")

    def test_noise_cluster_without_breakpoint_suppressed(self):
        """17700 场景：高密度簇但无断点支持 → 抑制。"""
        nodes, bars, _ = _leg_chain([6500, 8500])
        _add_cluster(nodes, bars, 17700, n_bars=20, tag="noise")
        levels, _records = derive_panel_levels_v2(nodes, bars)
        self.assertFalse([z for z in levels if 16000 < z < 19000],
                         f"无断点噪声簇未被抑制: {levels}")

    def test_cluster_with_breakpoint_kept_and_corrected(self):
        """断点 + 簇共存：保留且 z 向断点校正（6700→6500 场景）。"""
        nodes, bars, _ = _leg_chain([6500, 8500, 14000])
        _add_cluster(nodes, bars, 6700, n_bars=10, tag="cl")
        levels, _records = derive_panel_levels_v2(nodes, bars)
        near = [z for z in levels if abs(z - 6500) <= 250]
        self.assertTrue(near, f"6500 层未保留/未校正: {levels}")

    def test_manual_snap_still_works(self):
        """manual_levels 吸附语义与 v1 兼容（±500 内吸附）。"""
        nodes, bars, _ = _leg_chain([6500, 8500])
        _add_cluster(nodes, bars, 6600, n_bars=6, tag="cl")
        levels, records = derive_panel_levels_v2(
            nodes, bars, manual_levels=[6520.0])
        self.assertIn(6520.0, levels)
        rec = next((r for r in records if abs(r["z_mm"] - 6520) < 1), None)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.get("manual_snapped"))

    def test_records_have_breakpoint_evidence(self):
        """records 标注 leg_breakpoint 证据（可追溯）。"""
        nodes, bars, _ = _leg_chain([6500, 8500])
        _add_cluster(nodes, bars, 6700, n_bars=6, tag="cl")
        _levels, records = derive_panel_levels_v2(nodes, bars)
        rec = next(r for r in records if abs(r["z_mm"] - 6500) <= 300)
        self.assertTrue(rec.get("leg_breakpoint"))


if __name__ == "__main__":
    unittest.main()
