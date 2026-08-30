"""阶段 0.2 运行清单（run_manifest.json）测试。

全部使用纯合成数据 + 全新 tmp 目录：
    * 不读 out/jc1-hybrid-kimi-batch/、out/35A1-JC1-full-deliver/、
      out/agent_vision_cache/ 等旧产物；
    * 不触发真实 MLLM / 网络（deliver 用 ezdxf 纯矢量路径）；
    * 不注入 GT、不改容差。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from traceability.model import Component, EngineeringModel
from traceability.project.run_manifest import (
    DETERMINISTIC_SCOPE,
    STAGE_COUNT_FIELDS,
    build_run_manifest,
    sha256_file,
    write_run_manifest,
)


def _make_merged_model() -> EngineeringModel:
    """合成合并模型：2 物理杆 + 1 派生杆 + 2 节点（source_file 归属 sheet-a）。"""
    m = EngineeringModel(name="merged-synthetic")
    for i in range(2):
        m.add_component(Component(
            id=f"bar_{i}", name=f"物理杆{i}", kind="tower_bar",
            properties={
                "source_file": "sheet-a", "drawing_view": "sheet-a",
                "geometry_class": "recognized", "bar_id": f"10{i}",
            },
        ))
    m.add_component(Component(
        id="bar_derived", name="横隔面派生杆", kind="tower_bar",
        properties={"source_file": "sheet-a", "face": "diaphragm"},
    ))
    for i in range(2):
        m.add_component(Component(
            id=f"node_{i}", name=f"节点{i}", kind="tower_node",
            properties={"source_file": "sheet-a"},
        ))
    return m


def _write_steps_json(path: Path, *, cache_hit: bool = False) -> None:
    """合成 per-sheet steps.json（ProcessingGraph.to_dict 结构子集）。"""
    detail: dict = {"ezdxf_bars": 12, "nodes": 9, "stitched_fragments": 3}
    if cache_hit:
        detail["source"] = "agent_vision_cache"
    steps = {"name": "hybrid-dxf-sheet-a", "steps": [
        {"id": "a0_layout", "name": "版面分析（A0）", "status": "passed", "detail": {}},
        {"id": "a2_geom", "name": "几何检测（A2）", "status": "passed", "detail": detail},
    ]}
    path.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")


class Sha256FileTest(unittest.TestCase):
    """sha256_file：分块读取正确性 + 容错。"""

    def test_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.txt"
            payload = b"35A1-JC1 run manifest\n" * 100
            p.write_bytes(payload)
            self.assertEqual(sha256_file(p), hashlib.sha256(payload).hexdigest())

    def test_chunked_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.bin"
            payload = bytes(range(256)) * 10240  # 2.5 MiB，跨多个 1MiB 分块
            p.write_bytes(payload)
            self.assertEqual(sha256_file(p), hashlib.sha256(payload).hexdigest())

    def test_missing_file_returns_none(self):
        self.assertIsNone(sha256_file("/nonexistent/nope.dxf"))


class BuildRunManifestTest(unittest.TestCase):
    """build_run_manifest 纯函数：字段、sha256 正确性、缺失容错。"""

    def test_top_level_fields(self):
        m1 = build_run_manifest()
        m2 = build_run_manifest()
        self.assertRegex(m1["run_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(uuid.UUID(m1["run_id"]).hex, m1["run_id"])  # 合法 uuid4
        self.assertNotEqual(m1["run_id"], m2["run_id"])  # 每次运行唯一
        ts = datetime.fromisoformat(m1["created_at"])
        self.assertEqual(ts.utcoffset().total_seconds(), 0)  # UTC
        self.assertEqual(m1["deterministic_scope"], DETERMINISTIC_SCOPE)

    def test_inputs_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dxf = base / "35A1-JC1-02.dxf"
            dxf_bytes = b"SLSDXF synthetic"
            dxf.write_bytes(dxf_bytes)
            bom = base / "merged_bom.csv"
            bom.write_text("piece_no,len_mm\n105,1200\n", encoding="utf-8")
            ov = base / "overlay.json"
            ov.write_text("{}", encoding="utf-8")
            m = build_run_manifest(
                input_dir=base, overlay_path=ov, bom_path=bom,
            )
            dxfs = m["inputs"]["dxfs"]
            self.assertEqual(len(dxfs), 1)
            self.assertEqual(dxfs[0]["file"], "35A1-JC1-02.dxf")
            self.assertEqual(dxfs[0]["sha256"], hashlib.sha256(dxf_bytes).hexdigest())
            self.assertEqual(dxfs[0]["bytes"], len(dxf_bytes))
            self.assertEqual(m["inputs"]["bom"]["sha256"], sha256_file(bom))
            self.assertEqual(m["inputs"]["overlay"]["sha256"], sha256_file(ov))

    def test_inputs_missing_bom_and_dir(self):
        m = build_run_manifest(
            input_dir="/nonexistent-dir",
            bom_path="/nonexistent/bom.csv",
        )
        self.assertEqual(m["inputs"]["dxfs"], [])
        self.assertIsNone(m["inputs"]["bom"])
        self.assertIsNone(m["inputs"]["overlay"])

    def test_mllm_cache_used_from_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps_path = Path(tmp) / "steps.json"
            _write_steps_json(steps_path, cache_hit=True)
            m = build_run_manifest(
                steps_by_stem={"sheet-a": steps_path},
                mllm_provider="kimi", mllm_model="moonshot-v1-8k",
            )
            self.assertEqual(m["mllm"]["provider"], "kimi")
            self.assertEqual(m["mllm"]["model"], "moonshot-v1-8k")
            self.assertIs(m["mllm"]["cache_used"], True)
            self.assertEqual(m["mllm"]["cache_used_by_sheet"], {"sheet-a": True})

    def test_mllm_null_without_context(self):
        # ezdxf 纯矢量路径：无 MLLM 上下文 → 全部 null。
        m = build_run_manifest()
        self.assertIsNone(m["mllm"]["provider"])
        self.assertIsNone(m["mllm"]["model"])
        self.assertIsNone(m["mllm"]["cache_used"])
        self.assertIsNone(m["mllm"]["cache_used_by_sheet"])

    def test_stages_from_steps_and_merged_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps_path = Path(tmp) / "steps.json"
            _write_steps_json(steps_path, cache_hit=False)
            merged = _make_merged_model()
            m = build_run_manifest(
                sheet_ids=["sheet-a", "sheet-b"],
                sheet_stats={
                    "sheet-a": {"bars": 5, "nodes": 4},
                    "sheet-b": {"bars": 2, "nodes": 2},
                },
                merged_model=merged,
                steps_by_stem={"sheet-a": steps_path},
            )
            sa = m["stages"]["sheet-a"]
            # steps 里的 a2 明细优先于 sheet_stats
            self.assertEqual(sa["a2_vector_bars"], 12)
            self.assertEqual(sa["a2_nodes"], 9)
            # 合并模型按 source_file 归属
            self.assertEqual(sa["merged_bars"], 3)
            self.assertEqual(sa["merged_nodes"], 2)
            self.assertEqual(sa["physical_bars"], 2)   # fail-closed：仅 recognized
            self.assertEqual(sa["derived_bars"], 1)    # face=diaphragm 派生
            sb = m["stages"]["sheet-b"]
            # 无 steps → 回退 sheet_stats（ezdxf 纯矢量路径的 A2 计数）
            self.assertEqual(sb["a2_vector_bars"], 2)
            self.assertEqual(sb["a2_nodes"], 2)
            self.assertEqual(sb["merged_bars"], 0)  # 未参与合并，归属计数为 0
            for field in STAGE_COUNT_FIELDS:
                self.assertIn(field, sb)

    def test_stages_null_when_missing(self):
        # 无 steps / stats / merged：六项阶段计数全部 null（禁止编造）。
        m = build_run_manifest(sheet_ids=["sheet-x"])
        sx = m["stages"]["sheet-x"]
        self.assertEqual(set(sx.keys()), set(STAGE_COUNT_FIELDS))
        for field in STAGE_COUNT_FIELDS:
            self.assertIsNone(sx[field])

    def test_outputs_existing_only_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "model.json").write_text("{}", encoding="utf-8")
            sub = base / "cross_file"
            sub.mkdir()
            (sub / "model.json").write_text("{}", encoding="utf-8")
            m = build_run_manifest(
                out_dir=base,
                output_candidates=[
                    base / "model.json",
                    sub / "model.json",
                    base / "skeleton.glb",  # 不存在 → 不收录
                ],
            )
            self.assertEqual(m["outputs"], ["cross_file/model.json", "model.json"])

    def test_bar_changelog_aggregates_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps_a = Path(tmp) / "a_steps.json"
            _write_steps_json(steps_a)  # stitched_fragments=3
            steps_b = Path(tmp) / "b_steps.json"
            steps_b.write_text(json.dumps({"steps": [
                {"id": "a2_geom", "name": "几何检测（A2）", "status": "passed",
                 "detail": {"stitched_fragments": 2, "injected_bars": 7}},
            ]}), encoding="utf-8")
            m = build_run_manifest(
                steps_by_stem={"sheet-a": steps_a, "sheet-b": steps_b},
                merge_report={"synthetic_side_nodes": 4},
            )
            ch = m["bar_changelog"]
            self.assertEqual(ch["counts"]["stitched_fragments"], 5)  # 3 + 2
            self.assertEqual(ch["counts"]["injected_bars"], 7)
            self.assertEqual(ch["counts"]["synthetic_side_nodes"], 4)
            self.assertIsNone(ch["counts"]["split_nodes"])  # 几何代码未记录 → null
            self.assertEqual(ch["total_events"], 16)
            self.assertTrue(any(
                s["sheet"] == "sheet-b" and s["event"] == "stitched_fragments"
                for s in ch["samples"]
            ))

    def test_malformed_steps_json_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "steps.json"
            bad.write_text("{not json", encoding="utf-8")
            m = build_run_manifest(
                sheet_ids=["sheet-a"],
                steps_by_stem={"sheet-a": bad},
            )
            for field in STAGE_COUNT_FIELDS:
                self.assertIsNone(m["stages"]["sheet-a"][field])
            self.assertIsNone(m["mllm"]["cache_used"])


class WriteRunManifestTest(unittest.TestCase):
    """write_run_manifest：JSON 落盘 + 失败只 warning。"""

    def test_write_and_roundtrip(self):
        manifest = build_run_manifest(project_id="T-run")
        with tempfile.TemporaryDirectory() as tmp:
            path = write_run_manifest(manifest, tmp)
            self.assertIsNotNone(path)
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["run_id"], manifest["run_id"])
            self.assertEqual(data["deterministic_scope"], DETERMINISTIC_SCOPE)

    def test_write_failure_warns_not_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("占用路径", encoding="utf-8")
            with self.assertWarns(UserWarning):
                path = write_run_manifest({"run_id": "x"}, blocker)
            self.assertIsNone(path)  # 失败返回 None，不抛异常


class DeliverProjectRunManifestTest(unittest.TestCase):
    """deliver_project 集成：ezdxf 纯矢量路径落盘 run_manifest.json（无 MLLM）。"""

    def test_deliver_writes_run_manifest(self):
        import ezdxf

        from traceability.project.delivery import deliver_project

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp = base / "input"
            inp.mkdir()
            # 合成 mini DXF（ezdxf 生成，纯矢量，无任何外部产物依赖）
            doc = ezdxf.new("R2010")
            msp = doc.modelspace()
            msp.add_line((0, 0), (6000, 0))
            msp.add_line((6000, 0), (3000, 5000))
            msp.add_line((3000, 5000), (0, 0))
            doc.saveas(inp / "mini-sheet.dxf")

            delivery = deliver_project(inp, base / "out")
            rm_path = base / "out" / "run_manifest.json"
            self.assertTrue(rm_path.exists(), "deliver_project 应落盘 run_manifest.json")
            rm = json.loads(rm_path.read_text(encoding="utf-8"))
            # 顶层字段
            self.assertRegex(rm["run_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(rm["deterministic_scope"], DETERMINISTIC_SCOPE)
            # 交付 dict 与 manifest 关联
            self.assertEqual(delivery.get("run_id"), rm["run_id"])
            self.assertEqual(delivery.get("run_manifest_path"), str(rm_path))
            # 输入哈希：合成 DXF 被如实登记
            dxfs = rm["inputs"]["dxfs"]
            self.assertEqual([d["file"] for d in dxfs], ["mini-sheet.dxf"])
            self.assertEqual(dxfs[0]["sha256"], sha256_file(inp / "mini-sheet.dxf"))
            # ezdxf 路径无 MLLM → null
            self.assertIsNone(rm["mllm"]["provider"])
            self.assertIsNone(rm["mllm"]["cache_used"])
            # 关键输出清单包含两份 manifest
            self.assertIn("project_delivery.json", rm["outputs"])
            self.assertIn("run_manifest.json", rm["outputs"])
            self.assertTrue((base / "out" / "project_delivery.json").exists())


if __name__ == "__main__":
    unittest.main()
