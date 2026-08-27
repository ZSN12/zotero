#!/usr/bin/env bash
# 铁塔管线验收脚本（P2-11 / Phase A）。
#
# 把 P0/P1/P2 的关键验收口径固定成一条命令，CI / 交付前跑一遍即可：
#   1. tower_110kv --merge -> 五条验证规则 5/5 passed + 金标准偏差 < 2%
#   2. guowang 02 单立面 -> parse-rate（件号关联率）>= 50%
#   3. examples/clear/ 扫描批量 -> 3 个 view_type 正确 + merged model 存在
#   4. 全量 pytest 单测通过
#   5. （可选 --with-mllm）tower_front_hd + k3：A1 件号 > 0 且 A3 关联率 > 3%
#
# 用法：
#   bash scripts/acceptance.sh
#   bash scripts/acceptance.sh --with-mllm          # 追加 Kimi 门禁（需 API key）
#   MLLM_PROVIDER=kimi-code MLLM_MODEL=k3-256k KIMI_API_KEY=sk-... \
#     bash scripts/acceptance.sh --with-mllm
#
# 退出码：全部通过 0，任一失败 1。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

WITH_MLLM=0
for arg in "$@"; do
  case "$arg" in
    --with-mllm) WITH_MLLM=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

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

echo "==> [2/5] guowang 02 parse-rate >= 50%"
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

echo "==> [3/5] guowang cross_file front+plan（nodes_solved > 0 + 连接规则）"
"$PY" -m traceability.cli cross-file-batch \
  examples/external/guowang_35A1/ \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir "$OUT/guowang-cross" >/dev/null

"$PY" - "$OUT/guowang-cross/batch_report.json" "$OUT/guowang-cross/model.json" <<'PYEOF'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
model = json.load(open(sys.argv[2], encoding="utf-8"))
mr = report.get("merge_report") or {}
ns = mr.get("nodes_solved", 0)
if ns <= 0:
    print(f"FAIL: guowang cross_file nodes_solved={ns} <= 0")
    sys.exit(1)
front_nodes = [c for c in model.get("components", {}).values() if c.get("kind") == "tower_node" and (c.get("properties") or {}).get("view_type") == "front"]
if front_nodes and ns < len(front_nodes):
    print(f"FAIL: nodes_solved={ns} < front_nodes={len(front_nodes)}")
    sys.exit(1)
rules = model.get("rules") or {}
bolt = [k for k in rules if k.startswith("r_bolt_group_")]
gusset = [k for k in rules if k.startswith("r_gusset_")]
if not gusset:
    print("FAIL: 合并模型缺少 r_gusset_* 规则")
    sys.exit(1)
if not bolt:
    print("FAIL: 合并模型缺少 r_bolt_group_* 规则")
    sys.exit(1)
print(f"PASS: nodes_solved={ns}/{len(front_nodes) or '?'}, gusset_rules={len(gusset)}, bolt_rules={len(bolt)}")
PYEOF

echo "==> [4/5] examples/clear/ 扫描批量（3 view_type + merged model）"
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

echo "==> [5/5] pytest 全量单测"
"$PY" -m pytest tests -q

if [ "$WITH_MLLM" = "1" ]; then
  echo "==> [5/5] Kimi 门禁（tower_front_hd + k3：A1>0、A3 关联率 > 3%）"
  KEY="${KIMI_API_KEY:-${OPENAI_API_KEY:-}}"
  if [ -z "$KEY" ]; then
    echo "SKIP: 未配置 KIMI_API_KEY / OPENAI_API_KEY，跳过 Kimi 门禁（代码路径需真实 key 才能产出件号）"
  else
    # 强制 k3（默认已是 k3-256k），跑单张清晰扫描图 A0→A4
    MLLM_PROVIDER="${MLLM_PROVIDER:-kimi-code}" \
    MLLM_MODEL="${MLLM_MODEL:-k3-256k}" \
    "$PY" -m traceability.cli run-tower examples/clear/tower_front_hd.png \
      --backend mllm --out-dir "$OUT/front-mllm" >/dev/null

    "$PY" - "$OUT/front-mllm/steps.json" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
steps = {st["id"]: st for st in s.get("steps", [])}
a1 = steps.get("a1_labels")
a3 = steps.get("a3_link")
if a1 is None or a3 is None:
    print("FAIL: steps.json 缺少 a1_labels / a3_link 步骤")
    sys.exit(1)
labels = (a1.get("detail") or {}).get("labels", 0)
rate = (a3.get("detail") or {}).get("association_rate", 0.0)
if a1.get("status") == "skipped":
    print(f"FAIL: A1 被跳过（无 API/无件号）：{a1.get('error','')}")
    sys.exit(1)
if labels <= 0:
    print(f"FAIL: A1 件号数 {labels} <= 0（k3 应在清晰扫描图读到件号）")
    sys.exit(1)
if rate <= 0.03:
    print(f"FAIL: A3 关联率 {rate:.4f} <= 0.03（修复前基线 ~3%）")
    sys.exit(1)
print(f"PASS: A1 labels={labels} > 0, A3 association_rate={rate:.4f} > 0.03")
PYEOF
  fi
fi

echo ""
echo "✅ 验收全部通过"
