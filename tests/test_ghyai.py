"""光合云协议、幂等边界和任务恢复的离线回归测试。"""

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from utils.ghyai import GhyAIClient, GhyAIError, normalize_base_url


def capability(operation, **fields):
    return {"operation": operation, "limits": {}, "sizes": [], "input_modalities": ["text"],
            "output_formats": ["text"], "features": {}, **fields}


def client_for(handler, *capabilities):
    client = GhyAIClient("test-secret", transport=httpx.MockTransport(handler))
    client._catalog = {"model": list(capabilities)}
    return client


def test_base_url_accepts_root_sdk_and_previous_image_endpoint():
    for url in ["https://ghy-ai.com", "https://ghy-ai.com/v1/", "https://ghy-ai.com/v1/images/generations"]:
        assert normalize_base_url(url) == "https://ghy-ai.com/v1"


def test_discovery_intersects_authorized_models_and_unwraps_platform_envelope():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        return httpx.Response(200, json={"code": 200, "success": True, "data": {"items": [
            {"id": "model", "capabilities": [capability("chat.completions")]},
            {"id": "hidden", "capabilities": [capability("chat.completions")]},
        ]}})

    async def run():
        client = GhyAIClient("test-secret", transport=httpx.MockTransport(handler))
        assert set(await client.discover()) == {"model"}
        await client.discover()
    asyncio.run(run())
    assert sorted(seen) == ["/v1/models", "/v1/platform/models/capabilities"]


@pytest.mark.parametrize("status,code", [(400, "unsupported_parameter"), (401, "invalid_api_key"),
    (403, "insufficient_permissions"), (429, "insufficient_quota"), (409, "idempotency_conflict")])
def test_terminal_errors_are_not_retried_and_keep_safe_diagnostics(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"X-Request-ID": "req_test"}, json={"error": {
            "code": code, "message": "test-secret 参数错误", "param": "size"}})
    client = client_for(handler)
    with pytest.raises(GhyAIError) as caught:
        asyncio.run(client.request("POST", "/images/generations", json={}, idempotency_key="same-intent"))
    assert len(calls) == 1
    assert "req_test" in str(caught.value) and "size" in str(caught.value)
    assert "test-secret" not in str(caught.value)
    assert caught.value.code == code


def test_retry_after_and_idempotency_key_are_preserved():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(409, headers={"Retry-After": "2"}, json={"error": {"code": "idempotency_in_progress"}})
        return httpx.Response(200, json={"data": []})
    client = client_for(handler)
    with patch("utils.ghyai.asyncio.sleep", new_callable=AsyncMock) as sleep:
        asyncio.run(client.request("POST", "/images/generations", json={"model": "model"}, idempotency_key="same-intent"))
    sleep.assert_awaited_once_with(2.0)
    assert [r.headers["Idempotency-Key"] for r in calls] == ["same-intent"] * 2
    assert calls[0].content == calls[1].content


def test_chat_timeout_is_not_replayed():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("模拟读取超时")
    client = client_for(handler)
    with pytest.raises(GhyAIError):
        asyncio.run(client.request("POST", "/chat/completions", json={}))
    assert len(calls) == 1


def test_quota_error_type_without_code_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"type": "insufficient_quota", "code": None, "message": "额度不足"}})
    with pytest.raises(GhyAIError) as caught:
        asyncio.run(client_for(handler).request("POST", "/images/generations", json={}, idempotency_key="quota-check"))
    assert caught.value.error_type == "insufficient_quota"
    assert len(calls) == 1


def test_chat_dispatcher_preserves_all_tools_and_conversation_ids():
    captured = []
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "vimax_dispatch", "arguments": json.dumps({"name": "tool_11", "arguments": {"path": "x"}})}}]}}]})
    tools = [{"type": "function", "function": {"name": f"tool_{i}", "description": "测试工具", "parameters": {"type": "object"}}} for i in range(12)]
    client = client_for(handler, capability("chat.completions", limits={"max_tools": 10, "max_output_tokens": 100}))
    async def run():
        response = await client.chat("model", [{"role": "user", "content": "测试"}], tools=tools, max_tokens=4096)
        message = response["choices"][0]["message"]
        assert message["tool_calls"][0]["function"]["name"] == "tool_11"
        await client.chat("model", [{"role": "assistant", **message}, {"role": "tool", "tool_call_id": "call_1", "content": "完成"}], tools=tools)
    asyncio.run(run())
    assert len(captured[0]["tools"]) == 1
    assert len(captured[0]["tools"][0]["function"]["parameters"]["oneOf"]) == 12
    assert captured[0]["max_completion_tokens"] == 100
    assert captured[1]["messages"][0]["tool_calls"][0]["function"]["name"] == "vimax_dispatch"
    assert captured[1]["messages"][1]["tool_call_id"] == "call_1"


