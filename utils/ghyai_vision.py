"""按光合云整份请求体的限制压缩视觉分析副本，不改动原始媒体。"""

import base64
import binascii
from io import BytesIO
import json
import logging
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_CHAT_REQUEST_BYTES = 16 * 1024 * 1024
_JPEG_PREFIX = "data:image/jpeg;base64,"


def chat_payload_size(payload: dict) -> int:
    # 与 httpx 的 JSON 编码一致，中文按 UTF-8 字节计入体积。
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def fit_chat_images(payload: dict) -> None:
    """只调整已复制的待发送消息；保留所有图片、顺序、文本和工具定义。"""
    original_bytes = chat_payload_size(payload)
    if original_bytes <= MAX_CHAT_REQUEST_BYTES:
        return
    images = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            image = part.get("image_url") if part.get("type") == "image_url" else None
            url = image.get("url") if isinstance(image, dict) else None
            if isinstance(url, str) and url.startswith("data:image/"):
                images.append(image)
    if not images:
        raise ValueError("没有可压缩的内嵌图片，请减少文本或工具定义")

    # 先扣除完整文本、工具和 JSON 结构，再均分可用图片空间。
    url_sizes = [len(json.dumps(image["url"], ensure_ascii=False).encode("utf-8")) - 2 for image in images]
    available = MAX_CHAT_REQUEST_BYTES - (original_bytes - sum(url_sizes))
    per_image_bytes = available // len(images)
    raw_budget = (per_image_bytes - len(_JPEG_PREFIX)) // 4 * 3
    if raw_budget < 16 * 1024:
        raise ValueError(f"文本和工具占用过大，剩余空间不足以保留 {len(images)} 张图片")
    for image, url_size in zip(images, url_sizes):
        if url_size > per_image_bytes:
            image["url"] = _compress_image(image["url"], raw_budget)
    final_bytes = chat_payload_size(payload)
    if final_bytes > MAX_CHAT_REQUEST_BYTES:
        raise ValueError("压缩图片后仍超限，请减少单次分析的图片或文本")
    logging.getLogger(__name__).info(
        "光合云视觉请求已压缩：%.2f MiB → %.2f MiB，保留 %d 张图片，原图文件不变",
        original_bytes / 1024**2, final_bytes / 1024**2, len(images),
    )


def _compress_image(url: str, max_bytes: int) -> str:
    try:
        header, encoded = url.split(",", 1)
        if not header.endswith(";base64"):
            raise ValueError("内嵌图片须使用 Base64 编码")
        raw = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("超限图片须为单帧 PNG、JPEG 或 WebP，不能丢弃动画帧")
                source.load()
                with ImageOps.exif_transpose(source) as oriented, oriented.convert("RGBA") as rgba:
                    rendered = Image.new("RGB", rgba.size, "white")
                    with rgba.getchannel("A") as alpha:
                        rendered.paste(rgba, mask=alpha)
    except (binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("内嵌图片无法安全解码，未发送请求") from None

    try:
        rendered.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
        while True:
            for quality in (85, 75, 65):
                with BytesIO() as buffer:
                    rendered.save(buffer, format="JPEG", quality=quality, optimize=True)
                    content = buffer.getvalue()
                if len(content) <= max_bytes:
                    return _JPEG_PREFIX + base64.b64encode(content).decode("ascii")
            longest = max(rendered.size)
            if longest <= 256:
                raise ValueError("无法在保留全部图片的条件下压缩到请求限制，请减少单次图片数量")
            dimension = max(256, int(longest * 0.75))
            rendered.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
    finally:
        rendered.close()
