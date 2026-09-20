"""多图视觉请求按完整 JSON 体积适配，保留全部图片和上下文。"""

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json
import random

import httpx
from PIL import Image
import pytest

from utils.ghyai import GhyAIClient, GhyAIError


MAX_REQUEST_BYTES = 16 * 1024 * 1024


def data_url(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture(scope="module")
def large_image_urls():
    pixels = random.Random(42).randbytes(1024 * 1024 * 3)
    with Image.frombytes("RGB", (2048, 512), pixels) as wide:
        wide_url = data_url(wide)
    with Image.frombytes("RGB", (1024, 1024), pixels).convert("RGBA") as transparent:
        transparent.putalpha(0)
        transparent_url = data_url(transparent)
    return [wide_url] * 5 + [transparent_url]


def vision_client(handler):
    client = GhyAIClient("test-secret", transport=httpx.MockTransport(handler))
    client._catalog = {"vision-model": [{
        "operation": "chat.completions", "input_modalities": ["text", "image"],
        "output_formats": ["text"], "limits": {"max_output_tokens": 4096}, "features": {},
    }]}
    return client


def success():
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "已选出参考图"}}]})


def test_large_reference_images_fit_one_request_without_losing_order_or_originals(large_image_urls):
    content = []
    for i, url in enumerate(large_image_urls):
        content.extend([{"type": "text", "text": f"参考图 {i}"},
                        {"type": "image_url", "image_url": {"url": url, "detail": "high"}}])
    messages = [{"role": "system", "content": "选择参考图，保留图像编号。"},
                {"role": "user", "content": content}]
    original = deepcopy(messages)
    assert len(json.dumps(messages).encode()) > MAX_REQUEST_BYTES
    requests = []

    def handler(request):
        requests.append(request)
        assert len(request.content) <= MAX_REQUEST_BYTES
        sent = json.loads(request.content)["messages"]
        assert sent[0] == original[0]
        assert [p["text"] for p in sent[1]["content"] if p["type"] == "text"] == [f"参考图 {i}" for i in range(6)]
        images = [p["image_url"] for p in sent[1]["content"] if p["type"] == "image_url"]
        assert len(images) == 6
        for i, image in enumerate(images):
            assert image["detail"] == "high"
            prefix, encoded = image["url"].split(",", 1)
            assert prefix == "data:image/jpeg;base64"
            with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
                decoded.load()
                assert decoded.format == "JPEG"
                assert max(decoded.size) <= 1536
                if i < 5:
                    assert decoded.width == decoded.height * 4
                else:
                    assert min(decoded.getpixel((0, 0))) >= 250
        return success()

    asyncio.run(vision_client(handler).chat("vision-model", messages))
    assert len(requests) == 1
    assert messages == original


def test_image_budget_includes_text_instead_of_only_counting_images(large_image_urls):
    text = "x" * (13 * 1024 * 1024)
    content = [{"type": "text", "text": text}, *[
        {"type": "image_url", "image_url": {"url": url}} for url in large_image_urls[:2]
    ]]
    requests = []

    def handler(request):
        requests.append(request)
        assert len(request.content) <= MAX_REQUEST_BYTES
        sent = json.loads(request.content)["messages"][0]["content"]
        assert sent[0]["text"] == text
        assert len(sent) == 3
        return success()

    asyncio.run(vision_client(handler).chat("vision-model", [{"role": "user", "content": content}]))
    assert len(requests) == 1


def test_small_images_and_remote_urls_are_preserved():
    with Image.new("RGB", (24, 16), "blue") as image:
        small_url = data_url(image)
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": small_url}},
        {"type": "image_url", "image_url": {"url": "https://example.com/reference.png"}},
        {"type": "text", "text": "比较两张图"},
    ]}]
    seen = []

    def handler(request):
        seen.append(request)
        assert json.loads(request.content)["messages"] == messages
        return success()

    asyncio.run(vision_client(handler).chat("vision-model", messages))
    assert len(seen) == 1


def test_oversized_text_still_fails_before_sending():
    client = vision_client(lambda _: pytest.fail("超限文本不能发送到服务端"))
    with pytest.raises(GhyAIError, match="16 MiB"):
        asyncio.run(client.chat("vision-model", [{"role": "user", "content": "x" * MAX_REQUEST_BYTES}]))


def test_invalid_large_inline_image_is_not_dropped_or_sent():
    client = vision_client(lambda _: pytest.fail("不能发送无效图片或静默删除图片"))
    messages = [{"role": "user", "content": [{
        "type": "image_url", "image_url": {"url": "data:image/png;base64," + "!" * MAX_REQUEST_BYTES},
    }]}]
    with pytest.raises(GhyAIError, match="图片") as caught:
        asyncio.run(client.chat("vision-model", messages))
    assert len(str(caught.value)) < 1000
