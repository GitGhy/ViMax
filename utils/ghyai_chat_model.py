"""将光合云的非流式聊天响应接入现有 LangChain 规划链。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from utils.ghyai import GhyAIClient, is_ghyai_url


# 完整故事扩写为场景脚本时，4096 token 容易截断结构化输出。
DEFAULT_GHYAI_NARRATIVE_MAX_TOKENS = 16384


def init_compatible_chat_model(**kwargs):
    """小说各规划器也复用相同协议边界，其他服务商仍交给 LangChain。"""
    if is_ghyai_url(kwargs.get("base_url", "")):
        options = dict(kwargs)
        options.pop("model_provider", None)
        return GhyAIChatModel(**options)
    from langchain.chat_models import init_chat_model
    return init_chat_model(**kwargs)


class GhyAIChatModel(BaseChatModel):
    model: str
    max_tokens: int = DEFAULT_GHYAI_NARRATIVE_MAX_TOKENS
    _client: GhyAIClient = PrivateAttr()

    def __init__(self, *, api_key: str, base_url: str = "https://ghy-ai.com/v1", timeout: float = 300, **kwargs):
        super().__init__(**kwargs)
        self._client = GhyAIClient(api_key, base_url, timeout=timeout)

    @property
    def _llm_type(self):
        return "ghyai"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        def run():
            return asyncio.run(self._agenerate(messages, stop=stop, **kwargs))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        # 小说流程在异步入口中仍有同步 invoke，不能嵌套运行同一事件循环。
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ghyai-sync") as executor:
            return executor.submit(copy_context().run, run).result()

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop is not None:
            kwargs["stop"] = stop
        response = await self._client.chat(self.model, convert_to_openai_messages(messages), max_tokens=self.max_tokens, **kwargs)
        choice = response["choices"][0]
        value = choice["message"]
        tool_calls = [{"name": c["function"]["name"], "args": json.loads(c["function"]["arguments"]), "id": c["id"], "type": "tool_call"} for c in value.get("tool_calls") or []]
        message = AIMessage(content=value.get("content") or "", tool_calls=tool_calls,
                            response_metadata={"finish_reason": choice["finish_reason"], "request_id": self._client.last_request_id})
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"token_usage": response.get("usage", {}), "model_name": self.model})
