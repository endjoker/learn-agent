"""
LLM 客户端模块 —— 与各种大语言模型 API 通信的核心组件

支持两类部署方式：
  1. 云端 API（OpenAI、DeepSeek 等 OpenAI 兼容服务）
  2. 本地模型（Ollama、LM Studio、vLLM、llama.cpp 等）

通过 .env 配置：
    # 基础配置
    LLM_TYPE=cloud            # cloud = 云端, local = 本地模型
    LLM_MODEL_ID=gemma4       # 模型名称

    # 云端用：API Key 必填
    LLM_API_KEY=sk-xxx

    # 本地用：可通过 LLM_PROVIDER 自动补全地址
    LLM_PROVIDER=ollama       # ollama / lm_studio / vllm / llama_cpp
    # 或手动指定地址（优先级高于自动补全）
    LLM_BASE_URL=http://localhost:11434/v1

使用方式：
    from core import HelloAgentsLLM

    # 从 .env 自动加载
    llm = HelloAgentsLLM()

    # 手动指定（覆盖 .env）
    llm = HelloAgentsLLM(
        llm_type="local",
        provider="ollama",
        model="gemma4",
    )
"""

import os
from pathlib import Path
from typing import List, Dict, Optional

from dotenv import load_dotenv
from openai import OpenAI


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
        "api_key": "ollama",  # Ollama 不校验 key，但不能为空
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
    """列出所有支持的本地服务提供商（给用户看）"""
    lines = ["支持的本地模型服务:"]
    for key, info in LOCAL_PROVIDERS.items():
        lines.append(f"  {key:12s} → {info['base_url']:35s} ({info['desc']})")
    return "\n".join(lines)


def detect_provider_from_url(base_url: str) -> Optional[str]:
    """根据 base_url 自动匹配对应的提供商名称"""
    for key, info in LOCAL_PROVIDERS.items():
        if info["base_url"] in base_url:
            return key
    return None


# ============================================================
# LLM 客户端
# ============================================================

class HelloAgentsLLM:
    """
    大语言模型（LLM）客户端

    同时支持云端 API 和本地模型：
      - 云端 (LLM_TYPE=cloud)  ：需要 API Key，校验严格
      - 本地 (LLM_TYPE=local)  ：API Key 可选，自动补全服务地址

    使用方式：
        # .env 自动加载
        llm = HelloAgentsLLM()

        # 本地模型快捷方式
        llm = HelloAgentsLLM(provider="ollama", model="gemma4")

        # 手动指定全部参数
        llm = HelloAgentsLLM(
            model="gemma4",
            base_url="http://localhost:11434/v1",
        )
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        llm_type: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        """
        初始化 LLM 客户端

        参数优先级：传入参数 > 环境变量 > 预置默认值

        参数:
            model:    模型名称，如 "gemma4"、"deepseek-v4-flash"
                     （环境变量: LLM_MODEL_ID）
            api_key:  API 密钥。本地模型通常不需要
                     （环境变量: LLM_API_KEY）
            base_url: API 服务地址。本地模型可通过 provider 自动补全
                     （环境变量: LLM_BASE_URL）
            timeout:  请求超时秒数，默认 60
                     （环境变量: LLM_TIMEOUT）
            llm_type: "cloud" 或 "local"，控制校验策略
                     （环境变量: LLM_TYPE，默认 cloud）
            provider: 本地服务提供商。设置后自动补全 base_url 和 api_key
                     支持: ollama / lm_studio / vllm / llama_cpp
                     （环境变量: LLM_PROVIDER）
        """
        # ---- 读取配置（参数 > 环境变量 > 默认值） ----
        provider_name = provider or os.getenv("LLM_PROVIDER") or ""
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.provider = provider_name

        # ---- 判断模式：local / cloud ----
        # 指定了 provider 自动视为 local，否则从环境变量读，默认 cloud
        if provider_name:
            self.llm_type = "local"
        else:
            self.llm_type = (llm_type or os.getenv("LLM_TYPE") or "cloud").lower()

        # ---- 处理 base_url ----
        # 优先级：传入参数 > provider 预置（更具体）> 环境变量 > 报错
        if base_url:
            self.base_url = base_url
        elif provider_name and provider_name in LOCAL_PROVIDERS:
            self.base_url = LOCAL_PROVIDERS[provider_name]["base_url"]
        elif os.getenv("LLM_BASE_URL"):
            self.base_url = os.getenv("LLM_BASE_URL")
        else:
            self.base_url = None

        # ---- 处理 api_key ----
        # 优先级：传入参数 > 环境变量 > provider 预置 > 本地兜底
        if api_key is not None:
            api_key_value = api_key
        elif self.llm_type == "local" and provider_name in LOCAL_PROVIDERS:
            # 本地模式 + 已知 provider：用预置 key
            api_key_value = LOCAL_PROVIDERS[provider_name]["api_key"]
        elif os.getenv("LLM_API_KEY"):
            api_key_value = os.getenv("LLM_API_KEY")
        elif self.llm_type == "local":
            # 本地模式 + 未知 provider + 没有环境变量：用占位 key
            api_key_value = "not-needed"
        else:
            api_key_value = ""

        # ---- 处理 timeout ----
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
                + "\n\n💡 提示：本地模型用 provider='ollama' 自动补全配置"
            )

        # ---- 创建 OpenAI 客户端 ----
        self.client = OpenAI(
            api_key=api_key_value,
            base_url=self.base_url,
            timeout=timeout_value,
        )

    # ============================================================
    # 核心方法：调用 LLM
    # ============================================================

    def think(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        stream: bool = True,
    ) -> Optional[str]:
        """
        让 LLM 思考并返回响应

        参数:
            messages:    对话消息列表
            temperature: 温度参数（0~1），Agent 默认用 0
            stream:      是否流式输出，默认 True

        返回:
            LLM 响应文本，出错返回 None
        """
        # 显示调用信息（区分本地/云端）
        mode_tag = "🏠 本地" if self.llm_type == "local" else "☁️ 云端"
        print(f"  ▶ {mode_tag} {self.model}（{self.base_url}）")

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=stream,
            )

            if stream:
                # ---- 流式模式 ----
                collected = []
                for chunk in response:
                    if not chunk.choices:
                        continue
                    content = chunk.choices[0].delta.content or ""
                    print(content, end="", flush=True)
                    collected.append(content)
                print()
                return "".join(collected)
            else:
                # ---- 非流式模式 ----
                content = response.choices[0].message.content
                return content

        except Exception as e:
            print(f"\n  ❌ LLM 调用失败: {type(e).__name__}: {e}")
            return None

    # ============================================================
    # 辅助方法
    # ============================================================

    def __str__(self) -> str:
        mode = "本地" if self.llm_type == "local" else "云端"
        prov = f" [{self.provider}]" if self.provider else ""
        return f"HelloAgentsLLM({mode}{prov}, model={self.model})"

    def __repr__(self) -> str:
        return f"<HelloAgentsLLM type={self.llm_type} model='{self.model}'>"
