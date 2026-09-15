"""Agent 图片附件的固定限制、领域数据与安全错误。"""
from dataclasses import dataclass, field
from uuid import UUID

MAX_IMAGE_BYTES = 10_485_760
MAX_IMAGE_PIXELS = 40_000_000
MAX_MESSAGE_IMAGES = 5
IMAGE_TOKEN_ESTIMATE = 1000
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


class AttachmentError(RuntimeError):
    """携带明确审查的公开原因，不携带文件内容。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        """保存对外稳定错误和安全提示。"""
        super().__init__(safe_message)
        self.error_code = error_code  # HTTP 和 Agent 领域错误身份。
        self.safe_message = safe_message  # 不包含文件名、字节或 Provider 正文。


@dataclass(frozen=True)
class ValidatedImage:
    """完整验证后的原图；只由操作局部持有原始字节。"""
    filename: str  # 有界展示名，不作为文件路径。
    media_type: str  # decoder 确认的真实 MIME。
    width: int  # 原图像素宽。
    height: int  # 原图像素高。
    data: bytes = field(repr=False)  # 原始字节，不转码、不加入 repr。


@dataclass(frozen=True)
class AttachmentInfo:
    """无需加载 BLOB 即可返回的附件公开元数据。"""
    attachment_id: UUID  # Backend 生成的附件标识。
    draft_id: UUID  # 临时上传所属草稿。
    filename: str  # 展示名称。
    media_type: str  # 已验证 MIME。
    byte_size: int  # 原始 bytes 长度。
    width: int  # 原始像素宽。
    height: int  # 原始像素高。


@dataclass(frozen=True)
class ImagePayload:
    """仅图片响应或模型调用阶段持有的短生命周期内容。"""
    media_type: str  # 图片输入 MIME。
    data: bytes = field(repr=False)  # 明文原图，不进入消息存储副本或日志。
