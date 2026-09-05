"""多模态模型（MLLM）可插拔后端。

本质：MLLM 是「眼睛和脑子」，负责把图纸读成结构化候选对象。
本模块定义统一接口，支持三种后端：

    * RuleBasedBackend  —— ezdxf 规则解析（矢量图，无 API 时）
    * MLLMBackend       —— 调用多模态大模型 API（OpenAI 兼容）
    * NullBackend       —— 返回 placeholder 模型（兜底，绝不猜）

设计原则：
    * 模型输出 ≠ 交付物。模型只输出「候选」，交由 skill/contract.py
      强制转成 EngineeringModel，再经 Harness 验证。
    * 所有后端都返回 ModelCandidate（原始候选），不直接返回 EngineeringModel。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple


# --------------------------------------------------------------------------
# 输入 / 候选 数据结构
# --------------------------------------------------------------------------

@dataclass
class DrawingInput:
    """一份待分析的图纸输入。"""
    path: str
    kind: str = "dxf"          # dxf / dwg / pdf / png / jpg / scan
    version: str = "1"
    original_location: str = ""  # 文件原始位置（保留文件/版本/位置）
    tower: bool = False          # 铁塔专用管线：影响 prompt 与后端选择


# --------------------------------------------------------------------------
# 阶段2.3 视觉缓存内容指纹
# --------------------------------------------------------------------------
# 「旧 MLLM 缓存」问题的根源：cache 命中只按图片文件名(stem)键控，改 region bbox、
# 换面板切片、改 prompt 后重渲到同名文件，旧 JSON 仍被命中。这里给缓存加内容指纹：
#   缓存 JSON 顶层须带 "_cache_meta" = {crop_sha, prompt_sha, cache_version}，
#   三者任一不匹配即视为陈旧缓存（fallthrough 重新调用 MLLM）。
# 旧式缓存（无 _cache_meta）一律判陈旧——强制禁止旧缓存。
CACHE_META_KEY = "_cache_meta"
AGENT_CACHE_VERSION = "agent-cache-v1"


def _cache_content_meta(image_path: str, prompt: str) -> Dict[str, str]:
    """计算当前图片+prompt 的内容指纹（crop_sha / prompt_sha / cache_version）。"""
    import hashlib
    img_bytes = Path(image_path).read_bytes()
    return {
        "crop_sha": hashlib.sha256(img_bytes).hexdigest()[:16],
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        "cache_version": AGENT_CACHE_VERSION,
    }


def _cache_meta_matches(parsed: Any, image_path: str, prompt: str) -> bool:
    """缓存 JSON 是否与当前图片内容 + prompt 匹配（防旧缓存）。

    parsed 须是 dict 且带 CACHE_META_KEY，且 crop_sha/prompt_sha/cache_version
    与当前值完全一致才命中。缺 CACHE_META_KEY（旧式缓存）或任一字段不匹配
    都返回 False。
    """
    if not isinstance(parsed, dict):
        return False
    meta = parsed.get(CACHE_META_KEY)
    if not isinstance(meta, dict):
        return False
    want = _cache_content_meta(image_path, prompt)
    return all(meta.get(k) == v for k, v in want.items())


@dataclass
class CandidateObject:
    """模型识别出的一个候选对象（未通过 Skill 契约）。"""
    obj_type: str                     # component / dimension / connection / rule
    data: Dict[str, Any] = field(default_factory=dict)
    source: Optional[Dict[str, Any]] = None
    confidence: float = 0.6           # 模型识别置信度，永远 < 1.0


@dataclass
class ModelCandidate:
    """一次图纸分析的全部候选输出。"""
    input: DrawingInput
    objects: List[CandidateObject] = field(default_factory=list)
    raw: Optional[str] = None         # 模型原始返回（用于审计）
    backend: str = "unknown"
    warnings: List[str] = field(default_factory=list)  # 按条丢弃/降级记录
    meta: Dict[str, Any] = field(default_factory=dict) # 调用日志（model/耗时/raw长度）


class MLLMAnalysisError(RuntimeError):
    """MLLM 分析失败（结构级 Schema 拒绝 / API 失败 / 0 产出）。

    携带调用日志 meta（model、elapsed_s、raw_length、failure_reason），
    供 tower_harness 写入 steps.json，便于定位失败原因。
    """

    def __init__(self, message: str, meta: Optional[Dict[str, Any]] = None,
                 raw: Optional[str] = None, warnings: Optional[List[str]] = None):
        super().__init__(message)
        self.meta = meta or {}
        self.raw = raw
        self.warnings = warnings or []


# --------------------------------------------------------------------------
# 后端接口
# --------------------------------------------------------------------------

class DrawingBackend(Protocol):
    """所有后端必须实现 analyze()。"""
    name: str

    def analyze(self, drawing: DrawingInput) -> ModelCandidate: ...


# --------------------------------------------------------------------------
# 1) 规则后端：ezdxf 解析（矢量图）
# --------------------------------------------------------------------------

class RuleBasedBackend:
    """基于 ezdxf 的规则解析，当前 DXF 默认后端。"""
    name = "rule-based"

    def analyze(self, drawing: DrawingInput) -> ModelCandidate:
        if drawing.kind not in ("dxf", "dwg"):
            return ModelCandidate(
                input=drawing, backend=self.name,
                objects=[], raw="rule-based 后端仅支持 dxf/dwg",
            )

        from .tower_dxf import extract_tower_from_dxf
        model = extract_tower_from_dxf(drawing.path)

        # 把 EngineeringModel 转回候选对象列表（保持统一接口）
        objects: List[CandidateObject] = []
        for cid, comp in model.components.items():
            objects.append(CandidateObject(
                obj_type="component",
                data={"id": cid, "kind": comp.kind, "name": comp.name,
                      "properties": comp.properties},
                source=asdict_or_none(comp.source),
                confidence=comp.source.confidence if comp.source else 0.5,
            ))
        for did, dim in model.dimensions.items():
            objects.append(CandidateObject(
                obj_type="dimension",
                data={"id": did, "name": dim.name, "value": dim.value,
                      "unit": dim.unit, "origin": dim.origin.value},
                source=asdict_or_none(dim.source),
                confidence=dim.source.confidence if dim.source else 0.5,
            ))
        return ModelCandidate(input=drawing, objects=objects, backend=self.name)


class TowerScanBackend:
    """铁塔扫描图规则后端（P1-2 降级路径）。

    无 MLLM API 时，用版面分析 + 霍夫线检测产出候选模型，
    而不是 NullBackend 的 placeholder——扫描图仍有可用候选进人工复核队列。
    """

    name = "rule-based-scan"

    def analyze(self, drawing: DrawingInput) -> ModelCandidate:
        from .tower_layout import analyze_tower_scan
        model = analyze_tower_scan(drawing.path)
        objects: List[CandidateObject] = []
        for cid, comp in model.components.items():
            objects.append(CandidateObject(
                obj_type="component",
                data={"id": cid, "kind": comp.kind, "name": comp.name,
                      "properties": comp.properties},
                source=asdict_or_none(comp.source),
                confidence=comp.source.confidence if comp.source else 0.5,
            ))
        for did, dim in model.dimensions.items():
            objects.append(CandidateObject(
                obj_type="dimension",
                data={"id": did, "name": dim.name, "value": dim.value,
                      "unit": dim.unit, "origin": dim.origin.value},
                source=asdict_or_none(dim.source),
                confidence=dim.source.confidence if dim.source else 0.5,
            ))
        return ModelCandidate(input=drawing, objects=objects, backend=self.name)


# --------------------------------------------------------------------------
# 2) MLLM 后端：调用多模态大模型 API（OpenAI 兼容）
# --------------------------------------------------------------------------

class MLLMBackend:
    """调用 OpenAI 兼容的多模态 API。

    环境变量：
        MLLM_PROVIDER           kimi-code | moonshot | openai（默认 openai）
        KIMI_API_KEY            Kimi Code 会员密钥（MLLM_PROVIDER=kimi-code）
        MOONSHOT_API_KEY        Moonshot 开放平台密钥
        OPENAI_API_KEY          OpenAI 或通用回退密钥
        OPENAI_BASE_URL         自定义端点（可选；kimi-code 默认 api.kimi.com/coding/v1）
        MLLM_MODEL              模型名（kimi-code 默认 k3-256k）
        MLLM_TIMEOUT            单次调用总超时秒（默认 300，大图 OCR 需更久）
        MLLM_CONNECT_TIMEOUT    连接超时秒（默认 30）
        MLLM_MAX_IMAGE_EDGE     送图前最长边上限（默认 4096，Kimi 视觉推荐上限；大图 OCR 超时可下调）
    """

    name = "mllm"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        from .mllm_providers import resolve_mllm_config

        cfg = resolve_mllm_config(provider=provider, api_key=api_key, base_url=base_url, model=model)
        self.provider = cfg["provider"]
        self.api_key = cfg["api_key"] or ""
        self.base_url = cfg["base_url"]
        self.model = cfg["model"] or "gpt-4o"

    def available(self) -> bool:
        # agent-vision：本地 ezdxf 规则提取（0 API 调用，最快）。
        # antigravity-ocx：经本地 opencodex relay（OAuth 免 client key），
        # 无显式 api_key 也算可用。
        if self.provider in ("agent-vision", "antigravity-ocx", "glm-relay"):
            return True
        return bool(self.api_key)

    def analyze(self, drawing: DrawingInput) -> ModelCandidate:
        if not self.available():
            from .mllm_providers import mllm_config_status
            return ModelCandidate(
                input=drawing, backend=self.name,
                objects=[], raw=f"未配置 MLLM API Key。{mllm_config_status()}",
                meta={"model": self.model, "provider": self.provider,
                      "failure_reason": "未配置 MLLM API Key"},
            )

        if drawing.tower:
            from .mllm_tower_prompt import TOWER_MLLM_PROMPT as _TOWER_PROMPT
            prompt = _TOWER_PROMPT
        else:
            prompt = _MLLM_PROMPT

        import time as _time
        t0 = _time.time()
        image_b64 = None
        meta: Dict[str, Any] = {"model": self.model, "provider": self.provider}
        try:
            if drawing.kind in ("png", "jpg", "jpeg", "scan", "pdf"):
                image_b64, img_meta = _encode_image(drawing.path)
                meta.update(img_meta)
            else:
                image_b64 = None

            client = self._make_client()
            messages: List[Dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            if image_b64:
                messages[0]["content"].append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                })

            raw = None
            # 本地 opencodex relay（antigravity-ocx / glm-relay）非流式请求有
            # ~600s 硬超时：长 JSON 生成超时即 502。stream 分块保持连接活跃，
            # 可越过该墙。
            if getattr(self, "provider", "") in ("antigravity-ocx", "glm-relay"):
                # antigravity-ocx relay 专有：通过 stream 聚合结果，防止 relay 502
                stream = client.chat.completions.create(model=self.model, messages=messages, stream=True)
                chunks = []
                for c in stream:
                    if c.choices and c.choices[0].delta.content:
                        chunks.append(c.choices[0].delta.content)
                raw = "".join(chunks)
            else:
                try:
                    resp = client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        response_format={"type": "json_object"},
                    )
                except Exception as exc:
                    if not _is_response_format_error(exc):
                        raise
                    resp = client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                    )
                raw = resp.choices[0].message.content or "{}"

            meta["elapsed_s"] = round(_time.time() - t0, 2)
            meta["raw_length"] = len(raw)
            raw = _extract_json_text(raw)
            parsed = json.loads(raw)
            if drawing.tower:
                from .mllm_tower_prompt import parse_tower_mllm_output_with_warnings
                objects, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
                meta["parse_warnings"] = len(warnings)
                meta["objects"] = len(objects)
                if problems:
                    meta["failure_reason"] = f"铁塔 Schema：{problems[:3]}"
                    meta["parse_warning_detail"] = warnings[:20]
                    return ModelCandidate(input=drawing, backend=self.name,
                                          objects=[], raw=f"MLLM 输出未过铁塔 Schema：{problems}",
                                          warnings=warnings, meta=meta)
                if warnings:
                    meta["parse_warning_detail"] = warnings[:20]
                return ModelCandidate(input=drawing, objects=objects, raw=raw,
                                      backend=self.name, warnings=warnings, meta=meta)
            objects = _parse_candidate_objects(parsed)
            meta["objects"] = len(objects)
            return ModelCandidate(input=drawing, objects=objects, raw=raw,
                                  backend=self.name, meta=meta)
        except Exception as exc:  # 网络/API 失败 -> 降级为 null，绝不抛
            meta["elapsed_s"] = round(_time.time() - t0, 2)
            meta["failure_reason"] = str(exc)[:500]
            return ModelCandidate(
                input=drawing, backend=self.name,
                objects=[], raw=f"MLLM 调用失败：{exc}", meta=meta,
            )

    def call_agent_json(
        self,
        prompt: str,
        image_path: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        agent: str = "agent",
    ) -> Tuple[Any, Dict[str, Any]]:
        """P1 分步 Agent 调用（A1/A2 共用）。

        与 analyze() 不同：这里每次只跑一个小任务（只读件号 / 只检几何），
        用独立 prompt + 小 Schema，单步日志 duration_ms。
        优先读取本地 Agent 视觉缓存（如 out/agent_vision_cache 或与图片同名 .json），
        若命中则免 API 调用直接返回。
        """
        import time as _time

        t0 = _time.time()
        meta: Dict[str, Any] = {"model": self.model, "provider": self.provider, "agent": agent}

        # 1. 优先检查本地 Agent 视觉注入/缓存
        if image_path:
            img_p = Path(image_path)
            candidate_cache_paths = [
                img_p.with_suffix(".json"),
                img_p.parent / f"{agent}_{img_p.stem}.json",
                Path("out/agent_vision_cache") / f"{agent}_{img_p.stem}.json",
                Path("out/agent_vision_cache") / f"{img_p.stem}.json",
            ]
            for cp in candidate_cache_paths:
                if cp.exists():
                    try:
                        parsed = json.loads(cp.read_text(encoding="utf-8"))
                        # 阶段2.3：命中必须通过内容指纹校验（crop_sha/prompt_sha/
                        # cache_version），否则判陈旧缓存并 fallthrough 重新调用，
                        # 杜绝改 region/切片/prompt 后重渲到同名文件仍吃旧结果。
                        if not _cache_meta_matches(parsed, image_path, prompt):
                            meta.setdefault("warnings", []).append(
                                f"cache {cp.name} 内容指纹不匹配(crop_sha/prompt_sha/"
                                f"cache_version)，按陈旧缓存跳过"
                            )
                            continue
                        meta.update({
                            "source": "agent_vision_cache",
                            "cache_file": str(cp),
                            "elapsed_s": round(_time.time() - t0, 3),
                            "duration_ms": round((_time.time() - t0) * 1000, 2),
                        })
                        return parsed, meta
                    except Exception as exc:
                        # P4：缓存文件损坏时记录 warning 并 fallthrough 到下一个候选，
                        # 不再静默吞异常。
                        meta.setdefault("warnings", []).append(
                            f"cache {cp.name} 不可读，跳过：{exc}"
                        )

        if not self.available():
            from .mllm_providers import mllm_config_status
            meta.update({
                "failure_reason": "未配置 MLLM API Key 且无本地视觉缓存",
                "elapsed_s": 0.0,
                "duration_ms": 0.0,
                "note": mllm_config_status(),
            })
            return None, meta

        try:
            image_b64 = None
            if image_path:
                image_b64, img_meta = _encode_image(image_path)
                meta.update(img_meta)

            client = self._make_client()
            messages: List[Dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            if image_b64:
                messages[0]["content"].append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                })

            raw = None
            # 本地 opencodex relay（antigravity-ocx / glm-relay）非流式请求有
            # ~600s 硬超时：长 JSON 生成超时即 502。stream 分块保持连接活跃，
            # 可越过该墙。
            if getattr(self, "provider", "") in ("antigravity-ocx", "glm-relay"):
                # antigravity-ocx relay 专有：通过 stream 聚合结果，防止 relay 502
                stream = client.chat.completions.create(model=self.model, messages=messages, stream=True)
                chunks = []
                for c in stream:
                    if c.choices and c.choices[0].delta.content:
                        chunks.append(c.choices[0].delta.content)
                raw = "".join(chunks)
            else:
                try:
                    resp = client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        response_format={"type": "json_object"},
                    )
                except Exception as exc:
                    if not _is_response_format_error(exc):
                        raise
                    resp = client.chat.completions.create(model=self.model, messages=messages)
                raw = resp.choices[0].message.content or "{}"

            meta["elapsed_s"] = round(_time.time() - t0, 2)
            meta["duration_ms"] = round((_time.time() - t0) * 1000, 2)
            meta["raw_length"] = len(raw)
            parsed = json.loads(_extract_json_text(raw))
            if schema is not None:
                import jsonschema
                validator = jsonschema.Draft202012Validator(schema)
                problems = []
                for err in validator.iter_errors(parsed):
                    where = ".".join(str(p) for p in err.path) or "(root)"
                    problems.append(f"{where}: {err.message}")
                if problems:
                    meta["failure_reason"] = f"{agent} Schema：{problems[:3]}"
                    return None, meta
            return parsed, meta
        except Exception as exc:
            meta["elapsed_s"] = round(_time.time() - t0, 2)
            meta["duration_ms"] = round((_time.time() - t0) * 1000, 2)
            meta["failure_reason"] = str(exc)[:500]
            return None, meta

    def _make_client(self):
        """构造带超时的 OpenAI 兼容客户端（P1：单次调用 timeout 可配）。"""
        from openai import OpenAI  # type: ignore
        import httpx

        # 大图件号 OCR 默认 300s；连接 30s。可用 MLLM_TIMEOUT 覆盖。
        timeout_s = float(os.environ.get("MLLM_TIMEOUT") or "300")
        connect_s = float(os.environ.get("MLLM_CONNECT_TIMEOUT") or "30")
        is_local = "127.0.0.1" in str(self.base_url) or "localhost" in str(self.base_url)
        proxy = None if is_local else (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None)
        http_client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=connect_s),
            proxy=proxy,
        )
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http_client,
        )


# --------------------------------------------------------------------------
# 3) Null 后端：兜底，绝不猜
# --------------------------------------------------------------------------

class NullBackend:
    """没有任何解析能力时的兜底后端：只产出一个 placeholder 对象。"""
    name = "null"

    def analyze(self, drawing: DrawingInput) -> ModelCandidate:
        return ModelCandidate(
            input=drawing, backend=self.name,
            objects=[CandidateObject(
                obj_type="dimension",
                data={"id": "dim_placeholder", "name": "待补测尺寸",
                      "value": None, "unit": "", "origin": "placeholder"},
                source={"source_type": "unknown", "reference": drawing.path,
                        "confidence": 0.0},
                confidence=0.0,
            )],
            raw="null 后端：无解析能力，返回 placeholder",
        )


# --------------------------------------------------------------------------
# 后端选择
# --------------------------------------------------------------------------

def choose_backend(
    drawing: DrawingInput,
    mllm: Optional[MLLMBackend] = None,
    prefer_mllm: bool = False,
) -> DrawingBackend:
    """按输入类型选择后端。

    优先级：
        * dxf/dwg -> 默认 RuleBasedBackend（矢量规则）；当 prefer_mllm=True
          且配了 API 时，改走 MLLMBackend（栅格化 + 整塔识别，适用于
          ezdxf 图层映射失效的真实国网图纸）。
        * tower + png/jpg/scan/pdf -> MLLMBackend 优先（配 API 时）；
          无 API 则降级到 TowerScanBackend（规则线检测），绝不走 Null 丢弃扫描图
        * 其它 png/jpg/scan/pdf -> MLLMBackend（若配置 API），否则 NullBackend
    """
    mllm = mllm or MLLMBackend()
    if drawing.kind in ("dxf", "dwg"):
        if prefer_mllm and mllm.available():
            return mllm
        return RuleBasedBackend()
    if drawing.tower and drawing.kind in ("png", "jpg", "jpeg", "scan", "pdf"):
        if mllm.available():
            return mllm
        return TowerScanBackend()
    if mllm.available():
        return mllm
    return NullBackend()


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

_MLLM_PROMPT = """你是工程图纸识别器。请识别图中所有工程对象，输出 JSON。

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. 对每个对象必须给出 source（图纸号、位置、confidence）。
3. 读不到尺寸就写 null，并在 origin 写 "placeholder"，绝不猜值。
4. 输出格式：

