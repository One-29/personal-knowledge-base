"""文档上传解析边界。

对外只暴露经过校验的不可变数据和两个入口函数；ZIP 结构、Markdown 图片语法
与图片解码细节留在包内，路由和存储层无需知道解析实现。
"""

from .models import PreparedAsset, PreparedDocument, PreparedOccurrence, UploadValidationError
from .package_hash import calculate_package_hash
from .uploads import prepare_upload, read_upload

__all__ = [
    "PreparedAsset",
    "PreparedDocument",
    "PreparedOccurrence",
    "UploadValidationError",
    "calculate_package_hash",
    "prepare_upload",
    "read_upload",
]
