"""DXF → 产品站图纸裁切图（web/site/assets/）。

用 ezdxf drawing add-on 把关键册渲染成 PNG，再按包围盒裁到塔身
核心区。只取背景线（同 trace SVG 口径），不做任何前景叠加——
产品页用它们展示「输入是真实施工图」，非渲染示意图。

用法：
  python3 scripts/export_site_crops.py \
    --dxf-dir out/35A1-JC1-full-deliver/_dxf_scope \
    --out web/site/assets
"""
from __future__ import annotations

import argparse
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

# (dxf 文件名, 输出名, 标题, 竖向裁切比, 横向裁切区间) —— 裁切去大留白，
# 聚焦主视图。x-frac 为 (起, 止) 包围盒宽度占比。
CROPS = [
    ("35A1-JC1-02.dxf", "dxf-elevation-02.png", "Elevation · 35A1-JC1-02",
     0.72, (0.10, 0.46)),
    ("35A1-JC1-06.dxf", "dxf-plan-06.png", "Body Panel · 35A1-JC1-06",
     0.8, (0.08, 0.60)),
]


def render_crop(dxf_path: Path, out_path: Path, title: str, keep: float,
                x_frac: tuple[float, float]) -> None:
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    fig = plt.figure(figsize=(8, 11), dpi=160, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend).draw_layout(msp, finalize=True)
    ax.set_facecolor("white")
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    # 塔图内容集中在竖长包围盒；中心竖向裁 keep 比例 + 横向取 x_frac 区间
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    cy, half_h = (y0 + y1) / 2, (y1 - y0) * keep / 2
    ax.set_ylim(cy - half_h, cy + half_h)
    w = x1 - x0
    ax.set_xlim(x0 + w * x_frac[0], x0 + w * x_frac[1])
    ax.set_title(title, fontsize=11, color="#333", pad=8)

    fig.savefig(out_path, facecolor="white")
    plt.close(fig)
    print(f"✓ {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dxf-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for stem, name, title, keep, x_frac in CROPS:
        src = args.dxf_dir / stem
        if not src.exists():
            print(f"✗ 跳过（缺 DXF）：{src}")
            continue
        render_crop(src, args.out / name, title, keep, x_frac)


if __name__ == "__main__":
    main()