def test_json_tool_mode_uses_documented_response_format_and_preserves_history():
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        content = json.dumps({"content": "正在读取", "tool_calls": [{"name": "read_file", "arguments": {"path": "readme.md"}}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
    client = client_for(handler, capability("chat.completions", limits={"max_tools": 1}, output_formats=["text", "json_object"]))
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    async def run():
        body = await client.chat("model", [{"role": "user", "content": "读取说明"}], tools=tools, tool_mode="json")
        msg = body["choices"][0]["message"]
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"
        call_id = msg["tool_calls"][0]["id"]
        await client.chat("model", [{"role": "assistant", **msg}, {"role": "tool", "tool_call_id": call_id, "content": "说明内容"}], tools=tools, tool_mode="json")
    asyncio.run(run())
    for request in captured:
        assert request["response_format"] == {"type": "json_object"}
        assert "tools" not in request and "tool_choice" not in request
    assert captured[1]["messages"][-1]["role"] == "user"
    assert "工具执行结果" in captured[1]["messages"][-1]["content"]


def test_json_tool_mode_rejects_unknown_tool_without_retry():
    client = client_for(lambda _: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"content": "", "tool_calls": [{"name": "unknown", "arguments": {}}]})}}]}),
                        capability("chat.completions", output_formats=["text", "json_object"]))
    with pytest.raises(GhyAIError, match="工具名称"):
        asyncio.run(client.chat("model", [{"role": "user", "content": "读取"}], tools=[{"type": "function", "function": {"name": "read_file"}}], tool_mode="json"))


@pytest.mark.parametrize("tool_calls", [[], [{"name": "vimax_render_video", "arguments": {
    "session_id": "20260920-185927-vimax", "force": True,
}}]])
def test_json_tool_mode_accepts_single_wrapped_envelope_without_resubmission(tool_calls):
    requests = []
    envelope = {"content": "正在重新进入渲染阶段，生成最终视频。", "tool_calls": tool_calls}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"X-Request-ID": "req_wrapped"}, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps([envelope], ensure_ascii=False)},
        }]})

    client = client_for(handler, capability("chat.completions", output_formats=["text", "json_object"]))
    response = asyncio.run(client.chat("model", [{"role": "user", "content": "重新进入渲染阶段生成视频"}],
        tools=[{"type": "function", "function": {"name": "vimax_render_video"}}], tool_mode="json"))
    message = response["choices"][0]["message"]
    assert message["content"] == envelope["content"]
    assert [{"name": call["function"]["name"], "arguments": json.loads(call["function"]["arguments"])}
            for call in message["tool_calls"]] == tool_calls
    assert all(call["id"] for call in message["tool_calls"])
    assert client.last_request_id == "req_wrapped"
    assert len(requests) == 1


@pytest.mark.parametrize("envelope", [
    [],
    [{"content": "", "tool_calls": []}, {"content": "", "tool_calls": []}],
    [[{"content": "", "tool_calls": []}]],
    [None],
    [{"content": "不要输出这段正文"}],
    [{"content": "不要输出这段正文", "tool_calls": [{"name": "read_file", "arguments": "{}"}]}],
    [{"content": "不要输出这段正文", "tool_calls": [{"name": "unknown", "arguments": {}}]}],
])
def test_invalid_json_tool_envelope_reports_received_response_without_retry(envelope):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"X-Request-ID": "req_invalid_envelope"}, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(envelope, ensure_ascii=False)},
        }]})

    client = client_for(handler, capability("chat.completions", output_formats=["text", "json_object"]))
    with pytest.raises(GhyAIError) as caught:
        asyncio.run(client.chat("model", [{"role": "user", "content": "读取"}],
            tools=[{"type": "function", "function": {"name": "read_file"}}], tool_mode="json"))
    assert caught.value.status_code == 200
    assert caught.value.code == "protocol_error"
    assert caught.value.request_id == "req_invalid_envelope"
    assert "响应校验失败" in str(caught.value) and "HTTP 200" in str(caught.value)
    assert "本地校验" not in str(caught.value) and "不要输出这段正文" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("reason", ["length", "content_filter", None])
