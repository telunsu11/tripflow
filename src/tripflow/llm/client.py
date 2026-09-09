"""任意 OpenAI 兼容端点的轻封装：一套代码接 GLM / DeepSeek / Qwen / OpenAI / Ollama。"""

from __future__ import annotations

from typing import Any

from ..config import Settings


class LLMError(RuntimeError):
    pass


def _friendly(exc: Exception) -> str:
    from openai import APIConnectionError, AuthenticationError

    if isinstance(exc, AuthenticationError):
        return "API Key 无效或无权限（检查 LLM_API_KEY 与 LLM_BASE_URL 是否匹配）"
    if isinstance(exc, APIConnectionError):
        return "无法连接 LLM_BASE_URL（检查网络与地址是否以 / 结尾、含 /v1 等）"
    return f"{type(exc).__name__}: {exc}"


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = None
        if settings.llm_api_key:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=90.0,  # 长文生成（需求解析/POI 提名）偶发慢，给足余量
                max_retries=2,
            )

    @property
    def configured(self) -> bool:
        return self._client is not None

    def ping(self) -> tuple[bool, str]:
        """探测端点可用性：优先 /models，不支持的端点退化为一次最小补全。"""
        if not self.configured:
            return False, "未配置 LLM_API_KEY"
        try:
            resp = self._client.models.list()
            names = ", ".join(m.id for m in resp.data[:3])
            return True, f"连接正常，可见模型: {names}" + ("…" if len(resp.data) > 3 else "")
        except Exception:  # noqa: BLE001 - 部分兼容端点不支持 /models，需走降级路径
            if not self._s.llm_model:
                return (
                    False,
                    "该端点不支持 /models 且未配置 LLM_MODEL，无法探测（可先填好模型名再试）",
                )
            try:
                self._client.chat.completions.create(
                    model=self._s.llm_model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
                return True, f"连接正常（{self._s.llm_model} 可用）"
            except Exception as exc:  # noqa: BLE001 - ping 要返回可读原因而非裸异常
                return False, _friendly(exc)

    def chat(
        self, messages: list[dict[str, Any]], *, model: str | None = None, temperature: float = 0.3
    ) -> str:
        if not self.configured:
            raise LLMError("LLM 未配置（LLM_API_KEY 为空）")
        target = model or self._s.llm_model
        if not target:
            raise LLMError("未配置 LLM_MODEL")
        resp = self._client.chat.completions.create(
            model=target, messages=messages, temperature=temperature
        )
        return resp.choices[0].message.content or ""

    @property
    def fast_model(self) -> str:
        return self._s.llm_fast_model or self._s.llm_model
