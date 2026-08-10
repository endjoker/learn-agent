# -*- coding: utf-8 -*-
"""
网页工具 —— 让 Agent 能搜索互联网和读取网页内容

包含两个工具：
  1. WebSearchTool  — 搜索网页（基于 DuckDuckGo，免费无需 Key）
  2. WebFetchTool   — 读取指定网页的正文内容

使用方式：
    from tools.web_tools import WebSearchTool, WebFetchTool

    registry.register_tool(WebSearchTool())
    registry.register_tool(WebFetchTool())

依赖：
    pip install ddgs requests beautifulsoup4
"""

import re
import logging
import requests
from typing import Optional
from urllib.parse import urlparse
from .base_tool import BaseTool
from core.safe_http import UnsafeUrl, request as safe_request

logger = logging.getLogger('hello_agent')


# ============================================================
# 1. WebSearchTool —— 搜索网页
# ============================================================

class WebSearchTool(BaseTool):
    """
    网页搜索工具

    基于 DuckDuckGo 搜索引擎，免费、无需 API Key。
    返回标题、链接、内容摘要，适合 Agent 了解最新信息。
    """

    name: str = "search"
    capabilities = ("net:egress",)
    description: str = "搜索互联网。当需要查找最新信息、新闻、文档、教程等内容时使用。基于 DuckDuckGo，返回标题、链接和摘要。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，尽量精确简短",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认 5 条",
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, max_results: int = 5) -> str:
        """
        执行网页搜索

        参数:
            query:       搜索关键词
            max_results: 返回结果数（1~10）

        返回:
            搜索结果的格式化文本
        """
        max_results = max(1, min(max_results, 10))

        # 尝试多个搜索源，一个失败就换下一个
        results = None
        errors = []

        # 源 1：DuckDuckGo
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
        except ImportError:
            errors.append("ddgs 未安装 (pip install ddgs)")
        except Exception as e:
            errors.append(f"DuckDuckGo: {e}")

        # 源 2：Bing 网页抓取
        if not results:
            try:
                results = self._search_bing(query, max_results)
            except Exception as e:
                errors.append(f"Bing: {e}")

        # 所有源都失败
        if not results:
            err_msg = "；".join(errors)
            return (
                f"❌ 搜索失败（已尝试 DuckDuckGo、Bing）\n"
                f"   错误: {err_msg}\n\n"
                f"💡 尝试用 web_fetch 工具直接访问已知网页。"
            )

        # --- 构造输出 ---
        parts = [f"🔍 搜索: '{query}'（共 {len(results)} 条）", ""]

        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")
            link = r.get("href", "")
            snippet = r.get("body", "")

            if len(snippet) > 300:
                snippet = snippet[:300] + "……"

            parts.append(f"  [{i}] {title}")
            parts.append(f"      🔗 {link}")
            if snippet:
                parts.append(f"      📝 {snippet}")
            parts.append("")

        parts.append("💡 用 web_fetch 工具查看某条结果的详细内容")

        return "\n".join(parts)

    # ============================================================
    # 备用搜索源：Bing 网页抓取
    # ============================================================

    @staticmethod
    def _search_bing(query: str, max_results: int) -> list:
        """
        通过抓取 Bing 搜索结果页来搜索（备用方案）
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        url = f"https://www.bing.com/search?q={requests.utils.quote(query)}&count={max_results}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        results = []
        for item in soup.select("li.b_algo")[:max_results]:
            title_el = item.select_one("h2 a")
            snippet_el = item.select_one(".b_caption p")

            if title_el:
                results.append({
                    "title": title_el.get_text(strip=True),
                    "href": title_el.get("href", ""),
                    "body": snippet_el.get_text(strip=True) if snippet_el else "",
                })

        return results


# ============================================================
# 2. WebFetchTool —— 读取网页内容
# ============================================================

class WebFetchTool(BaseTool):
    """
    网页内容读取工具

    访问指定 URL 并提取正文文本内容。
    适合 Agent 查看文章、文档、新闻详情等。
    """

    name: str = "web_fetch"
    capabilities = ("net:egress",)
    description: str = "读取指定网页的正文内容。当需要查看某篇文章、新闻或文档的详细内容时使用。传入 URL 即可获取纯文本内容。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要读取的网页链接（完整 URL，如 https://example.com/article）",
            },
            "max_chars": {
                "type": "integer",
                "description": "最多获取多少字符，默认 5000",
            },
        },
        "required": ["url"],
    }

    # 常见阅读模式的 user-agent
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def execute(self, url: str, max_chars: int = 5000) -> str:
        """
        读取网页正文内容

        参数:
            url:       网页链接
            max_chars: 最多返回字符数

        返回:
            网页正文的纯文本
        """
        # ---- 1. 校验 URL ----
        if not url.startswith(("http://", "https://")):
            return f"❌ 无效 URL: {url}\n   必须以 http:// 或 https:// 开头"

        # ---- 2. 请求网页 ----
        try:
            resp = safe_request("GET", url, headers=self.HEADERS, timeout=15)
            resp.raise_for_status()

        except requests.Timeout:
            return f"⏰ 请求超时（15 秒）: {url}"
        except requests.HTTPError as e:
            return f"❌ HTTP 错误 {e.response.status_code}: {url}"
        except requests.ConnectionError:
            return f"❌ 无法连接: {url}"
        except UnsafeUrl as e:
            return f"⛔ 安全拦截: {e}"
        except Exception as e:
            logger.error(f"请求失败: {e}", exc_info=True)
            return f"❌ 请求失败: {type(e).__name__}: {e}"

        # ---- 3. 检测编码 ----
        resp.encoding = resp.apparent_encoding or "utf-8"

        # ---- 4. 提取正文 ----
        content = self._extract_text(resp.text)

        # ---- 5. 截断 ----
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n……（内容过长，仅显示前 {max_chars} 字符）"

        # ---- 6. 构造结果 ----
        domain = urlparse(url).netloc
        char_count = len(content)

        return (
            f"📄 {url}\n"
            f"   来源: {domain}  |  获取 {char_count} 字符\n\n"
            f"{content}\n"
        )

    @staticmethod
    def _extract_text(html: str) -> str:
        """
        从 HTML 中提取可读文本

        优先用 BeautifulSoup，失败时用正则兜底。
        """
        text = ""

        # 尝试用 BeautifulSoup
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")

            # 移除无用标签
            for tag in soup(["script", "style", "nav", "footer", "header",
                             "iframe", "noscript", "form", "button"]):
                tag.decompose()

            # 提取正文
            text = soup.get_text(separator="\n", strip=True)

        except ImportError:
            pass
        except Exception:
            text = ""

        # BeautifulSoup 失败时用正则兜底
        if not text.strip():
            text = _regex_extract_text(html)

        # 清理多余空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)


# ============================================================
# 正则兜底提取（不依赖 BeautifulSoup）
# ============================================================

def _regex_extract_text(html: str) -> str:
    """用正则从 HTML 中提取文本（备用方案）"""
    # 移除 script 和 style 块
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", " ", html)

    # 处理 HTML 实体
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")

    # 合并空白字符
    text = re.sub(r"\s+", " ", text).strip()

    return text


# ============================================================
# 工具列表，方便批量注册
# ============================================================

WEB_TOOLS = [
    WebSearchTool,
    WebFetchTool,
]


def register_web_tools(registry):
    """一键注册所有网页工具到注册表"""
    from .registry import ToolRegistry
    if not isinstance(registry, ToolRegistry):
        raise TypeError("参数必须是 ToolRegistry 实例")
    for tool_cls in WEB_TOOLS:
        registry.register_tool(tool_cls())
    return registry
