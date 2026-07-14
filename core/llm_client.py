"""
LLM 客户端模块 —— 与各种大语言模型 API 通信的核心组件

支持两类部署方式：
  1. 云端 API（OpenAI、DeepSeek 等 OpenAI 兼容服务）
  2. 本地模型（Ollama、LM Studio、vLLM、llama.cpp 等）

通过 .env 配置：
    LLM_TYPE=cloud              # cloud / local
    LLM_MODEL_ID=deepseek-v4-flash
    LLM_API_KEY=sk-xxx
    LLM_BASE_URL=https://api.deepseek.com
    LLM_CONTEXT_LENGTH=1048576  # 可选，自动检测时无需设置
"""

import os
import re
import time
import logging
from pathlib import Path
from typing import List, Dict, Optional

from dotenv import load_dotenv
from openai import OpenAI

logger = logging.getLogger('hello_agent')


# ============================================================
# 环境变量加载
# ============================================================

def _load_env_file():
    """自动寻找项目根目录的 .env 文件"""
    loaded = load_dotenv(verbose=False)
    if loaded:
        return
    current_dir = Path(__file__).resolve().parent
    for parent in [current_dir, current_dir.parent]:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path, verbose=False)
            return

_load_env_file()


# ============================================================
# 本地模型服务预置配置
# ============================================================

LOCAL_PROVIDERS = {
    "ollama": {
        "name": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "desc": "最易用的本地模型运行工具，支持 Gemma/Llama/Qwen 等",
    },
    "lm_studio": {
        "name": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm_studio",
        "desc": "图形化本地模型管理，内置 OpenAI 兼容服务",
    },
    "vllm": {
        "name": "vLLM",
        "base_url": "http://localhost:8000/v1",
        "api_key": "not-needed",
        "desc": "高性能推理引擎，适合生产级部署",
    },
    "llama_cpp": {
        "name": "llama.cpp",
        "base_url": "http://localhost:8080/v1",
        "api_key": "not-needed",
        "desc": "轻量级 C++ 推理，资源占用低",
    },
}


def list_local_providers() -> str:
    """列出所有支持的本地服务提供商"""
    lines = ["支持的本地模型服务:"]
    for key, info in LOCAL_PROVIDERS.items():
        lines.append(f"  {key:12s} → {info['base_url']:35s} ({info['desc']})")
    return "\n".join(lines)


# ============================================================
# 模型上下文长度映射表
# ============================================================
#
# 根据模型名称自动匹配上下文长度。
# 匹配规则：小写 + 模糊匹配（包含关键词）。
# 越靠前的规则优先级越高。

_MODEL_CONTEXT_MAP = [
    # DeepSeek 系列
    (r"deepseek.*v4|deepseek.*flash", 1048576),
    (r"deepseek.*v3|deepseek.*r1", 131072),
    (r"deepseek", 131072),

    # Gemma 系列
    (r"gemma.*4|gemma4", 262144),
    (r"gemma.*3|gemma3", 131072),
    (r"gemma", 8192),

    # GPT 系列
    (r"gpt-4o|gpt4o", 131072),
    (r"gpt-4", 32768),
    (r"gpt-3\.5|gpt3\.5", 16384),

    # Claude 系列
    (r"claude.*opus|claude3.*opus", 200000),
    (r"claude.*sonnet|claude3.*sonnet", 200000),
    (r"claude.*haiku|claude3.*haiku", 200000),
    (r"claude", 100000),

    # Qwen 系列
    (r"qwen.*2\.5|qwen2\.5", 131072),
    (r"qwen.*max|qwen-max", 32768),
    (r"qwen.*plus|qwen-plus", 131072),
    (r"qwen", 32768),

    # Llama 系列
    (r"llama.*3\.1|llama3\.1", 131072),
    (r"llama.*3|llama3", 8192),
    (r"llama.*2|llama2", 4096),
    (r"llama", 8192),

    # Mistral 系列
    (r"mistral.*large|mistral-large", 131072),
    (r"mistral|mixtral", 32768),

    # Yi 系列
    (r"yi.*1\.5|yi1\.5|yi-1\.5", 131072),
    (r"yi-34b|yi-6b|yi", 4096),

    # GLM / ChatGLM 系列
    (r"glm-4|chatglm-4", 131072),
    (r"glm-3|chatglm-3", 32768),
    (r"chatglm", 32768),

    # Kimi / Moonshot
    (r"moonshot|kimi", 131072),

    # Ornith 等自定义模型
    (r"ornith", 204800),

    # 默认
]


