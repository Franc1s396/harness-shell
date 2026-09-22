"""真实图像格式和资源准入边界。"""
import io

import pytest
from PIL import Image

from harness_ssh_sidecar.agent.image_models import AttachmentError
from harness_ssh_sidecar.agent.image_validation import validate_image


def picture(format: str, *, animated: bool = False) -> bytes:
    """构造小型真实文件，验证实际 decoder 而非 mock。"""
    with io.BytesIO() as stream, Image.new("RGB", (2, 3), "red") as first:
        with Image.new("RGB", (2, 3), "blue") as second:
            options = {"save_all": True, "append_images": [second], "duration": 100} if animated else {}
            first.save(stream, format=format, **options)
        return stream.getvalue()


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")])
def test_preserves_original_image_without_extension(format, mime):
    raw = picture(format)
    value = validate_image("clipboard", raw)
    assert (value.media_type, value.width, value.height) == (mime, 2, 3)
    assert value.data == raw


@pytest.mark.parametrize("format", ["PNG", "WEBP", "GIF"])
def test_rejects_animation(format):
    with pytest.raises(AttachmentError) as error:
        validate_image("photo", picture(format, animated=True))
    assert error.value.error_code == "AGENT_IMAGE_ANIMATED"


@pytest.mark.parametrize("raw", [b"", b"not an image", picture("PNG")[:30]])
def test_rejects_corrupt_image(raw):
    with pytest.raises(AttachmentError):
        validate_image("photo.png", raw)


def test_rejects_oversized_bytes_before_decoding():
    with pytest.raises(AttachmentError) as error:
        validate_image("photo", b"x" * 10_485_761)
    assert error.value.error_code == "AGENT_IMAGE_TOO_LARGE"


@pytest.mark.parametrize("filename", ["../photo.png", "a\\b.png", "a\x00.png", "x" * 256])
def test_rejects_path_or_invalid_display_name(filename):
    with pytest.raises(AttachmentError):
        validate_image(filename, picture("PNG"))


def test_rejects_unsupported_real_format():
    with pytest.raises(AttachmentError) as error:
        validate_image("photo.png", picture("BMP"))
    assert error.value.error_code == "AGENT_IMAGE_FORMAT_UNSUPPORTED"


@pytest.mark.parametrize('pixels,accepted', [(40_000_000, True), (40_000_001, False)])
def test_pixel_boundary_before_full_decode(monkeypatch, pixels, accepted):
    """超大 header 用受控 decoder 验证准入，不在测试中分配 160MB 像素。"""
    class HeaderImage:
        """只模拟 header 与 load；正常完整解码由真实小图测试覆盖。"""
        format = 'PNG'
        size = (pixels, 1)
        n_frames = 1
        def __enter__(self):
            """匹配 Pillow 的受控上下文。"""
            return self
        def __exit__(self, *args):
            """该 fake 不持有真实资源。"""
        def verify(self):
            """模拟完整 header 校验。"""
        def load(self):
            """只允许已通过像素边界的输入继续。"""
            assert accepted
    monkeypatch.setattr(Image, 'open', lambda stream: HeaderImage())
    if accepted:
        assert validate_image('image', b'header').width == pixels
    else:
        with pytest.raises(AttachmentError, match='pixel'):
            validate_image('image', b'header')


def test_exact_byte_limit_is_accepted_without_transcoding():
    raw = picture('PNG')
    raw += b'\0' * (10_485_760 - len(raw))
    assert validate_image('image', raw).data == raw
