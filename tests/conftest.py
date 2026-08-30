"""pytest 共享 fixture。

- ``guowang_cross_file_result``：session 级缓存国网全册图纸解析结果，
  使 ``cross_file_batch`` 在单次测试会话中只执行一次，供多个断言复用。

国网全册解析入口是 ``traceability.intake.tower_batch.cross_file_batch``，
输入目录 ``examples/external/guowang_35A1``（立面 02 + 平面 35C2-SJG1-ML +
大样 03 + 目录 00-1），overlay 为 ``layer_overlay.json``。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUOWANG_DIR = REPO / "examples" / "external" / "guowang_35A1"
GUOWANG_OVERLAY = GUOWANG_DIR / "layer_overlay.json"


@pytest.fixture(scope="session")
def guowang_cross_file_result():
    """缓存国网全册目录的 cross_file_batch 解析结果（会话级，仅解析一次）。

    返回 ``cross_file_batch`` 的 dict（含 ``model_path`` / ``merge_report`` /
    ``models_by_stem`` / ``files`` 等）。落盘目录是会话级临时目录，保证
    多个测试复用同一份解析产物；session 结束后自动清理。
    """
    if not GUOWANG_DIR.exists():
        pytest.skip("国网目录不存在: %s" % GUOWANG_DIR)

    from traceability.intake.tower_batch import cross_file_batch

    # 会话级临时目录：整个测试会话共用同一 out_dir，解析产物只写一次。
    _tmp = tempfile.TemporaryDirectory(prefix="guowang-cross-file-session-")
    try:
        result = cross_file_batch(
            GUOWANG_DIR,
            _tmp.name,
            layer_map_path=str(GUOWANG_OVERLAY),
        )
    except Exception:
        _tmp.cleanup()
        raise

    # 缓存解析结果对象（dict），供多个测试复用；绝不在 fixture 内二次解析。
    yield result
    _tmp.cleanup()


# --------------------------------------------------------------------------- #
# 阶段8.2：unittest 风格测试无法注入 fixture 参数，提供会话级缓存加载函数。
# 首次调用真正解析，之后每次返回深拷贝（防止测试原地修改污染缓存）。
# --------------------------------------------------------------------------- #

_SHEET02_CACHE: dict = {}


def guowang_sheet02_model():
    """国网 35A1-JC1-02 单图解析模型（会话级只解析一次，深拷贝返回）。"""
    import copy

    if "model" not in _SHEET02_CACHE:
        dxf = GUOWANG_DIR / "35A1-JC1-02.dxf"
        if not dxf.exists():
            raise RuntimeError(f"国网 02 图纸不存在: {dxf}")
        from traceability.intake.tower_dxf import extract_tower_from_dxf

        _SHEET02_CACHE["model"] = extract_tower_from_dxf(
            str(dxf), layer_map_path=str(GUOWANG_OVERLAY),
        )
    return copy.deepcopy(_SHEET02_CACHE["model"])