def detect_context_length(model_name: Optional[str], default: int = 32768) -> int:
    """
    根据模型名称自动检测上下文长度

    匹配规则：小写后按正则匹配，返回第一个匹配的值。
    未匹配时返回默认值。

    参数:
        model_name: 模型名称，如 "deepseek-v4-flash"、"gpt-4"
        default:    未匹配时的默认值

    返回:
        上下文长度（token 数）
    """
    if not model_name:
        return default

    name_lower = model_name.lower()
    for pattern, ctx_len in _MODEL_CONTEXT_MAP:
        if re.search(pattern, name_lower):
            return ctx_len

    return default


# ============================================================
# LLM 客户端
# ============================================================

class HelloAgentsLLM:
    """
    大语言模型（LLM）客户端

    同时支持云端 API 和本地模型：
      - 云端 (LLM_TYPE=cloud)  ：需要 API Key，校验严格
      - 本地 (LLM_TYPE=local)  ：API Key 可选，自动补全服务地址

    上下文长度自动检测：
      - 根据模型名称从内置映射表匹配
      - 可通过 LLM_CONTEXT_LENGTH 环境变量覆盖
      - 用于 Agent 上下文截断阈值的自动计算
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        llm_type: Optional[str] = None,
        provider: Optional[str] = None,
        context_length: Optional[int] = None,
    ):
        """
        初始化 LLM 客户端

        参数优先级：传入参数 > 环境变量 > 模型自动检测 > 默认值

        参数:
            model:    模型名称，如 "gemma4"、"deepseek-v4-flash"
            api_key:  API 密钥。本地模型通常不需要
            base_url: API 服务地址
            timeout:  请求超时秒数，默认 60
            llm_type: "cloud" 或 "local"
            provider: 本地服务提供商：ollama / lm_studio / vllm / llama_cpp
            context_length: 模型上下文窗口大小（token）
                     默认自动检测，环境变量 LLM_CONTEXT_LENGTH 可覆盖
        """
        # ---- 获取模型名称 ----
        self.model = model or os.getenv("LLM_MODEL_ID")
        model_prefix = f"{self.model}_" if self.model else ""

        # ---- 读取 model-prefixed 参数（优先），后备通用变量 ----
        def _get(key: str, default=None):
            """先读 {model}_{key}，再读 {key}"""
            return os.getenv(f"{model_prefix}{key}") or os.getenv(key) or default

        # 提供者：用于本地模型判断
        provider_name = provider or _get("PROVIDER", "")
        self.provider = provider_name

        # ---- 判断模式 ----
        if provider_name:
            self.llm_type = "local"
        else:
            self.llm_type = (llm_type or _get("TYPE") or "cloud").lower()

        # ---- base_url：传参 > {model}_BASE_URL > provider 默认 > LLM_BASE_URL ----
        if base_url:
            self.base_url = base_url
        elif _get("BASE_URL"):
            self.base_url = _get("BASE_URL")
        elif provider_name and provider_name in LOCAL_PROVIDERS:
            self.base_url = LOCAL_PROVIDERS[provider_name]["base_url"]
        else:
            self.base_url = None

        # ---- api_key：传参 > {model}_API_KEY > LLM_API_KEY > provider 默认 > 兜底 ----
        if api_key is not None:
            api_key_value = api_key
        elif _get("API_KEY"):
            api_key_value = _get("API_KEY")
        elif self.llm_type == "local" and provider_name in LOCAL_PROVIDERS:
            api_key_value = LOCAL_PROVIDERS[provider_name]["api_key"]
        elif self.llm_type == "local":
            api_key_value = "not-needed"
        else:
            api_key_value = ""

        # ---- 上下文长度：传参 > {model}_CONTEXT_LENGTH > 自动检测 ----
        if context_length is not None:
            self.context_length = context_length
        elif _get("CONTEXT_LENGTH"):
            self.context_length = int(_get("CONTEXT_LENGTH"))
        else:
            self.context_length = detect_context_length(self.model)

        # ---- 超时 ----
        timeout_value = timeout or int(os.getenv("LLM_TIMEOUT", "60"))

        # ---- 参数校验 ----
        missing = []
        if not self.model:
            missing.append("LLM_MODEL_ID（模型名称）")
        if not self.base_url:
            if provider_name and provider_name not in LOCAL_PROVIDERS:
                missing.append(
                    f"LLM_BASE_URL 或有效的 LLM_PROVIDER\n"
                    f"   提供商 '{provider_name}' 不在支持列表中\n"
                    f"   支持: {', '.join(LOCAL_PROVIDERS.keys())}"
                )
            else:
                missing.append("LLM_BASE_URL（API服务地址）")
        if self.llm_type == "cloud" and not api_key_value:
            missing.append("LLM_API_KEY（云端模式必填，本地模式可忽略）")
        if self.llm_type == "cloud" and not self.base_url:
            missing.append("LLM_BASE_URL（API服务地址）")

        if missing:
            raise ValueError(
                "LLM 客户端初始化失败，缺少以下配置：\n"
                + "\n".join(f"  - {m}" for m in missing)
                + f"\n\n当前模式: {self.llm_type}"
                + (f"\n提供商: {provider_name}" if provider_name else "")
            )

        # ---- 创建 OpenAI 客户端 ----
        self.client = OpenAI(
            api_key=api_key_value,
            base_url=self.base_url,
            timeout=timeout_value,
        )

        # ---- 最后一次 API 调用的 token 用量（锚点） ----
        self.last_usage: Optional[Dict[str, int]] = None

    @staticmethod
    def _extract_usage(usage) -> Optional[Dict[str, int]]:
        """
        从 API 返回的 usage 对象中提取 token 数。

        兼容两种字段命名：
          - OpenAI 格式: prompt_tokens / completion_tokens
          - DeepSeek 格式: input_tokens / output_tokens
        """
        if not usage:
            return None
        return {
            "input_tokens": getattr(usage, "input_tokens", None)
                            or getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", None)
                             or getattr(usage, "completion_tokens", 0) or 0,
        }

    # ============================================================
    # 核心方法：调用 LLM
    # ============================================================

    # 可重试的异常类型名（网络/连接类）
    _RETRYABLE_NAMES = frozenset({
        "RemoteProtocolError", "ReadError", "ConnectError",
        "ReadTimeout", "ConnectTimeout", "WriteError", "PoolTimeout",
        "APIConnectionError", "APITimeoutError", "Timeout",
        "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    })

    @classmethod
    def _get_retryable_types(cls):
        """收集可重试的异常类型（httpx + openai）"""
        types = []
        try:
            import httpx
            for name in ("RemoteProtocolError", "ReadError", "ConnectError",
                          "ReadTimeout", "ConnectTimeout", "WriteError",
                          "PoolTimeout", "ProtocolError"):
                t = getattr(httpx, name, None)
                if t is not None:
                    types.append(t)
        except ImportError:
            pass
        try:
            import openai
            for name in ("APIConnectionError", "APITimeoutError"):
                t = getattr(openai, name, None)
                if t is not None:
                    types.append(t)
        except (ImportError, AttributeError):
            pass
        return tuple(types)

    @classmethod
    def _is_retryable(cls, e):
        """判断异常是否值得重试（网络/连接类错误）"""
        exc_name = type(e).__name__
        if exc_name in cls._RETRYABLE_NAMES:
            return True
        for t in cls._get_retryable_types():
            if isinstance(e, t):
                return True
        if hasattr(e, "status_code") and isinstance(getattr(e, "status_code"), int):
            if e.status_code >= 500 or e.status_code == 429:
                return True
        return False

    def think(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        stream: bool = True,
        silent: bool = False,
    ) -> Optional[str]:
        """
        让 LLM 思考并返回响应（带重试逻辑）

        重试策略：
        - 网络类错误（RemoteProtocolError/Timeout/ConnectionError 等）自动重试
        - 最多重试 3 次，退避 1s -> 2s -> 4s
        - 流式失败后降级为非流式重试（更可靠）
        - plan 模式（silent=True）同样重试，仅写日志不打印

        参数:
            messages:   对话消息列表
            temperature: 生成温度
            stream:      是否流式输出
            silent:      静默模式（不输出模式标签，供内部压缩等场景使用）
        """
        MAX_RETRIES = 3
        RETRY_DELAYS = [1, 2, 4]

        if not silent:
            mode_tag = "🏠 本地" if self.llm_type == "local" else "☁️ 云端"
            print(f"  ▶ {mode_tag} {self.model}（{self.base_url}）")

        self.last_usage = None

        for attempt in range(1, MAX_RETRIES + 1):
            use_stream = stream if attempt == 1 else False

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    stream=use_stream,
                )

                if use_stream:
                    collected = []
                    for chunk in response:
                        if chunk.usage:
                            self.last_usage = self._extract_usage(chunk.usage)
                        if not chunk.choices:
                            continue
                        content = chunk.choices[0].delta.content or ""
                        if not silent:
                            print(content, end="", flush=True)
                        collected.append(content)
                    if not silent:
                        print()
                    return "".join(collected)
                else:
                    content = response.choices[0].message.content
                    if response.usage:
                        self.last_usage = self._extract_usage(response.usage)
                    if stream and attempt > 1 and not silent:
                        print(content)
                    return content

            except Exception as e:
                should_retry = self._is_retryable(e)

                if not should_retry or attempt >= MAX_RETRIES:
                    if not silent:
                        print(f"\n  ❌ LLM 调用失败: {type(e).__name__}: {e}")
                    logger.error(
                        f"LLM 调用失败（第 {attempt}/{MAX_RETRIES} 次）: "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    return None

                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                if not silent:
                    fallback = "，降级为非流式" if stream else ""
                    print(f"\n  ⚠️ 第 {attempt}/{MAX_RETRIES} 次失败: "
                          f"{type(e).__name__}，{delay}s 后重试{fallback}…")
                else:
                    logger.warning(
                        f"LLM 调用第 {attempt}/{MAX_RETRIES} 次失败: "
                        f"{type(e).__name__}，{delay}s 后重试…"
                    )

                time.sleep(delay)

        return None

    # ============================================================
    # 辅助方法
    # ============================================================

    def __str__(self) -> str:
        mode = "本地" if self.llm_type == "local" else "云端"
        prov = f" [{self.provider}]" if self.provider else ""
        return f"HelloAgentsLLM({mode}{prov}, model={self.model}, ctx={self.context_length})"

    def __repr__(self) -> str:
        return f"<HelloAgentsLLM type={self.llm_type} model='{self.model}'>"


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  LLM 客户端测试")
    print("=" * 50)

    print(f"\n{list_local_providers()}\n")

    # 测试上下文长度自动检测
    test_models = [
        "deepseek-v4-flash", "deepseek-r1", "gpt-4o", "gpt-4",
        "gemma4", "qwen2.5-32b", "claude-sonnet-4", "llama3.1-70b",
        "mistral-large", "glm-4", "moonshot-v1",
        "unknown-model",
    ]
    print("上下文长度检测:")
    for m in test_models:
        ctx = detect_context_length(m)
        print(f"  {m:30s} → {ctx}")

    # 测试初始化
    try:
        llm = HelloAgentsLLM()
        print(f"\n  ✅ 当前: {llm}")
    except ValueError as e:
        print(f"\n  ⚠️  配置不完整: {e}")
