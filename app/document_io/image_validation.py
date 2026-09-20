"""图片类型与解码完整性校验。"""

import io

from PIL import Image, UnidentifiedImageError

from app.core.config import settings

from .models import UploadValidationError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_FORMATS = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
}
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def validate_image(path: str, data: bytes) -> tuple[str, str, int, int]:
    """验证实际格式、尺寸和完整性；一期只接受单帧静态图片。"""
    if not data:
        raise UploadValidationError(f"图片文件为空：{path}")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            if width <= 0 or height <= 0:
                raise UploadValidationError(f"图片尺寸无效：{path}")
            if width * height > settings.max_image_pixels:
                raise UploadValidationError(f"图片像素数量超过上限：{path}", status_code=413)
            if int(getattr(image, "n_frames", 1)) != 1:
                raise UploadValidationError(f"一期不支持动态图片：{path}")
            image.verify()
        # verify() 校验容器结构；重新打开并 load()，确保像素流也能完整解码。
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except UploadValidationError:
        raise
    except (EOFError, UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise UploadValidationError(f"图片文件已损坏或无法解码：{path}") from None

    mime_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
        image_format
    )
    if mime_type is None:
        raise UploadValidationError(f"图片实际格式不受支持：{path}")
    return image_format, mime_type, width, height
