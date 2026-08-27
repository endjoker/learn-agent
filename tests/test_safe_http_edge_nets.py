# -*- coding: utf-8 -*-
"""safe_http 边缘网段回归（内网拦截移除后语义）。

设计取向（本地 Agent 工具）：默认放行**内网段**（RFC1918 私网 + 回环 +
链路本地 + CGNAT）——Agent 需访问本地 MCP/dev server/内部 API，
对应 agent_runtime.http_allow_intranet（默认 True）。真正不具内网语义的
组播/保留/未指定段仍拦截。

例外（恒拦）：云元数据端点（169.254.169.254 / 169.254.170.2 /
fd00:ec2::254 / 100.100.100.200 / 168.63.129.16 及已知元数据主机名）
不受任何放行开关影响——提示注入窃取云凭证攻击链的最后闸门。

"严格模式"（http_allow_intranet=False）保留完整 SSRF fail-closed，用于
默认部署/公网控外场景；本文件的"拦截"用例统一在该模式下验证。
"""
import socket
import unittest
from unittest import mock

from core.safe_http import UnsafeUrl, _resolve_validated, validate_url


def _fake_getaddrinfo(addresses):
    def fake(hostname, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (addr, port)) for addr in addresses]
    return fake


class _StrictMixin(unittest.TestCase):
    """在严格模式下跑（内网拦截开启）——patch _intranet_allowed=False。"""

    def setUp(self):
        # 严格模式：内网放行 + 回环放行都关（config.json 可能设了 http_allow_loopback）
        p1 = mock.patch("core.safe_http._intranet_allowed", return_value=False)
        p2 = mock.patch("core.safe_http._loopback_allowed", return_value=False)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        # 清空线程局部 DNS 缓存，防串扰
        from core.safe_http import _thread_local
        _thread_local.dns_cache = {}


class CgnatBlockedTests(_StrictMixin):
    def test_cgnat_ip_literal_rejected(self):
        """100.64.1.1（CGNAT）严格模式下必须被拒绝——旧检查漏拦的边缘段。"""
        with self.assertRaises(UnsafeUrl):
            validate_url("http://100.64.1.1/")

    def test_cgnat_upper_edge_rejected(self):
        with self.assertRaises(UnsafeUrl):
            validate_url("http://100.127.255.255/")

    def test_cgnat_via_hostname_rejected(self):
        with mock.patch("socket.getaddrinfo",
                        _fake_getaddrinfo(["100.64.1.1"])):
            with self.assertRaises(UnsafeUrl):
                validate_url("http://cgnat.example.com/")

    def test_cgnat_upper_edge_via_hostname_rejected(self):
        with mock.patch("socket.getaddrinfo",
                        _fake_getaddrinfo(["100.127.255.255"])):
            with self.assertRaises(UnsafeUrl):
                validate_url("http://cgnat-edge.example.com/")


class PublicUnaffectedTests(unittest.TestCase):
    def test_public_ip_literal_passes_validation(self):
        for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
            addrs = _resolve_validated(ip, 80)
            self.assertIn(ip, addrs)

    def test_public_hostname_resolves_and_passes(self):
        with mock.patch("socket.getaddrinfo",
                        _fake_getaddrinfo(["8.8.8.8", "8.8.4.4"])):
            validate_url("http://example.com/")

    def test_special_global_anycast_not_blocked_by_is_global(self):
        addrs = _resolve_validated("192.0.0.9", 80)
        self.assertIn("192.0.0.9", addrs)


class StrictBlockedTests(_StrictMixin):
    """严格模式（http_allow_intranet=False）：私网/回环/链路本地/CGNAT 拦截。"""

    def test_private_still_blocked(self):
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1",
                   "169.254.1.1", "127.0.0.1", "100.64.1.1"):
            with self.assertRaises(UnsafeUrl, msg=ip):
                validate_url(f"http://{ip}/")

    def test_mixed_resolution_fail_closed(self):
        """混合解析结果逐地址判定：任一私网成员命中黑名单仍整体拒绝。"""
        with mock.patch("socket.getaddrinfo",
                        _fake_getaddrinfo(["127.0.0.1", "10.0.0.5"])):
            with self.assertRaises(UnsafeUrl):
                validate_url("http://mixed-loopback.example.com/")


