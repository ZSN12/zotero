#!/usr/bin/env python3
"""实验：Gemini 3.7 Flash（opencodex relay）识别 DXF 材料明细表，提取件号→截面→长度→数量。

对比规则解析 parse_bom_dxf_anchored 的结果，评估多模态识别的准确率。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.intake.hybrid_dxf_agent import render_dxf_preview_with_mapping
from traceability.intake.mllm_backend import MLLMBackend, _encode_image
from traceability.intake.tower_bom import parse_bom_dxf_anchored

PROMPT = """这是一张输电铁塔结构施工图（DXF 渲染图）。图右上方有一个「材料明细表」（构件清单表）。

请只提取材料明细表的内容，不要管塔体图形。表格每一行是：
  件号（纯数字，如 301/303/304...）| 截面规格（如 L90X6 / L50X4 / L40X3）| 长度(mm) | 数量

请逐行输出，严格按 JSON 格式：
{"items": [{"bar_id": "301", "section": "L90X6", "length_mm": 6823, "qty": 1}, ...]}

要求：
1. bar_id 必须是材料表里的纯数字件号（不要混入尺寸标注里的数字）
2. section 保持原样（含 Q345 前缀如果有）
3. length_mm 是整数毫米
4. qty 是整数
5. 只输出 JSON，不要任何解释文字
"""


def rule_rows(dxf_path: str) -> list[dict]:
    rows = parse_bom_dxf_anchored(dxf_path)
    return [
        {"bar_id": str(r["bar_id"]), "section": (r.get("section") or "").strip(),
         "length_mm": int(r.get("length_mm", 0) or 0), "qty": int(r.get("qty", 1) or 1)}
        for r in rows
        if str(r.get("bar_id", "")).isdigit() and 100 <= int(r["bar_id"]) <= 999
    ]


def main() -> int:
    dxf = REPO / "out/xianyu-acceptance/batch-jc1/dxf/35A1-JC1-04.dxf"
    png = Path("/tmp/jc1_04_gemini.png")
    render_dxf_preview_with_mapping(str(dxf), str(png), dpi=200)

    # Gemini 3.7 Flash 经 opencodex relay（OAuth 免 key，但 OpenAI client
    # 空 key 会发 "Bearer " 非法 header，这里传占位非空 key，relay 忽略 auth）。
    backend = MLLMBackend(provider="antigravity-ocx", api_key="ocx-relay")
    print(f"provider={backend.provider} model={backend.model} available={backend.available()}")
    if not backend.available():
        print("Gemini relay 不可用")
        return 1

    img_b64, meta = _encode_image(str(png))
    client = backend._make_client()
    import time
    t0 = time.time()
    resp = client.chat.completions.create(
        model=backend.model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]}],
    )
    elapsed = time.time() - t0
    raw = resp.choices[0].message.content or ""
    print(f"\n耗时 {elapsed:.1f}s, 原始输出长度 {len(raw)}")

    # 解析 JSON
    import re
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0))
        items = data.get("items", [])
    except Exception as e:
        print(f"JSON 解析失败: {e}")
        print("原始输出前 500 字:", raw[:500])
        return 1

    print(f"\nGemini 识别出 {len(items)} 行")

    # 对比规则解析
    rule = rule_rows(str(dxf))
    rule_by_id = {r["bar_id"]: r for r in rule}
    print(f"规则解析出 {len(rule)} 行")

    # 逐件号对比
    matched = 0
    total_checked = 0
    for it in items:
        bid = str(it.get("bar_id", ""))
        if bid in rule_by_id:
            total_checked += 1
            r = rule_by_id[bid]
            sec_ok = (it.get("section", "") or "").replace("Q345", "").replace(" ", "") == \
                     r["section"].replace("Q345", "").replace(" ", "")
            len_ok = abs(int(it.get("length_mm", 0) or 0) - r["length_mm"]) <= 5
            qty_ok = int(it.get("qty", 0) or 0) == r["qty"]
            if sec_ok and len_ok and qty_ok:
                matched += 1
            else:
                print(f"  不一致 {bid}: Gemini({it.get('section')},{it.get('length_mm')},{it.get('qty')}) "
                      f"vs 规则({r['section']},{r['length_mm']},{r['qty']})")

    # 规则有但 Gemini 漏的
    gemini_ids = {str(it.get("bar_id", "")) for it in items}
    missed = [bid for bid in rule_by_id if bid not in gemini_ids]
    print(f"\n准确率: {matched}/{total_checked}（在 Gemini 识别出的件号里，与规则一致的比例）")
    print(f"Gemini 漏识别件号数: {len(missed)}/{len(rule)}")
    if missed:
        print(f"  漏的前 15 个: {missed[:15]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
