"""光合云文生/首帧图生视频：持久化任务、轮询状态和鉴权下载。"""

import asyncio
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from interfaces.video_output import VideoOutput
from utils.ghyai import GhyAIClient, GhyAIError


def _seconds(name: str, default: float) -> float:
    try:
        return max(0.01, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


class VideoGeneratorGhyAIAPI:
    def __init__(self, api_key: str, model: str = "doubao-seedance-2.0", base_url: str = "https://ghy-ai.com/v1"):
        self.model = model
        self.client = GhyAIClient(api_key, base_url, timeout=_seconds("VIMAX_VIDEO_REQUEST_TIMEOUT_SECONDS", 60))
        self._identity = hashlib.sha256(api_key.encode()).hexdigest()
        self._locks = {}

    async def generate_single_video(self, prompt: str = "", reference_image_paths: list[str] | None = None,
                                    aspect_ratio: str = "16:9", **kwargs) -> VideoOutput:
        cap = await self.client.capability(self.model, "videos")
        self.client.validate_prompt(prompt, cap)
        try:
            duration = int(kwargs.get("duration", os.environ.get("VIMAX_GHYAI_VIDEO_SECONDS", "4")))
        except (ValueError, TypeError):
            raise self.client.error("视频时长必须为整数秒", param="seconds") from None
        maximum = min(60, cap.get("limits", {}).get("max_duration_seconds", 60))
        if not 1 <= duration <= maximum:
            raise self.client.error(f"视频时长须在 1–{maximum} 秒之间", param="seconds")
        sizes = cap.get("sizes", [])
        size = kwargs.get("size") or os.environ.get("VIMAX_GHYAI_VIDEO_SIZE")
        if not size:
            try:
                w, h = map(float, aspect_ratio.split(":"))
                size = min(sizes, key=lambda s: abs(int(s.split("x")[0]) / int(s.split("x")[1]) - w / h))
            except (ValueError, ZeroDivisionError):
                raise self.client.error("视频宽高比或模型尺寸无效", param="size") from None
        if size not in sizes:
            raise self.client.error(f"模型允许的视频尺寸：{', '.join(sizes)}", param="size")
        progress = kwargs.get("progress")
        reference = None
        if reference_image_paths:
            reference = self._read_first_frame(reference_image_paths[0], cap)
            if progress:
                progress("video_reference_ready", "光合云将使用第一张参考图作为首帧生成视频", {"model": self.model, "input_bytes": len(reference[1]), "mime_type": reference[2]})
            if len(reference_image_paths) > 1:
                message = f"光合云标准接口只支持单张首帧，尾帧不会参与生成；忽略其余 {len(reference_image_paths) - 1} 张参考图"
                logging.warning(message)
                if progress:
                    progress("video_last_frame_ignored", message, {"model": self.model, "ignored_frame_count": len(reference_image_paths) - 1})
        payload = {"model": self.model, "prompt": prompt, "seconds": str(duration), "size": size}
        state_path = Path(kwargs.get("request_state_path") or f".working_dir/.ghyai/video_tasks/{uuid4().hex}.json")
        lock = self._locks.setdefault(str(state_path.resolve()), asyncio.Lock())
        async with lock:
            return await self._generate(payload, state_path, progress, reference=reference)

    def _read_first_frame(self, path, cap):
        if "image" not in cap.get("input_modalities", []):
            raise self.client.error("当前模型未开放图生视频，不能提交参考图", param="input_reference")
        maximum = min(200 * 1024 * 1024, cap.get("limits", {}).get("max_input_bytes", 200 * 1024 * 1024))
        try:
            # 保存本次上传的字节快照，避免重试期间文件变化而复用旧幂等键。
            with Path(path).open("rb") as handle:
                content = handle.read(maximum + 1)
        except OSError:
            raise self.client.error("无法读取首帧，请提供可访问的本地图片文件", param="input_reference") from None
        if len(content) > maximum:
            raise self.client.error(f"首帧图片不能超过 {maximum} 字节", param="input_reference")
        try:
            with Image.open(BytesIO(content)) as picture:
                fmt = (picture.format or "").lower()
                if fmt not in {"jpeg", "png", "webp"} or fmt not in cap.get("input_formats", []):
                    raise self.client.error("首帧实际图片格式不在模型声明的 input_formats 内", param="input_reference")
                if getattr(picture, "n_frames", 1) != 1:
                    raise self.client.error("首帧必须是单帧静态图片", param="input_reference")
                picture.verify()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            raise self.client.error("首帧不是有效的静态图片", param="input_reference") from None
        extension = "jpg" if fmt == "jpeg" else fmt
        return (f"first_frame.{extension}", content, f"image/{fmt}")

    async def _generate(self, payload, state_path, progress, *, reference=None):
        intent = {"payload": payload, "base_url": self.client.base_url, "identity": self._identity}
        if reference:
            intent["input_reference"] = {"sha256": hashlib.sha256(reference[1]).hexdigest(), "mime_type": reference[2]}
        # 无参考图时保持旧指纹格式，以便恢复升级前的文生视频任务。
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise self.client.error(f"无法读取视频任务记录：{state_path}；请先检查已有任务") from None
            if state.get("fingerprint") != fingerprint:
                raise self.client.error(f"视频参数、首帧或 Key 已变化，请确认旧任务后移走任务记录再生成：{state_path}")
        else:
            state = {"fingerprint": fingerprint, "idempotency_key": str(uuid4())}
            self._save(state_path, state)
        if not state.get("video_id"):
            if progress:
                mode = "首帧图生视频" if reference else "文生视频"
                progress("video_create", f"正在创建光合云{mode}任务", {"model": self.model, "seconds": payload["seconds"], "size": payload["size"], "reference_count": 1 if reference else 0})
            # 全部字段都属于 multipart；(None, value) 确保无文件时也不退回表单编码。
            files = [(k, (None, v)) for k, v in payload.items()]
            if reference:
                files.append(("input_reference", reference))
            body = await self.client.request("POST", "/videos", files=files, idempotency_key=state["idempotency_key"])
            video_id = body.get("id", "")
            self._validate_id(video_id)
            state["video_id"] = video_id
            state["status"] = body.get("status")
            self._save(state_path, state)
        else:
            video_id = state["video_id"]
            self._validate_id(video_id)
        if progress:
            progress("video_task_created", "光合云任务已保存，可恢复查询", {"model": self.model, "job_id": video_id, "state_path": str(state_path)})
        return await self.resume_video(video_id, progress=progress, state_path=state_path, state=state)

    async def resume_video(self, video_id: str, *, progress=None, state_path=None, state=None) -> VideoOutput:
        self._validate_id(video_id)
        timeout = _seconds("VIMAX_VIDEO_QUERY_TIMEOUT_SECONDS", 600)
        interval = _seconds("VIMAX_VIDEO_POLL_INTERVAL_SECONDS", 5)

        async def poll():
            while True:
                body = await self.client.request("GET", f"/videos/{video_id}")
                if body.get("id") != video_id:
                    raise self.client.error("查询响应中的视频 ID 不匹配")
                status = body.get("status")
                if state_path and state is not None:
                    state["status"] = status
                    self._save(Path(state_path), state)
                if progress:
                    label = {"queued": "排队中", "in_progress": "生成中", "completed": "已完成", "failed": "失败"}.get(status, str(status))
                    progress("video_status", f"光合云视频任务状态：{label}", {"job_id": video_id, "status": status, "progress": body.get("progress")})
                if status == "completed":
                    try:
                        data = await self.client.request("GET", f"/videos/{video_id}/content", binary=True)
                    except GhyAIError as exc:
                        if exc.code == "video_not_ready":
                            await asyncio.sleep(interval)
                            continue
                        raise
                    if progress:
                        progress("video_completed", "光合云视频已完成并下载", {"job_id": video_id})
                    return VideoOutput(fmt="bytes", ext="mp4", data=data)
                if status == "failed":
                    error = body.get("error") or {}
                    if not isinstance(error, dict):
                        raise self.client.error(f"视频任务 {video_id} 的错误详情格式无效")
                    raise self.client.error(f"视频任务 {video_id} 失败：{error.get('message', '未提供详情')}", code=error.get("code") or "video_failed", request_id=self.client.last_request_id)
                if status not in {"queued", "in_progress"}:
                    raise self.client.error(f"视频任务 {video_id} 返回未知状态：{status}")
                await asyncio.sleep(interval)
        try:
            return await asyncio.wait_for(poll(), timeout=timeout)
        except asyncio.TimeoutError:
            raise self.client.error(f"视频任务 {video_id} 在 {timeout:g} 秒内未完成；任务仍可能运行，请恢复查询，勿重新创建", code="poll_timeout") from None

    def _validate_id(self, video_id):
        if not isinstance(video_id, str) or not re.fullmatch(r"video_[0-9a-f]{32}", video_id):
            raise self.client.error("标准视频响应缺少有效 id（不能使用平台 task_no）")

    @staticmethod
    def _save(path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
