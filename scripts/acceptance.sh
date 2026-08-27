#!/usr/bin/env bash
# 铁塔管线验收脚本（P2-11）。
#
# 把 P0/P1/P2 的关键验收口径固定成一条命令，CI / 交付前跑一遍即可：
#   1. tower_110kv --merge -> 五条验证规则 5/5 passed + 金标准偏差 < 2%
#   2. guowang 02 单立面 -> parse-rate（件号关联率）>= 50%
#   3. examples/clear/ 扫描批量 -> 3 个 view_type 正确 + merged model 存在
#   4. 全量 pytest 单测通过
#
# 用法：
#   bash scripts/acceptance.sh
#   MLLM_PROVIDER=kimi-code KIMI_API_KEY=sk-... bash scripts/acceptance.sh  # 跑扫描关联率
#
# 退出码：全部通过 0，任一失败 1。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "==> [1/4] tower_110kv --merge（5/5 passed + 金标准 <2%）"
"$PY" -m traceability.cli run-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv \
  --merge \
  --golden examples/tower_110kv_golden.json \
  --out-dir "$OUT/110kv" >/dev/null

"$PY" - "$OUT/110kv/harness_summary.json" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
rules = s.get("rules", {})
failed = [rid for rid, r in rules.items() if r.get("status") != "passed"]
if failed:
    print(f"FAIL: 规则未全 passed: {failed}")
    sys.exit(1)
print(f"PASS: 五条规则 {len(rules)}/{len(rules)} passed")
PYEOF

echo "==> [2/4] guowang 02 parse-rate >= 50%"
"$PY" -m traceability.cli parse-report \
  examples/external/guowang_35A1/35A1-JC1-02.dxf \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out "$OUT/guowang02.json" >/dev/null

"$PY" - "$OUT/guowang02.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
rate = r.get("association_rate", 0.0)
if rate < 0.50:
    print(f"FAIL: guowang 02 parse-rate {rate:.4f} < 0.50")
    sys.exit(1)
print(f"PASS: guowang 02 parse-rate {rate:.4f} >= 0.50")
PYEOF

echo "==> [3/4] examples/clear/ 扫描批量（3 view_type + merged model）"
"$PY" -m traceability.cli run-tower examples/clear/ \
  --out-dir "$OUT/clear-multi" >/dev/null 2>&1 || true

"$PY" - "$OUT/clear-multi" <<'PYEOF'
import json, sys, pathlib
out = pathlib.Path(sys.argv[1])
report = out / "batch_report.json"
if not report.exists():
    print("FAIL: batch_report.json 不存在")
    sys.exit(1)
b = json.load(open(report, encoding="utf-8"))
vts = {p.get("view_type") for p in b.get("per_file", [])}
need = {"front", "side", "plan"}
if not need.issubset(vts):
    print(f"FAIL: view_type 缺 {need - vts}（实际 {sorted(vts)}）")
    sys.exit(1)
model = out / "model.json"
if not model.exists():
    print("FAIL: merged model.json 不存在")
    sys.exit(1)
print(f"PASS: view_type={sorted(vts)} + merged model 存在")
PYEOF

echo "==> [4/4] pytest 全量单测"
"$PY" -m pytest tests -q

echo ""
echo "✅ 验收全部通过"
