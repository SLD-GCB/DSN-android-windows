"""网卡枚举与选择。

关键点（本机实测有多张 VMware 虚拟网卡 + 一批 169.254.* 的 down 网卡）：
- 排除 127.*（环回）、169.254.*（APIPA）、以及 VMware/VirtualBox 等虚拟网卡；
- 优先命中「热点网段」（192.168.137/43/42/172.20.10）的接口；
- 发送广播时必须把 socket 绑定到目标网卡的源 IP，否则可能从错误的网卡出包。
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

import psutil

from ..protocol.constants import HOTSPOT_BROADCASTS

HOTSPOT_SUBNETS = [
    ipaddress.ip_network(n)
    for n in ("192.168.137.0/24", "192.168.43.0/24", "192.168.42.0/24", "172.20.10.0/24")
]

_VIRTUAL_HINTS = (
    "vmware", "vmnet", "virtualbox", "vbox", "hyper-v", "vethernet",
    "docker", "wsl", "tailscale", "zerotier", "loopback", "bluetooth",
)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Iface:
    name: str
    ip: str
    netmask: str
    broadcast: str
    is_hotspot: bool = False


def _is_virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _VIRTUAL_HINTS)


def list_ipv4_interfaces(include_virtual: bool = False) -> List[Iface]:
    """枚举可用 IPv4 接口（排除环回/APIPA/虚拟网卡）。"""
    result: List[Iface] = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        if not include_virtual and _is_virtual(name):
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = a.address
            if not isinstance(ip, str) or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            mask = a.netmask or "255.255.255.0"
            try:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
            except ValueError:
                continue
            is_hot = any(net.subnet_of(h) or net.overlaps(h) for h in HOTSPOT_SUBNETS)
            result.append(
                Iface(
                    name=name,
                    ip=ip,
                    netmask=mask,
                    broadcast=str(net.broadcast_address),
                    is_hotspot=is_hot,
                )
            )
    return result


def pick_primary(ifaces: Optional[List[Iface]] = None) -> Optional[Iface]:
    """选择主用接口：热点网段优先，其次按网卡名排序。"""
    items = ifaces if ifaces is not None else list_ipv4_interfaces()
    if not items:
        return None
    return sorted(items, key=lambda i: (not i.is_hotspot, i.name))[0]


def broadcast_targets(ifaces: Optional[List[Iface]] = None, include_hotspot_fallback: bool = True) -> List[str]:
    """所有应发送定向广播的目标地址（去重）。"""
    items = ifaces if ifaces is not None else list_ipv4_interfaces()
    out: List[str] = []
    for i in items:
        if i.broadcast not in out:
            out.append(i.broadcast)
    if include_hotspot_fallback:
        for b in HOTSPOT_BROADCASTS:
            if b not in out:
                out.append(b)
    if "255.255.255.255" not in out:
        out.append("255.255.255.255")
    return out


def open_sender(src_ip: str, broadcast: bool = True) -> socket.socket:
    """创建绑定到指定源 IP 的 UDP 发送 socket（强制从该网卡出包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if broadcast:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((src_ip, 0))
    return s


def _decode_console(data: Optional[bytes]) -> str:
    """解码控制台输出：中文 Windows 的 route/ipconfig 多为 GBK(mbcs)。"""
    if not data:
        return ""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def default_gateway() -> Optional[str]:
    """读取系统默认网关（Windows 解析 route print；其他平台返回 None）。"""
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return None
    best: Optional[str] = None
    best_metric: Optional[int] = None
    for line in _decode_console(proc.stdout).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw = parts[2]
            try:
                metric = int(parts[4])
            except ValueError:
                metric = 9999
            if not gw or gw == "0.0.0.0":
                continue
            if best is None or metric < (best_metric or 9999):
                best, best_metric = gw, metric
    return best