def test_chat_does_not_parse_incomplete_or_filtered_responses(reason):
    client = client_for(lambda _: httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": "{不完整"}}]}), capability("chat.completions"))
    with pytest.raises(GhyAIError):
        asyncio.run(client.chat("model", [{"role": "user", "content": "测试"}]))


def test_truncated_response_reports_actual_token_limit_and_usage_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"X-Request-ID": "req_truncated"}, json={
            "choices": [{"finish_reason": "length", "message": {"content": '{"script":["未完成'}}],
            "usage": {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 512}},
        })
    client = client_for(handler, capability("chat.completions", limits={"max_output_tokens": 8192}))
    with pytest.raises(GhyAIError) as caught:
        asyncio.run(client.chat("model", [{"role": "user", "content": "生成脚本"}], max_tokens=16384))
    assert caught.value.code == "output_length_exceeded"
    assert caught.value.param == "max_completion_tokens"
    assert "8192" in str(caught.value) and "512" in str(caught.value)
    assert "req_truncated" in str(caught.value)
    assert '"script"' not in str(caught.value)
    assert len(calls) == 1


def test_ghyai_narrative_budget_preserves_override_and_other_providers():
    from agent_runtime.vimax_adapters import _narrative_max_tokens
    from utils.ghyai_chat_model import GhyAIChatModel
    with patch.dict("os.environ", {}, clear=True), patch("agent_runtime.vimax_adapters.llm_base_url", return_value="https://ghy-ai.com/v1"):
        assert _narrative_max_tokens() == 16384
        with patch.dict("os.environ", {"VIMAX_NARRATIVE_MAX_TOKENS": "24000"}):
            assert _narrative_max_tokens() == 24000
        with patch.dict("os.environ", {"VIMAX_NARRATIVE_MAX_TOKENS": "invalid"}):
            assert _narrative_max_tokens() == 16384
    with patch.dict("os.environ", {}, clear=True), patch("agent_runtime.vimax_adapters.llm_base_url", return_value="https://example.invalid/v1"):
        assert _narrative_max_tokens() == 4096
    assert GhyAIChatModel(model="model", api_key="test-secret").max_tokens == 16384


def png_bytes(size=(32, 32)):
    buffer = BytesIO()
    Image.new("RGB", size, "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def image_cap(operation):
    return capability(operation, limits={"max_prompt_characters": 4000, "max_input_images": 14, "max_input_bytes": 30_000_000},
                      sizes=["2048x2048", "3072x3072", "2K", "3K"], input_formats=["png", "jpeg", "webp"],
                      input_modalities=["text", "image"], output_formats=["png", "jpeg"])


def test_image_generation_uses_supported_dimensions_and_decodes_png():
    from tools.image_generator_ghyai_api import ImageGeneratorGhyAIAPI
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})
    generator = ImageGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, image_cap("images.generations"))
    result = asyncio.run(generator.generate_single_image("一只纸船", size="1600x900"))
    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/images/generations"
    assert "Idempotency-Key" not in captured[0].headers
    assert payload["size"] == "2048x2048" and payload["response_format"] == "b64_json"
    assert "aspect_ratio" not in payload and "output_compression" not in payload
    assert result.data.size == (1600, 900)


