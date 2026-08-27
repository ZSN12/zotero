# 示例命令

```bash
# 1) 校验 + 报告
python -m traceability.cli validate examples/pipe_network.json
python -m traceability.cli report examples/pipe_network.json

# 2) 铁塔 DXF 解析（Phase 1）
python -m traceability.cli intake-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out tower_model.json

# 3) Harness 验证
python -m traceability.cli harness tower_model.json

# 4) 3D 求解 + 金标准验收
python -m traceability.cli solve-tower tower_model.json \
  --out tower.glb --format glb --golden examples/tower_110kv_golden.json

# 5) 多步编排（P0-1）
python -m traceability.cli run-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out-dir out/run

# 6) 交付包（P0-4）
python -m traceability.cli deliver-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out-dir out/delivery

# 7) 扫描图（Phase 4 候选）
python -m traceability.cli intake-scan examples/clear/tower_front_hd.png --tower --out scan.json

# 8) PDF 转图 + 扫描
python -m traceability.cli compile-drawing examples/tower_scan.pdf --tower --out scan.json
```
