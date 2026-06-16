from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from newcode.config import AppConfig
from newcode.providers.base import ProviderError
from newcode.session import ChatMessage


class DeepSeekProvider:
    def __init__(self, config: AppConfig, api_key: str, client: Any | None = None) -> None:
        self.config = config
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=config.base_url)
        self.client = client

    def stream_chat(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        payload = [{"role": message.role, "content": message.content} for message in messages]
        try:
            stream = self.client.chat.completions.create(
                model=self.config.model,
                messages=payload,
                stream=True,
            )
            for chunk in stream:
                text = _extract_delta_text(chunk)
                if text:
                    yield text
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("模型服务请求失败，请稍后重试。", exc) from exc


def _extract_delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""
