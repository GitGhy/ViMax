"""查询光合云能力；显式启用参数后才执行会产生用量的冒烟测试。"""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from agent_runtime.config import llm_api_key, llm_base_url, llm_model
from utils.ghyai import GhyAIClient, GhyAIError


async def check(args):
    client = GhyAIClient(llm_api_key(), llm_base_url())
    catalog = await client.discover()
    print(json.dumps({"可用模型": catalog}, ensure_ascii=False, indent=2), flush=True)
    if args.chat:
        from agent_runtime.session_index import SessionIndex
        from agent_runtime.tools import build_builtin_registry
        from agent_runtime.vimax_adapters import build_vimax_adapter_specs, _build_chat_model
        from agent_runtime.llm import OpenAICompatibleLLM
        with tempfile.TemporaryDirectory() as directory:
            index = SessionIndex(directory)
            registry = build_builtin_registry(directory, index, build_vimax_adapter_specs(directory, index))
            response = await OpenAICompatibleLLM().complete([{"role": "user", "content": "请只调用 todo_read 工具读取待办，不要调用其他工具。"}], tools=registry.list_function_tools())
        calls = [c.name for c in response.tool_calls]
        if "todo_read" not in calls:
            raise GhyAIError("冒烟测试未返回预期的 todo_read 工具调用")
        model = _build_chat_model()
        result = await model.ainvoke([("human", '只返回 JSON 对象 {"ok": true}，不要附加解释。')])
        print(json.dumps({"聊天测试": "通过", "工具": calls, "规划模型响应": result.content}, ensure_ascii=False), flush=True)
    if args.image:
        from agent_runtime.vimax_adapters import _build_image_generator
        generator = _build_image_generator()
        output = await generator.generate_single_image("纯白背景上的一只蓝色折纸船，简洁插画，无文字。", size="1600x900")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / "image.png"
        output.save(str(target))
        edited = await generator.generate_single_image("把参考图片中的蓝色纸船改成绿色，保留原有构图，无文字。", [str(target)], size="1600x900")
        edited.save(str(args.output_dir / "image-edited.png"))
        print(json.dumps({"图片生成与编辑测试": "通过", "输出目录": str(args.output_dir)}, ensure_ascii=False), flush=True)
    if args.video or args.resume_video:
        from agent_runtime.vimax_adapters import _build_video_generator
        generator = _build_video_generator()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        def progress(stage, message, metadata):
            print(json.dumps({"阶段": stage, "信息": message, **metadata}, ensure_ascii=False), flush=True)
        if args.resume_video:
            output = await generator.resume_video(args.resume_video, progress=progress)
        else:
            references = [str(args.video_reference)] if args.video_reference else []
            state_name = "image-video.ghyai.json" if references else "video.ghyai.json"
            output = await generator.generate_single_video("一只蓝色纸船在平静的水面缓缓漂浮，固定镜头，自然光，无人物，无文字。", duration=4,
                reference_image_paths=references, request_state_path=args.output_dir / state_name, progress=progress)
        target = args.output_dir / ("image-video.mp4" if args.video_reference else "video.mp4")
        output.save(str(target))
        print(json.dumps({"视频测试": "通过", "输出": str(target), "字节数": len(output.data)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description="光合云模型能力检查；生成测试会产生 API 用量")
    parser.add_argument("--chat", action="store_true", help="测试完整工具目录和规划模型")
    parser.add_argument("--image", action="store_true", help="生成一张图片并编辑一次")
    parser.add_argument("--video", action="store_true", help="创建或恢复一个 4 秒视频")
    parser.add_argument("--video-reference", type=Path, help="搭配 --video 上传一张本地图片作为首帧")
    parser.add_argument("--resume-video", help="仅恢复指定标准视频 ID 的查询和下载")
    parser.add_argument("--output-dir", type=Path, default=Path(".working_dir/ghyai-check"))
    args = parser.parse_args()
    if args.video_reference and (not args.video or args.resume_video):
        parser.error("--video-reference 必须搭配 --video 使用，且不能与 --resume-video 同时使用")
    try:
        asyncio.run(check(args))
    except GhyAIError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
