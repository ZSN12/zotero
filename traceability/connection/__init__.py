"""节点连接详图：大样坐标变换 + 节点板 + 螺栓验算（Gap 2）。"""

from .detail_view import DetailViewTransform, parse_detail_view_meta, local_to_global
from .gusset import GussetPlate, parse_gusset_from_detail
from .bolt_verify import BoltGroup, parse_bolt_annotation, verify_bolt_group

__all__ = [
    "DetailViewTransform",
    "parse_detail_view_meta",
    "local_to_global",
    "GussetPlate",
    "parse_gusset_from_detail",
    "BoltGroup",
    "parse_bolt_annotation",
    "verify_bolt_group",
]
