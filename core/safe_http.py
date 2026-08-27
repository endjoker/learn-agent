"""Fail-closed HTTP helper for agent egress tools."""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger("jk_agent")

_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 响应体上限默认 10MB


class UnsafeUrl(ValueError):
    pass


_thread_local = threading.local()
_DNS_CACHE_TTL = 5.0


def _session() -> requests.Session:
    """Reuse one HTTP session per calling thread without sharing mutable state."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def _loopback_allowed() -> bool:
    """是否放行回环地址（方案 C 全局开关，默认关闭）。

    读 config.json → agent_runtime.http_allow_loopback；True 时仅放行
    is_loopback 段（127.0.0.0/8、::1），私网/链路本地/CGNAT 等照旧拦截。
    适用场景：Agent 需要访问本机 dev server / 本地 MCP 端点。
    注意这是进程级开关：http 工具、web 工具、cron/webhook 投递共用此判定。
    """
    try:
        from core.config_loader import load_config
        return bool((load_config().get("agent_runtime") or {}).get(
            "http_allow_loopback", False))
    except Exception:
        return False


def _intranet_allowed() -> bool:
    """是否放行整个内网段（默认开启——本 Agent 为本地工具，需访问本地
    MCP/dev server/内部 API）。

    读 config.json → agent_runtime.http_allow_intranet；True 时放行
    RFC1918 私网（10/8、172.16/12、192.168/16）+ 回环（127/8、::1）+
    链路本地（169.254/16）+ CGNAT（100.64/10）等内网段。组播/保留/未指定
    段仍拦截（不具内网语义）。

    例外：云元数据端点（_METADATA_BLOCKLIST/_METADATA_HOSTNAMES，如
    AWS/GCP/Azure 的 169.254.169.254、阿里云 100.100.100.200）**恒拦**，
    不受本开关与 http_allow_loopback 影响——它们是内网段中几乎没有合法
    使用场景、却在提示注入攻击链中价值最高的目标（窃取云凭证），策略
    语义等同高危命令硬拒。
    """
    try:
        from core.config_loader import load_config
        return bool((load_config().get("agent_runtime") or {}).get(
            "http_allow_intranet", True))
    except Exception:
        return True


# 云元数据端点恒拦名单（策略无关，任何开关都翻不掉）：
# - 169.254.169.254        AWS / Azure / GCP 实例元数据（IMDS）
# - 169.254.170.2          AWS ECS 任务元数据
# - fd00:ec2::254          AWS IMDS IPv6 端点（unique-local → is_private）
# - 100.100.100.200        阿里云 ECS 元数据（落在 CGNAT 段）
# - 168.63.129.16          Azure 平台服务虚拟公网 IP（WAMDS/元数据族）
_METADATA_BLOCKLIST = frozenset(
    ipaddress.ip_address(x) for x in (
        "169.254.169.254", "169.254.170.2",
        "fd00:ec2::254", "100.100.100.200", "168.63.129.16"))
# 已知元数据主机名字面量：解析前直接拒绝（GCE 内解析到 169.254.169.254，
# 公网环境解析结果不固定——按名字拦比按解析结果拦更稳）。
_METADATA_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})


def _is_intranet_address(ip) -> bool:
    """是否属于放行的内网段（_intranet_allowed 时豁免）。

    放行：loopback / private(RFC1918) / link-local(含云元数据) / CGNAT；
    不放行：multicast / reserved / unspecified（仍落入严格拦截）。
    CGNAT(100.64/10)：is_global=False 且 is_private=False，需用
    "not is_global 且非保留/组播/未指定" 显式判定。
    """
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return (not ip.is_global and not ip.is_multicast
            and not ip.is_reserved and not ip.is_unspecified)


def _resolve_validated(hostname: str, port: int) -> set[str]:
    """解析 hostname 并校验地址集合（TTL 内缓存直接复用）。

    任一解析结果命中私网/回环等黑名单即抛 UnsafeUrl（fail-closed）；
    回环段可经 agent_runtime.http_allow_loopback 显式放行。
    返回通过校验的地址集合。
    """
    cache = getattr(_thread_local, "dns_cache", None)
    if cache is None:
        cache = {}
        _thread_local.dns_cache = cache
    # 云元数据主机名字面量恒拦（解析前判定，任何放行开关不可豁免）
    host_lower = str(hostname).lower().rstrip(".")
    if host_lower in _METADATA_HOSTNAMES:
        logger.warning("SSRF 拦截(云元数据恒拦): 主机名 %s", hostname)
        raise UnsafeUrl(f"destination is a blocked metadata endpoint: {hostname}")
    cache_key = (host_lower, port)
    cached = cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _DNS_CACHE_TTL:
        return set(cached[1])
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrl(f"hostname cannot be resolved: {exc}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise UnsafeUrl("hostname has no addresses")
    allow_loopback = _loopback_allowed()
    allow_intranet = _intranet_allowed()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # 云元数据段恒拦：优先级高于一切放行开关（内网放开/回环放开都不
        # 豁免）。这是提示注入→窃取云凭证攻击链的最后闸门。
        if ip in _METADATA_BLOCKLIST:
            logger.warning("SSRF 拦截(云元数据恒拦): %s 解析到 %s",
                           hostname, address)
            raise UnsafeUrl(
                f"destination resolves to a blocked metadata address: {address}")
        # 内网段放行（agent_runtime.http_allow_intranet，默认开）：本 Agent 为
        # 本地工具，需访问本地 MCP/dev server/内部 API。组播/保留/未指定段仍
        # 拦截（不具内网语义）。回环段的细分豁免仍尊重 http_allow_loopback。
        if allow_intranet and _is_intranet_address(ip):
            continue
        # 兼容旧开关：仅回环段可经 http_allow_loopback=true 显式放行
        if ip.is_loopback and allow_loopback:
            continue
        # 综合判定（P3 边缘段修复）：在既有检查外增加 not ip.is_global，
        # 补齐 CGNAT 100.64.0.0/10 等"非 private 但不可公网路由"的边缘段
        # （venv Python 3.14 实测：100.64.1.1 → is_global=False 且
        # is_private=False，旧检查拦不住）。is_global 语义随版本有差异的
        # 特殊段已核实不误伤公网：
        #   - 192.0.0.9 / 192.0.0.10（PCP/TURN anycast，全局可达）→ True；
        #   - 组播 224.0.0.0/4 → True 但被下方 is_multicast 兜底拦截；
        #   - 64:ff9b::/96（NAT64）→ True 但被 is_reserved 兜底拦截。
        # 保留全部既有谓词做纵深防御，防不同 Python 版本 is_global 判定差异。
        if (not ip.is_global or ip.is_loopback or ip.is_private
                or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified):
            logger.warning("SSRF 拦截: %s 解析到受限地址 %s (http_allow_loopback=%s, http_allow_intranet=%s)",
                           hostname, address, allow_loopback, allow_intranet)
            raise UnsafeUrl(f"destination resolves to a blocked address: {address}")
    cache[cache_key] = (time.monotonic(), tuple(addresses))
    return addresses


def validate_url(url: str) -> None:
    """请求前校验：scheme/hostname 合法性 + 解析结果私网/黑名单检查。

    每次 hop 前同步解析，fail-closed 防私网/回环 SSRF 与 DNS-rebinding 的
    校验侧绕过（校验结果缓存在线程局部，供连接后对端复核使用）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrl("URL must be http(s), have a hostname, and contain no user-info")
    _resolve_validated(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


def _peer_ip(response: requests.Response) -> str | None:
    """取连接对端 IP（依赖 urllib3 内部结构，失败返回 None）。"""
    try:
        raw = response.raw
        connection = getattr(raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            return None
        return sock.getpeername()[0]
    except Exception:
        return None


def _using_proxy(scheme: str) -> bool:
    """环境是否配置了代理（代理场景无法核对目标对端 IP，跳过 peer 复核）。"""
    keys = (f"{scheme.upper()}_PROXY", f"{scheme.lower()}_proxy",
            "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    return any(os.environ.get(k) for k in keys)


def _verify_peer(response: requests.Response, validated: set[str]) -> None:
    """连接后二次校验：对端 IP 必须 ∈ 请求前校验过的地址集。

    DNS rebinding TOCTOU 缓解：请求前 validate_url 的解析结果与 requests
    实际连接所用 IP 可能不一致（校验后 DNS 被重绑到内网地址）。连接建立后
    取对端 IP 复核，不一致立即拒绝并关闭连接。
    """
    if not validated or _using_proxy(urlparse(response.url).scheme):
        return
    peer = _peer_ip(response)
    if peer is None:
        return  # 无法取到对端（连接已关闭/异常）：依赖请求前校验
    if peer not in validated:
        logger.warning("SSRF/rebinding 拦截: 对端 %s 不在已校验地址集 %s",
                       peer, sorted(validated))
        raise UnsafeUrl(f"连接对端 IP 与校验结果不一致（疑似 DNS rebinding）: {peer}")


def _pin_http_target(url: str, addresses: set[str]) -> tuple[str, dict]:
    """http 目标固定到已校验 IP（Host 头保留原域名），消除 DNS rebinding TOCTOU。

    https 不做 IP 重写（TLS SNI/证书校验与 Host 绑定冲突），靠 _verify_peer 兜底。
    返回 (请求 URL, 需合并的请求头)。
    """
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or not addresses:
        return url, {}
    address = sorted(addresses)[0]
    if ":" in address and not address.startswith("["):
        address = f"[{address}]"
    port = parsed.port or 80
    netloc = address if port == 80 else f"{address}:{port}"
    pinned = parsed._replace(netloc=netloc).geturl()
    host_header = parsed.hostname if port == 80 else f"{parsed.hostname}:{port}"
    return pinned, {"Host": host_header}


def _consume_body(response: requests.Response, max_bytes: int) -> requests.Response:
    """流式读取响应体，超过上限截断；仍返回可用 Response（content/text/json 正常）。

    截断时记录 response.truncated = True（调用方可观察），并用 logger 提示。
    """
    max_bytes = max(0, int(max_bytes))
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                truncated = True
                room = max_bytes - (total - len(chunk))
                if room > 0:
                    chunks.append(chunk[:room])
                break
            chunks.append(chunk)
    except Exception as exc:
        # 流读取异常：保留已读部分，不把异常扩散给调用方
        logger.warning("响应体读取中断: %s", exc)
    finally:
        response.close()
    response._content = b"".join(chunks)
    response._content_consumed = True
    response.truncated = truncated
    if truncated:
        logger.warning("响应体超过 %d 字节上限，已截断: %s", max_bytes, response.url)
    return response


def request(method: str, url: str, *, max_redirects: int = 5,
            max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
            **kwargs) -> requests.Response:
    """带每跳 DNS 校验与响应体上限的请求；不隐式跟随重定向。

    - 每跳请求前 validate_url（解析 + 私网/黑名单 fail-closed）
    - http 目标固定到已校验 IP（Host 头保留原域名）
    - 连接建立后对端 IP 复核（https 的 DNS rebinding TOCTOU 兜底）
    - 响应体流式读取，超过 max_response_bytes 截断（默认 10MB，可配）
    """
    current = url
    session = _session()
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        validate_url(current)
        addresses = _resolve_validated(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        request_url, pin_headers = _pin_http_target(current, addresses)
        headers = dict(kwargs.get("headers") or {})
        headers.update(pin_headers)
        hop_kwargs = dict(kwargs)
        hop_kwargs["headers"] = headers
        response = session.request(method, request_url, allow_redirects=False,
                                   stream=True, **hop_kwargs)
        try:
            _verify_peer(response, addresses)
        except Exception:
            response.close()
            raise
        if not response.is_redirect and response.status_code not in {301, 302, 303, 307, 308}:
            return _consume_body(response, max_response_bytes)
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise UnsafeUrl("redirect without Location")
        if _ == max_redirects:
            raise UnsafeUrl("too many redirects")
        current = urljoin(current, location)
        if response.status_code == 303 and method.upper() != "HEAD":
            method = "GET"
            kwargs.pop("json", None)
            kwargs.pop("data", None)
    raise UnsafeUrl("too many redirects")