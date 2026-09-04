"""Engineering Traceability —— 从一张图，到可供 AI 使用的工程上下文。

三阶段管线：
    1. DRAWING INTAKE          多源图纸接入（扫描图、PDF、DWG、DXF）
    2. ENGINEERING COMPILATION 工程信息编译（构件、尺寸、连接 + 交叉核验）
    3. VERIFIED DELIVERY       可信结果交付（Agent Harness 编排 + 验证 + 导出）

本包提供数据模型、依赖 DAG、变更传播与命令行工具。
"""

__version__ = "0.4.0"  # 与 pyproject.toml [project].version 同步（P3-7 修正 0.1.0 漂移）
