"""Agent 运行时配置与网络错误分类。"""

from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
import os
from pathlib import Path
import httpx
from google.genai import errors as genai_errors


ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 32768
DEFAULT_TIMEOUT = 300
MULTI_LABEL = os.getenv("MULTI_LABEL", "false").strip().lower() in {"1", "true", "yes"}

_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MODEL_OVERRIDE_VAR: ContextVar[str | None] = ContextVar("MODEL_OVERRIDE_VAR", default=None)


@lru_cache(maxsize=1)
def _load_base_env() -> tuple[str, str]:
    """优先从项目本地环境加载 Gemini API Key 和模型配置。"""
    # 优先查找项目根目录下的 .env.local 或 .env
    for candidate in (ROOT / ".env.local", ROOT / ".env", ROOT.parent.parent / ".env.local", ROOT.parent.parent / ".env"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API")
    model_name = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"
    if not api_key:
        raise RuntimeError("未配置 GEMINI_API_KEY 或 GEMINI_API 环境变量")
    return api_key, model_name


def load_agent_env() -> tuple[str, str]:
    api_key, base_model = _load_base_env()
    return api_key, MODEL_OVERRIDE_VAR.get() or base_model


load_agent_env.cache_info = _load_base_env.cache_info  # type: ignore[attr-defined]
load_agent_env.cache_clear = _load_base_env.cache_clear  # type: ignore[attr-defined]


def is_transient_model_error(exc: BaseException) -> bool:
    """判断模型调用失败是否属于瞬时网络/限流/服务端问题，值得原样退避重试。"""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError)):
        return True
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, (genai_errors.ClientError, genai_errors.APIError)):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in _TRANSIENT_STATUS_CODES:
            return True

    transient_markers = (
        " 429 ", " 500 ", " 502 ", " 503 ", " 504 ",
        "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "timed out", "SSL", "EOF",
    )
    return any(marker in str(exc) for marker in transient_markers)
