"""tarchive —— 庭审笔录争点与引用归档工具（合成材料专用）。

仅使用 Python 标准库。系统不生成法律结论，只对笔录文本做归档、
索引与核阅状态维护。
"""

from .models import (
    ArchiveError,
    IngestError,
    NotFoundError,
    WorkflowError,
)
from .store import Store

__all__ = [
    "ArchiveError",
    "IngestError",
    "NotFoundError",
    "WorkflowError",
    "Store",
]

__version__ = "0.1.0"
