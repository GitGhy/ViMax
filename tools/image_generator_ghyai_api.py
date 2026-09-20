"""使用光合云标准图片接口生成图片和编辑参考图。"""

import base64
import binascii
from io import BytesIO
from pathlib import Path
import re

from PIL import Image, ImageOps, UnidentifiedImageError

from interfaces.image_output import ImageOutput
from utils.ghyai import GhyAIClient


class ImageGeneratorGhyAIAPI:
    def __init__(self, api_key: str, model: str = "doubao-seedream-5.0-lite", base_url: str = "https://ghy-ai.com/v1"):
        self.model = model
        self.client = GhyAIClient(api_key, base_url)

    async def generate_single_image(self, prompt: str, reference_image_paths: list[str] | None = None, **kwargs) -> ImageOutput:
        references = reference_image_paths or []
        operation = "images.edits" if references else "images.generations"
        cap = await self.client.capability(self.model, operation)
        self.client.validate_prompt(prompt, cap)
        if "png" not in cap.get("output_formats", []):
            raise self.client.error("模型未开放 PNG 输出", param="output_format")
        sizes = [s for s in cap.get("sizes", []) if re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", s)]
        if not sizes:
            raise self.client.error("模型没有与标准图片协议兼容的明确尺寸", param="size")
        requested_size = kwargs.get("size")
        target = None
        if requested_size:
            if not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", requested_size):
                raise self.client.error("size 应为像素宽x高", param="size")
            target = tuple(map(int, requested_size.split("x")))
            if max(target) > 16384 or target[0] * target[1] > 100_000_000:
                raise self.client.error("目标图片尺寸过大", param="size")
        size = requested_size if requested_size in sizes else sizes[0]
        payload = {"model": self.model, "prompt": prompt, "size": size, "n": 1,
                   "output_format": "png", "response_format": "b64_json"}
        progress = kwargs.get("progress")
        if progress:
            progress("image_generation", "正在使用光合云生成图片", {"model": self.model, "size": size, "target_size": requested_size})
        # 当前服务携带可选幂等头时返回 503；默认单次提交，失败后不自动重发。
        idem = kwargs.get("idempotency_key")
        if references:
            if "image" not in cap.get("input_modalities", []) or "png" not in cap.get("input_formats", []):
                raise self.client.error("模型未开放 PNG 参考图编辑", param="image")
            maximum = cap.get("limits", {}).get("max_input_images", 1)
            if len(references) > maximum:
                raise self.client.error(f"参考图最多 {maximum} 张，当前 {len(references)} 张", param="image")
            files = self._reference_files(references, cap)
            body = await self.client.request("POST", "/images/edits", data={k: str(v) for k, v in payload.items()},
                                             files=files, idempotency_key=idem)
        else:
            body = await self.client.request("POST", "/images/generations", json=payload, idempotency_key=idem)
        try:
            raw = base64.b64decode(body["data"][0]["b64_json"], validate=True)
            with Image.open(BytesIO(raw)) as decoded:
                if decoded.format != "PNG" or getattr(decoded, "n_frames", 1) != 1:
                    raise ValueError("返回图片与 output_format=png 不一致")
                decoded.load()
                output = decoded.copy()
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error, UnidentifiedImageError, OSError) as exc:
            raise self.client.error(f"图片响应无效（{type(exc).__name__}）", request_id=self.client.last_request_id) from None
        # 能力目录未开放任意宽高时，在本地完成调用方要求的中心裁剪。
        if target and output.size != target:
            output = ImageOps.fit(output, target, method=Image.Resampling.LANCZOS)
        if progress:
            progress("image_completed", "光合云图片生成完成", {"model": self.model, "size": list(output.size)})
        return ImageOutput(fmt="pil", ext="png", data=output)

    def _reference_files(self, paths, cap):
        files = []
        canvas = None
        total = 0
        for index, path in enumerate(paths):
            if Path(path).stat().st_size > 25 * 1024 * 1024:
                raise self.client.error("参考图原文件超过 25 MiB", param="image")
            with Image.open(path) as source:
                if (source.format or "").lower() not in cap.get("input_formats", []) or getattr(source, "n_frames", 1) != 1:
                    raise self.client.error("参考图格式不受支持或包含多帧", param="image")
                if max(source.size) > 16384 or source.width * source.height > 100_000_000:
                    raise self.client.error("参考图像素超过协议限制", param="image")
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                normalized.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                canvas = canvas or normalized.size
                normalized = ImageOps.pad(normalized, canvas, method=Image.Resampling.LANCZOS, color="white")
                buffer = BytesIO()
                normalized.save(buffer, format="PNG")
            content = buffer.getvalue()
            total += len(content)
            if len(content) > 25 * 1024 * 1024 or total > min(63 * 1024 * 1024, cap.get("limits", {}).get("max_input_bytes", 63 * 1024 * 1024)):
                raise self.client.error("参考图总大小超过模型或上传协议限制", param="image")
            files.append(("image", (f"reference_{index}.png", content, "image/png")))
        return files
