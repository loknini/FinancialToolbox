from dataclasses import dataclass
from typing import Protocol

from app.core.errors import BusinessError


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int


class ModelProvider(Protocol):
    def respond(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


class DisabledModelProvider:
    enabled = False

    def respond(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        raise BusinessError("CAPABILITY_DISABLED", "尚未接入聊天模型", 403)


class ChatCompletionsProvider:
    """Server-configured function-calling HTTP adapter; no SDK/vendor lock-in."""

    enabled = True

    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def respond(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        import json
        import re

        import httpx

        body = {
            "model": self.settings.model_name,
            "messages": messages,
            "max_tokens": self.settings.model_max_output_tokens,
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ],
        }
        try:
            with httpx.Client(
                timeout=self.settings.model_timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self.settings.model_base_url.rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.settings.model_api_key.get_secret_value()
                    },
                    json=body,
                )
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise BusinessError("MODEL_UNAVAILABLE", "模型暂不可用", 503, retryable=True)
            if response.status_code != 200:
                raise BusinessError(
                    "MODEL_CONFIGURATION_ERROR", "模型请求被拒绝，请检查服务端配置", 503
                )
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ValueError("incomplete reply")
            msg = choice["message"]
            text = msg.get("content") or ""
            if not isinstance(text, str) or len(text) > 20000:
                raise ValueError("invalid content")
            calls = []
            for item in msg.get("tool_calls") or []:
                arguments = json.loads(item["function"]["arguments"])
                if (
                    not isinstance(arguments, dict)
                    or not isinstance(item["id"], str)
                    or not item["id"]
                ):
                    raise ValueError("invalid call")
                name = item["function"]["name"]
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", name)
                    or len(item["id"]) > 128
                    or len(item["function"]["arguments"]) > 64000
                ):
                    raise ValueError("invalid tool")
                calls.append(ToolCall(item["id"], name, arguments))
            if len(calls) > 20 or len({call.id for call in calls}) != len(calls):
                raise ValueError("invalid call count")
            if not calls and not text.strip():
                raise ValueError("empty response")
            usage = data.get("usage") or {}
            counts = [usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)]
            if any(type(n) is not int or not 0 <= n <= 10000000 for n in counts):
                raise ValueError("invalid usage")
            return ModelReply(text, calls, *counts)
        except httpx.HTTPError as exc:
            raise BusinessError(
                "MODEL_UNAVAILABLE", "模型连接暂时失败", 503, retryable=True
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BusinessError("MODEL_INVALID_RESPONSE", "模型返回格式不符合要求", 503) from exc


def build_model(settings):
    if settings.model_provider == "disabled":
        return DisabledModelProvider()
    return ChatCompletionsProvider(settings)
