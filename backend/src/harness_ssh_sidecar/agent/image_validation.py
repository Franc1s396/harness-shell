"""用真实 decoder 完整验证图片，保留原始字节且不做转码。"""
from io import BytesIO
import warnings

from PIL import Image, UnidentifiedImageError

from .image_models import AttachmentError, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, ValidatedImage

_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def validate_image(filename: str, data: bytes) -> ValidatedImage:
    """在写事务外拒绝格式、资源、动画和损坏图片；解码失败安全传播。"""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError("AGENT_IMAGE_TOO_LARGE", "Image must contain between 1 byte and 10 MiB")
    if (not filename or len(filename) > 255 or any(char in filename for char in "/\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise AttachmentError("AGENT_IMAGE_NAME_INVALID", "Image display name is invalid")
    try:
        # 限定同步解码期间将资源警告视为错误，不更改 Pillow 像素全局开关。
        with warnings.catch_warnings(action="error", category=Image.DecompressionBombWarning):
            # 自身像素上限低于 Pillow 的默认警告阈值，不修改进程全局 decoder 配置。
            with BytesIO(data) as stream, Image.open(stream) as image:
                media_type = _FORMATS.get(image.format or "")
                if media_type is None:
                    raise AttachmentError("AGENT_IMAGE_FORMAT_UNSUPPORTED", "Only PNG, JPEG, WebP and static GIF images are supported")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise AttachmentError("AGENT_IMAGE_TOO_LARGE", "Image exceeds the 40 million pixel limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise AttachmentError("AGENT_IMAGE_ANIMATED", "Animated images are not supported")
                image.verify()
            # verify 不能替代完整解码；重新打开检测截断的像素流，不保留 decoder 对象。
            with BytesIO(data) as stream, Image.open(stream) as image:
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise AttachmentError("AGENT_IMAGE_TOO_LARGE", "Image exceeds the decoder resource limit") from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as error:
        raise AttachmentError("AGENT_IMAGE_INVALID", "Image cannot be decoded completely") from error
    return ValidatedImage(filename, media_type, width, height, data)