{
  "objects": [
    {"obj_type": "component", "data": {"id": "...", "kind": "tower_bar",
      "name": "...", "properties": {"bar_id": "G01", "section": "L100x8"}},
     "confidence": 0.85},
    {"obj_type": "dimension", "data": {"id": "...", "name": "...",
      "value": 5000, "unit": "mm", "origin": "measured"},
     "confidence": 0.9}
  ]
}
"""


def _encode_image(path: str, max_edge: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    """把图片缩放后转 base64（MLLM 输入）。

    P1：送图前缩放，最长边 ≤ max_edge。max_edge 可显式传入，否则读
    MLLM_MAX_IMAGE_EDGE（默认 2048；大图件号 OCR 易超时时可下调为 1536）。
    输出保持 PNG。PDF 会自动先栅格化成 PNG（P1-3）。
    返回 (base64, meta)；meta 记录缩放前后尺寸与发送字节数。
    """
    import io

    if max_edge is None:
        max_edge = int(os.environ.get("MLLM_MAX_IMAGE_EDGE") or "4096")

    p = Path(path)
    meta: Dict[str, Any] = {"max_edge": max_edge}

    if p.suffix.lower() == ".pdf":
        from .pdf_raster import rasterize_pdf_to_png
        p = Path(rasterize_pdf_to_png(path))

    data = p.read_bytes()
    meta["original_bytes"] = len(data)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        meta["original_size"] = [img.width, img.height]
        # 只缩小，不放大；统一 RGB + PNG 输出（保持可预期画质/格式）
        if max(img.width, img.height) > max_edge:
            ratio = max_edge / max(img.width, img.height)
            new_w, new_h = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
            img = img.convert("RGB").resize((new_w, new_h))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
            meta["resized_to"] = [new_w, new_h]
        elif p.suffix.lower() != ".png":
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        meta["bytes_sent"] = len(data)
    except Exception as exc:  # PIL 不可用则原图直发，并记录 warning
        meta["resize_warning"] = f"PIL 不可用，原图直发：{exc}"

    return base64.b64encode(data).decode("ascii"), meta


def _is_response_format_error(exc: Exception) -> bool:
    """判断异常是否为「服务端不支持 response_format/json_object」类错误。

    只有这类错误才值得回退为纯文本输出再抽 JSON；网络超时/鉴权失败
    等异常不应触发重试（否则会重复调用、白白消耗超时时间与配额）。
    """
    text = str(exc).lower()
    return ("response_format" in text) or ("json_object" in text) or \
        ("unsupported parameter" in text and "json" in text)


def _extract_json_text(raw: str) -> str:
    """从模型返回中抽出 JSON 对象（兼容 markdown 代码块）。"""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _parse_candidate_objects(parsed: Any) -> List[CandidateObject]:
    if isinstance(parsed, dict):
        parsed = parsed.get("objects", [])
    out = []
    for item in parsed or []:
        out.append(CandidateObject(
            obj_type=item.get("obj_type", "component"),
            data=item.get("data", {}),
            confidence=float(item.get("confidence", 0.6)),
        ))
    return out


def asdict_or_none(obj) -> Optional[Dict[str, Any]]:
    if obj is None:
        return None
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(obj)
    return dict(obj)