def test_image_edit_is_multipart_with_repeated_identical_format_and_dimensions(tmp_path):
    from tools.image_generator_ghyai_api import ImageGeneratorGhyAIAPI
    from email.parser import BytesParser
    from email.policy import default
    images = []
    def handler(request):
        assert request.url.path == "/v1/images/edits"
        assert "Idempotency-Key" not in request.headers
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        message = BytesParser(policy=default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "image":
                image = Image.open(BytesIO(part.get_payload(decode=True)))
                images.append((image.format, image.size))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})
    one, two = tmp_path / "a.png", tmp_path / "b.jpg"
    one.write_bytes(png_bytes((40, 20)))
    Image.new("RGB", (20, 40)).save(two)
    generator = ImageGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, image_cap("images.edits"))
    asyncio.run(generator.generate_single_image("编辑图片", [str(one), str(two)]))
    assert images == [("PNG", (40, 20)), ("PNG", (40, 20))]


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_image_failure_is_not_automatically_resubmitted(tmp_path, reference, failure):
    from tools.image_generator_ghyai_api import ImageGeneratorGhyAIAPI
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("响应丢失，生成结果未知")
        return httpx.Response(503, headers={"X-Request-ID": "req_image_failure"},
                              json={"error": {"code": "service_unavailable", "message": "图片服务异常"}})

    path = tmp_path / "reference.png"
    path.write_bytes(png_bytes())
    generator = ImageGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, image_cap("images.edits" if reference else "images.generations"))
    with patch("utils.ghyai.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(GhyAIError) as caught:
            asyncio.run(generator.generate_single_image("纸船", [str(path)] if reference else []))
    assert len(requests) == 1
    assert caught.value.retryable is False
    if failure == "http":
        assert caught.value.request_id == "req_image_failure"


def test_image_limits_fail_before_paid_request():
    from tools.image_generator_ghyai_api import ImageGeneratorGhyAIAPI
    generator = ImageGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(lambda _: pytest.fail("不能提交付费请求"), image_cap("images.edits"))
    with pytest.raises(GhyAIError, match="14"):
        asyncio.run(generator.generate_single_image("编辑", ["unused"] * 15))
    with pytest.raises(GhyAIError, match="4000"):
        asyncio.run(generator.generate_single_image("字" * 4001, ["unused"]))


VIDEO_ID = "video_" + "a" * 32


def video_cap():
    return capability("videos", limits={"max_prompt_characters": 4000, "max_duration_seconds": 15, "max_input_bytes": 31457280},
                      input_modalities=["text", "image"], input_formats=["jpeg", "png", "webp"],
                      sizes=["864x496", "640x640"], output_formats=["mp4"])


def test_video_creation_polling_download_and_resume_never_recreate(tmp_path):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    requests = []
    statuses = iter(["queued", "in_progress", "completed", "completed"])
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.method == "POST":
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert b'name="seconds"\r\n\r\n4' in request.content
            assert b"input_reference" not in request.content
            return httpx.Response(200, json={"id": VIDEO_ID, "status": "queued"})
        if request.url.path.endswith("/content"):
            assert not request.url.query
            return httpx.Response(200, content=b"mp4-data", headers={"Content-Type": "video/mp4"})
        return httpx.Response(200, json={"id": VIDEO_ID, "status": next(statuses), "error": None})
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, video_cap())
    events = []
    state = tmp_path / "video.ghyai.json"
    with patch("tools.video_generator_ghyai_api.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(generator.generate_single_video("一只纸船", request_state_path=state,
                            progress=lambda stage, message, metadata: events.append((stage, message))))
        assert result.data == b"mp4-data"
        asyncio.run(generator.generate_single_video("一只纸船", request_state_path=state))
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert any("文生视频" in message for _, message in events)
    assert json.loads(state.read_text())["video_id"] == VIDEO_ID
    assert "test-secret" not in state.read_text()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_image_to_video_uploads_first_frame_with_actual_mime_and_resumes(tmp_path, fmt):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    from email.parser import BytesParser
    from email.policy import default
    reference = tmp_path / "incorrect-extension.txt"
    Image.new("RGB", (64, 36), "blue").save(reference, format=fmt)
    original = reference.read_bytes()
    posts, events = [], []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            form = BytesParser(policy=default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            parts = [p for p in form.iter_parts() if p.get_param("name", header="content-disposition") == "input_reference"]
            assert len(parts) == 1
            assert parts[0].get_content_type() == "image/" + fmt.lower()
            assert parts[0].get_payload(decode=True) == original
            return httpx.Response(200, json={"id": VIDEO_ID, "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"mp4-data", headers={"Content-Type": "video/mp4"})
        return httpx.Response(200, json={"id": VIDEO_ID, "status": "completed", "error": None})
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, video_cap())
    state = tmp_path / "image-video.json"
    for _ in range(2):
        asyncio.run(generator.generate_single_video("纸船漂浮", [str(reference), "unused-last-frame.png"], request_state_path=state,
                    progress=lambda stage, message, metadata: events.append((stage, message, metadata))))
    assert len(posts) == 1
    assert any("尾帧不会参与" in message for _, message, _ in events)
    assert any("图生视频" in message for _, message, _ in events)
    Image.new("RGB", (64, 36), "red").save(reference, format=fmt)
    with pytest.raises(GhyAIError, match="变化"):
        asyncio.run(generator.generate_single_video("纸船漂浮", [str(reference)], request_state_path=state))
    assert len(posts) == 1


@pytest.mark.parametrize("scenario", ["unsupported", "size", "format", "invalid", "missing", "animated"])
def test_invalid_video_reference_fails_before_creating_task(tmp_path, scenario):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    path = tmp_path / "input.png"
    path.write_bytes(png_bytes())
    cap = video_cap()
    if scenario == "unsupported":
        cap["input_modalities"] = ["text"]
    elif scenario == "size":
        cap["limits"]["max_input_bytes"] = 1
    elif scenario == "format":
        cap["input_formats"] = ["jpeg"]
    elif scenario == "invalid":
        path.write_bytes(b"not-an-image")
    elif scenario == "missing":
        path.unlink()
    elif scenario == "animated":
        Image.new("RGB", (16, 16), "red").save(path, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (16, 16), "blue")], duration=100, loop=0)
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(lambda _: pytest.fail("无效参考图不能创建任务"), cap)
    state = tmp_path / "task.json"
    with pytest.raises(GhyAIError):
        asyncio.run(generator.generate_single_video("测试", [str(path)], request_state_path=state))
    assert not state.exists()


