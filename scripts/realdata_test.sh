#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."   # 若在 scripts/ 下保存；否则改成 cd engineering-trace

export MLLM_PROVIDER="${MLLM_PROVIDER:-kimi-code}"
export MLLM_MODEL="${MLLM_MODEL:-k3-256k}"
# 无 key 时不硬失败，改为跳过 MLLM 相关门禁，其余照跑
if [[ -z "${KIMI_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "⚠ 未设置 KIMI_API_KEY / OPENAI_API_KEY，[4/4] 与 --with-mllm 门禁将 SKIP"
fi

echo "=== [1/4] 验收 ==="
bash scripts/acceptance.sh --with-mllm

echo "=== [2/4] 110kV merge + GLB ==="
python3 -m traceability.cli run-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge \
  --golden examples/tower_110kv_golden.json --format glb \
  --out-dir out/dsh-110kv

echo "=== [3/4] 国网 deliver-project + GLB ==="
GW=examples/external/guowang_35A1
python3 -m traceability.cli deliver-project "$GW" \
  --layer-map "$GW/layer_overlay.json" \
  --bom "$GW/guowang_merged_bom.csv" \
  --out-dir out/dsh-guowang-deliver

echo "=== [4/4] clear + Kimi + confirm + GLB ==="
OUT=out/dsh-clear-kimi
python3 -m traceability.cli run-tower examples/clear/ --backend mllm --out-dir "$OUT"
python3 -m traceability.cli confirm-scan "$OUT/model.json"
python3 -m traceability.cli solve-tower "$OUT/model.json" \
  --format glb --out "$OUT/tower.glb" --allow-scan

echo "=== 完成 ==="
echo "110kV GLB:     out/dsh-110kv/tower.glb"
echo "国网 GLB:      out/dsh-guowang-deliver/tower.glb"
echo "Clear Kimi GLB: $OUT/tower.glb"