class IntranetAllowedTests(unittest.TestCase):
    """内网放行（默认，http_allow_intranet=True）：本地 Agent 访问内网。"""

    def test_private_and_loopback_allowed(self):
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1",
                   "169.254.1.1", "127.0.0.1", "100.64.1.1"):
            addrs = _resolve_validated(ip, 80)
            self.assertIn(ip, addrs)

    def test_multicast_still_blocked(self):
        # 组播段不具内网语义，即使内网放行仍拦截
        with self.assertRaises(UnsafeUrl):
            validate_url("http://224.0.0.1/")


class MetadataHardBlockTests(unittest.TestCase):
    """云元数据端点恒拦：优先级高于内网放行/回环放行等一切开关。"""

    def setUp(self):
        # 不 patch _intranet_allowed——保持 config 默认（内网放行）状态，
        # 证明元数据拦截在"最放行"配置下依然生效。
        from core.safe_http import _thread_local
        _thread_local.dns_cache = {}

    def test_imds_v1_v4_blocked(self):
        with self.assertRaises(UnsafeUrl):
            validate_url("http://169.254.169.254/latest/meta-data/iam/security-credentials/")

    def test_ecs_task_metadata_blocked(self):
        with self.assertRaises(UnsafeUrl):
            validate_url("http://169.254.170.2/v2/credentials/")

    def test_aws_imds_ipv6_blocked(self):
        with self.assertRaises(UnsafeUrl):
            validate_url("http://[fd00:ec2::254]/latest/api/token")

    def test_aliyun_metadata_blocked(self):
        """阿里云元数据落在 CGNAT 段（非 private），同样必须恒拦。"""
        with self.assertRaises(UnsafeUrl):
            validate_url("http://100.100.100.200/latest/meta-data/")

    def test_azure_wamds_blocked(self):
        with self.assertRaises(UnsafeUrl):
            validate_url("http://168.63.129.16/machine/")

    def test_metadata_hostname_literal_blocked_without_dns(self):
        """主机名字面量在解析前拒绝；DNS 根本不应被调用。"""
        with mock.patch("socket.getaddrinfo",
                        side_effect=AssertionError("must not resolve")):
            for host in ("metadata.google.internal", "metadata.goog",
                         "METADATA.GOOGLE.INTERNAL."):
                with self.assertRaises(UnsafeUrl, msg=host):
                    validate_url(f"http://{host}/computeMetadata/v1/")

    def test_metadata_hostname_resolving_to_imds_blocked(self):
        """GCE 内按域名解析到 IMDS 地址时逐地址判定仍拦。"""
        with mock.patch("socket.getaddrinfo",
                        _fake_getaddrinfo(["169.254.169.254"])):
            with self.assertRaises(UnsafeUrl):
                validate_url("http://metadata.internal.example/")

    def test_non_metadata_link_local_still_allowed(self):
        """防误伤：链路本地非元数据地址在内网放行下照常可用（如本地服务发现）。"""
        addrs = _resolve_validated("169.254.20.1", 80)
        self.assertIn("169.254.20.1", addrs)

    def test_blocklist_purely_local_no_network(self):
        """恒拦名单命中发生在解析/网络之前或本地判定层，不发起任何连接。"""
        from core.safe_http import _METADATA_BLOCKLIST
        import ipaddress
        self.assertIn(ipaddress.ip_address("169.254.169.254"), _METADATA_BLOCKLIST)
        self.assertIn(ipaddress.ip_address("fd00:ec2::254"), _METADATA_BLOCKLIST)


if __name__ == "__main__":
    unittest.main()