def test_video_reference_bytes_and_idempotency_key_stay_stable_during_retry(tmp_path):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    path = tmp_path / "frame.png"
    original = png_bytes()
    path.write_bytes(original)
    requests = []
    def handler(request):
        if request.method == "POST":
            requests.append(request)
            assert original in request.content
            if len(requests) == 1:
                Image.new("RGB", (32, 32), "red").save(path)
                return httpx.Response(503, json={"error": {"code": "service_unavailable"}})
            return httpx.Response(200, json={"id": VIDEO_ID, "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"mp4-data", headers={"Content-Type": "video/mp4"})
        return httpx.Response(200, json={"id": VIDEO_ID, "status": "completed"})
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, video_cap())
    with patch("utils.ghyai.asyncio.sleep", new_callable=AsyncMock):
        asyncio.run(generator.generate_single_video("测试", [str(path)], request_state_path=tmp_path / "task.json"))
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]


@pytest.mark.parametrize("status", ["failed", "SUCCESS", "canceled", "unknown"])
def test_standard_video_terminal_and_unknown_states_fail_without_download(status):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"id": VIDEO_ID, "status": status, "error": {"code": "render_failed", "message": "测试失败"}})
    generator = VideoGeneratorGhyAIAPI("test-secret")
    generator.client = client_for(handler)
    with pytest.raises(GhyAIError):
        asyncio.run(generator.resume_video(VIDEO_ID))
    assert paths == [f"/v1/videos/{VIDEO_ID}"]


def test_video_timeout_keeps_task_id_and_resumes_without_post(tmp_path):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"id": VIDEO_ID, "status": "queued"})
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(handler, video_cap())
    state = tmp_path / "task.json"
    with patch.dict("os.environ", {"VIMAX_VIDEO_QUERY_TIMEOUT_SECONDS": "0.02", "VIMAX_VIDEO_POLL_INTERVAL_SECONDS": "0.1"}):
        for _ in range(2):
            with pytest.raises(GhyAIError, match=VIDEO_ID):
                asyncio.run(generator.generate_single_video("测试", request_state_path=state))
    assert calls.count("POST") == 1


def test_video_parameter_limit_and_changed_request_fail_before_create(tmp_path):
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    generator = VideoGeneratorGhyAIAPI("test-secret", model="model")
    generator.client = client_for(lambda _: pytest.fail("不能提交请求"), video_cap())
    with pytest.raises(GhyAIError, match="15"):
        asyncio.run(generator.generate_single_video("测试", duration=16))
    state = tmp_path / "task.json"
    state.write_text('{"fingerprint": "previous", "video_id": "previous"}')
    with pytest.raises(GhyAIError, match="参数、首帧或 Key 已变化"):
        asyncio.run(generator.generate_single_video("新提示词", request_state_path=state))


