"""ladder_z 分散式阶梯（35A1-ZC1 版式）单测。

背景（Phase 4 续，2026-09-04 诊断实测定稿）：
  * 35A1-ZC1 的 01-1 单线总图是「分段索引图」——H 向标注只有每站的
    接地节拍对 (1000,2400) 与两个分段高度 4500/6400、4000/7000，
    无 ZC1 式整塔阶梯列（``ladder_from_sheet`` 返回 None）；
  * 阶梯证据分散在各分段立面自带的「段内阶梯列」：首值=段高锚
    （5400/6500/8000…），其余=等节拍；相邻列错半位（800+1600×4+800）；
  * 重复视图：锚+节拍多重集完全相同的页互为复制（07/11、10/13 实测）；
  * 主链=册号序堆叠 02,04,05,06,07,08，和 36400≈GT 塔顶 36300，
    边界 6/6 命中 GT（±1000），全列并集 GT 层位命中 91%（32/35）。

铁律对齐：全部纯 DXF 证据，无 GT 注入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.intake.ladder_z import (  # noqa: E402
    Ladder,
    distributed_ladder,
    ladder_from_sheet,
    segment_ladders_from_sheets,
)

_GT_A1_ZC1 = REPO / "examples/gt/35A1-ZC1_ground_truth.json"
_A1_DXF_DIR = REPO / "out/tier3/35A1-ZC1/dxf"
_ZC1_DXF_DIR = REPO / "out/tier3/35A2-ZC1/dxf"


def _a1_sheets():
    return sorted(_A1_DXF_DIR.glob("35A1-ZC1-*.dxf")) if _A1_DXF_DIR.exists() else []


needs_a1 = pytest.mark.skipif(
    not _a1_sheets() or not _GT_A1_ZC1.exists(),
    reason="35A1-ZC1 DXF/GT 不在仓库（外部验收数据）",
)


def _gt_levels() -> list:
    import json
    gt = json.loads(_GT_A1_ZC1.read_text(encoding="utf-8"))
    return sorted({round(p[2]) for p in gt["nodes"].values()})


def _make_sheet(path: Path, cols: dict, x0: float = 34900.0, y0: float = -7000.0):
    """造一张带段内阶梯列的分段立面 DXF。cols: {dx: [值...]}。

    真实版式（35A1-ZC1 实测）：锚在列底（图面 y 最小），节拍向上；
    生成顺序 = 自底向上（锚先、y 递增）。
    """
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for dx, vals in cols.items():
        x = x0 + dx
        y = y0
        for v in vals:
            # H 向 DIM：defpoint 与 defpoint3 同 y（水平测量），文本=值
            dim = msp.add_linear_dim(
                base=(x, y), p1=(x, y), p2=(x + 10.0, y),
                text=str(int(v)), override={"dimtxsty": "Standard"})
            dim.render()
            y += 10.0
    doc.saveas(path)


class TestDistributedLadder:

    def test_signature_dedup_removes_duplicate_views(self, tmp_path):
        """锚+节拍签名相同的两页只保留一个（07/11、10/13 复制视图判据）。"""
        _make_sheet(tmp_path / "T-02.dxf", {0: [5400, 900, 900, 800, 700, 700, 700, 700]})
        _make_sheet(tmp_path / "T-07.dxf", {0: [4000, 2000, 2000]})
        _make_sheet(tmp_path / "T-11.dxf", {0: [4000, 2000, 2000]})  # 07 的复制视图
        segs = segment_ladders_from_sheets(sorted(tmp_path.glob("T-*.dxf")))
        stems = [s.stem for s in segs]
        assert "T-07" in stems and "T-11" not in stems, stems
        assert "T-02" in stems

    def test_stacking_offsets_are_prefix_sums(self, tmp_path):
        """册号序堆叠：段边界（锚前缀和）都在 junctions 里。"""
        _make_sheet(tmp_path / "T-02.dxf", {0: [5400, 900, 900, 800, 700, 700, 700, 700]})
        _make_sheet(tmp_path / "T-04.dxf", {0: [6500, 800, 800, 800, 800, 800, 800, 800]})
        lad = distributed_ladder(sorted(tmp_path.glob("T-*.dxf")))
        assert lad is not None
        # 段边界 = 锚前缀和 [0, 5400, 11900]
        for acc in (5400.0, 5400.0 + 6500.0):
            assert any(abs(j - acc) <= 1.0 for j in lad.junctions), \
                f"段边界 {acc} 缺失"
        # 段内节拍累计也是 junction（02 段从锚起：5400+900=6300）
        assert 6300.0 in lad.junctions
        # 04 段：11900+800=12700
        assert 12700.0 in lad.junctions

    def test_single_sheet_no_ladder(self, tmp_path):
        """少于 2 段无法堆叠——返回 None。"""
        _make_sheet(tmp_path / "T-02.dxf", {0: [5400, 900, 900, 800, 700, 700, 700, 700]})
        assert distributed_ladder(sorted(tmp_path.glob("T-*.dxf"))) is None

    def test_small_value_columns_ignored(self, tmp_path):
        """构件尺寸列（无塔高量级锚）不进阶梯。"""
        _make_sheet(tmp_path / "T-02.dxf", {0: [5400, 900, 900, 800, 700, 700, 700, 700]})
        _make_sheet(tmp_path / "T-03.dxf", {0: [90, 90, 80, 70, 70, 70, 70]})  # 构件尺寸
        segs = segment_ladders_from_sheets(sorted(tmp_path.glob("T-*.dxf")))
        assert [s.stem for s in segs] == ["T-02"]

    @needs_a1
    def test_a1_zc1_ladder_from_sheet_fails(self):
        """01-1 索引图无整塔阶梯——旧路径在 35A1-ZC1 上必须失败（版式判定）。"""
        assert ladder_from_sheet(_A1_DXF_DIR / "35A1-ZC1-01-1.dxf") is None

    @needs_a1
    def test_a1_zc1_distributed_ladder_hits_gt(self):
        """35A1-ZC1 分散式重建：主链边界对齐 GT、层位命中 ≥85%。"""
        lad = distributed_ladder(
            _a1_sheets(), ladder_sheet=_A1_DXF_DIR / "35A1-ZC1-01-1.dxf")
        assert lad is not None, "分散式阶梯应重建成功"
        # 主链锚序列 [5400,6500,8000,8000,4000,4500] 前缀和 = 段边界
        anchors = [5400, 6500, 8000, 8000, 4000, 4500]
        acc = 0.0
        for anchor in anchors:
            acc += anchor
            assert any(abs(j - acc) <= 1.0 for j in lad.junctions), \
                f"段边界 {acc} 缺失"
        # GT 层位命中（主链区间内，±500mm）
        lv = _gt_levels()
        main_js = [j for j in lad.junctions if j <= 36400 + 500]
        hit = sum(1 for z in lv if any(abs(z - p) <= 500 for p in main_js))
        assert hit / len(lv) >= 0.85, f"GT 层位命中 {hit}/{len(lv)} 不足 85%"

    @needs_a1
    def test_a1_zc1_duplicate_views_excluded(self):
        """07/11、10/13 复制视图被签名去重剔除。"""
        segs = segment_ladders_from_sheets(
            _a1_sheets(), exclude_stems=["35A1-ZC1-01-1"])
        stems = [s.stem for s in segs]
        assert "35A1-ZC1-11" not in stems, "11 是 07 的复制视图"
        assert "35A1-ZC1-13" not in stems, "13 是 10 的复制视图"
        assert "35A1-ZC1-07" in stems and "35A1-ZC1-10" in stems

    def test_zc1_primary_path_unaffected(self):
        """35A2-ZC1 主路径（01-1 整塔阶梯）不被分散式兜底改变。"""
        if not (_ZC1_DXF_DIR / "35A2-ZC1-01-1.dxf").exists():
            pytest.skip("35A2-ZC1 DXF 不在仓库")
        lad = ladder_from_sheet(_ZC1_DXF_DIR / "35A2-ZC1-01-1.dxf")
        assert lad is not None, "ZC1 主阶梯不应退化"
        # 累计锚 39400/33000 是 ZC1 版式的图纸明示标高
        assert 39400.0 in [round(a) for a in lad.cum_anchors]
