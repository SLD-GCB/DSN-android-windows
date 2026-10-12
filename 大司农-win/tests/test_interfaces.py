"""网卡枚举/选择的单测（依赖本机真实网卡，弱断言 + 强约束）。"""
from dasinong_core.network import interfaces


def test_excludes_loopback_apipa_and_virtual():
    for i in interfaces.list_ipv4_interfaces():
        assert not i.ip.startswith("127.")
        assert not i.ip.startswith("169.254.")
        name = i.name.lower()
        assert "vmnet" not in name
        assert "vmware" not in name


def test_broadcast_address_is_directed():
    import ipaddress

    for i in interfaces.list_ipv4_interfaces():
        net = ipaddress.IPv4Network(f"{i.ip}/{i.netmask}", strict=False)
        assert i.broadcast == str(net.broadcast_address)
        # 广播地址不等同于主机地址
        assert i.broadcast != i.ip


def test_pick_primary_returns_non_virtual_when_available():
    ifaces = interfaces.list_ipv4_interfaces()
    if not ifaces:
        return  # 无网卡环境（如纯 CI）跳过
    primary = interfaces.pick_primary()
    assert primary is not None
    assert "vmnet" not in primary.name.lower()
    # 若存在非热点网卡，pick_primary 不应返回虚拟网卡
    assert primary.ip not in ("127.0.0.1",)


def test_broadcast_targets_include_limited_broadcast():
    targets = interfaces.broadcast_targets()
    assert "255.255.255.255" in targets
    assert all(isinstance(t, str) for t in targets)
    # 去重
    assert len(targets) == len(set(targets))


def test_hotspot_subnet_detection():
    from dasinong_core.network.interfaces import Iface

    hot = Iface("本地连接* 1", "192.168.137.5", "255.255.255.0", "192.168.137.255", True)
    assert hot.is_hotspot is True