def test_planning_retry_boundary_does_not_replay_ghyai_errors():
    from agents.screenwriter import Screenwriter
    from types import SimpleNamespace
    call = AsyncMock(side_effect=GhyAIError("额度不足", code="insufficient_quota"))
    writer = Screenwriter(SimpleNamespace(ainvoke=call))
    with pytest.raises(GhyAIError):
        asyncio.run(writer.write_script_based_on_story("一只纸船"))
    assert call.await_count == 1


def test_planning_parse_error_does_not_replay_a_successful_paid_chat():
    from agents.screenwriter import Screenwriter
    from utils.ghyai_chat_model import GhyAIChatModel
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"unexpected": "字段"}'}}]})
    model = GhyAIChatModel(model="model", api_key="test-secret")
    model._client = client_for(handler, capability("chat.completions"))
    with pytest.raises(Exception):
        asyncio.run(Screenwriter(model).write_script_based_on_story("纸船"))
    assert len(calls) == 1


def test_factory_and_langchain_planning_use_ghyai_protocol():
    from agent_runtime.config import api_provider_from_base_url
    from agent_runtime.vimax_adapters import _build_chat_model, _build_image_generator, _build_video_generator
    from utils.ghyai_chat_model import GhyAIChatModel
    from tools.image_generator_ghyai_api import ImageGeneratorGhyAIAPI
    from tools.video_generator_ghyai_api import VideoGeneratorGhyAIAPI
    assert api_provider_from_base_url("https://ghy-ai.com/v1") == "ghyai"
    assert api_provider_from_base_url("https://ghy-ai.com.example/v1") == ""
    env = {f"VIMAX_{kind}_{field}": value for kind in ["LLM", "IMAGE", "VIDEO"] for field, value in
           [("API_KEY", "test-secret"), ("MODEL", "model"), ("BASE_URL", "https://ghy-ai.com/v1")]}
    with patch.dict("os.environ", env):
        model = _build_chat_model()
        assert isinstance(model, GhyAIChatModel)
        assert isinstance(_build_image_generator(), ImageGeneratorGhyAIAPI)
        assert isinstance(_build_video_generator(), VideoGeneratorGhyAIAPI)
    model._client = client_for(lambda _: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{\"script\":[]}"}}]}), capability("chat.completions"))
    assert asyncio.run(model.ainvoke([("human", "生成脚本")])).content == '{"script":[]}'
    assert "test-secret" not in repr(model)


def test_novel_planners_share_ghyai_client():
    from agents.novel_compressor import NovelCompressor
    from agents.event_extractor import EventExtractor
    from agents.scene_extractor import SceneExtractor
    from agents.global_information_planner import GlobalInformationPlanner
    from utils.ghyai_chat_model import GhyAIChatModel
    for cls in [NovelCompressor, EventExtractor, SceneExtractor, GlobalInformationPlanner]:
        assert isinstance(cls(api_key="test-secret", base_url="https://ghy-ai.com/v1", chat_model="model").chat_model, GhyAIChatModel)


def test_novel_sync_aggregation_can_run_inside_async_pipeline():
    from agents.novel_compressor import NovelCompressor
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "合并后的故事"}}]})
    compressor = NovelCompressor(api_key="test-secret", base_url="https://ghy-ai.com/v1", chat_model="model")
    compressor.chat_model._client = client_for(handler, capability("chat.completions"))
    async def run():
        # 与 Novel2MoviePipeline.plan_text_artifacts 的同步聚合调用保持一致。
        assert compressor.aggregate(["第一段", "第二段"]) == "合并后的故事"
    asyncio.run(run())
    assert len(calls) == 1


def test_camera_tree_manual_retry_does_not_replay_ghyai(tmp_path):
    from pipelines.script2video_pipeline import Script2VideoPipeline
    from utils.ghyai_chat_model import GhyAIChatModel
    model = GhyAIChatModel(api_key="test-secret", model="model")
    pipeline = Script2VideoPipeline(chat_model=model, image_generator=object(), video_generator=object(), working_dir=str(tmp_path))
    pipeline.design_storyboard = AsyncMock(return_value=[])
    pipeline.decompose_visual_descriptions = AsyncMock(return_value=[])
    pipeline.construct_camera_tree = AsyncMock(side_effect=GhyAIError("额度不足"))
    with pytest.raises(GhyAIError):
        asyncio.run(pipeline.plan_text_artifacts("测试脚本", "要求", "风格", characters=[]))
    assert pipeline.construct_camera_tree.await_count == 1
