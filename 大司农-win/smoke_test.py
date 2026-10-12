"""大司农 Windows 端 headless 端到端自测（单机跑两个实例）。

用法：python smoke_test.py
覆盖：互相发现 -> 文本收发（后续扩展 文件/图片/文件夹/续传）。
退出码非 0 表示失败。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "后端"))

# 测试隔离：prefs（含「记住的设备地址」）指到临时文件，别污染用户真实配置，
# 否则跑完自测后真实端会去直连 127.0.0.1:37031 这种测试地址。
if not os.environ.get("DASINONG_PREFS"):
    os.environ["DASINONG_PREFS"] = str(
        Path(tempfile.mkdtemp(prefix="dasinong-smoke-prefs-")) / "config.json"
    )

from dasinong_core.config import Config, load_or_create_device_id  # noqa: E402
from dasinong_core.events import EventBus, EventType  # noqa: E402
from dasinong_core.models import ChatMessage, MsgKind  # noqa: E402
from dasinong_core.node import NodeEngine  # noqa: E402

TEST_UDP_PORT = 37030


def make_config(name: str, tcp_port: int, data_dir: Path, udp_port: int = TEST_UDP_PORT) -> Config:
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        udp_port=udp_port,
        tcp_port=tcp_port,
        data_dir=data_dir,
        device_name=name,
        device_id=load_or_create_device_id(data_dir),
    )
    cfg.ensure_dirs()
    return cfg


def make_engine(name: str, tcp_port: int, data_dir: Path,
                udp_port: int = TEST_UDP_PORT) -> NodeEngine:
    """建一台「干净设备」。

    真实世界里每台设备有**自己的配置**；而本自测在同一进程里跑多台，prefs 是共用的，
    于是「记住上次连过的设备」这条功能会让后面的实例去自动重连前面的实例
    （比如验证「错口令不该互相发现」时会被这个干扰）。所以每个实例先清空记忆。
    """
    engine = NodeEngine(make_config(name, tcp_port, data_dir, udp_port))
    engine._known_peers = {}
    return engine


def wait_for(predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Collector:
    def __init__(self, engine: NodeEngine) -> None:
        self.engine = engine
        self.messages = []
        self.peers_added = []
        self.transfers = []
        self.status_events = []
        self._bus: EventBus = engine.bus
        self._bus.subscribe(self._on_event)

    def _on_event(self, event) -> None:
        if event.type == EventType.MESSAGE:
            self.messages.append(event.data)
        elif event.type == EventType.PEER_ADDED:
            self.peers_added.append(event.data)
        elif event.type in (EventType.TRANSFER_ADDED, EventType.TRANSFER_UPDATED):
            self.transfers.append(event.data)
        elif event.type == EventType.MESSAGE_STATUS:
            # 状态变化走事件（引擎不再原地改 ChatMessage，UI 也不会因此漏刷新）
            self.status_events.append(event.data)


def main() -> int:
    # 口令模式：默认带口令（加密）；DASINONG_SMOKE_PLAIN=1 时用空口令（明文）跑
    plain = os.environ.get("DASINONG_SMOKE_PLAIN") == "1"
    passphrase = "" if plain else os.environ.get("DASINONG_SMOKE_PW", "smoke-pass-2026")

    tmp = Path(tempfile.mkdtemp(prefix="dasinong-smoke-"))
    a_dir = tmp / "A"
    b_dir = tmp / "B"
    a = make_engine("Win-A", 37031, a_dir)
    b = make_engine("Win-B", 37032, b_dir)
    a.set_passphrase(passphrase)
    b.set_passphrase(passphrase)
    ca, cb = Collector(a), Collector(b)
    mode = "明文（口令为空）" if plain else "加密（口令已设置）"

    try:
        print(f"[0] 隧道模式：{mode}")
        if not plain:
            assert a.fingerprint() == b.fingerprint(), "同口令的两端指纹必须一致"
            print(f"    本机指纹：{a.fingerprint()[:32]}…")
        a.start()
        b.start()

        print(f"[1] 互相发现 ... A={a.cfg.device_id[:8]} B={b.cfg.device_id[:8]}")
        ok = wait_for(
            lambda: len(a.peers()) >= 1 and len(b.peers()) >= 1, timeout=15
        )
        if not ok:
            print(f"    FAIL: A 看到 {len(a.peers())} 个, B 看到 {len(b.peers())} 个")
            return 1
        peer_in_b = a.peers()[0]
        print(f"    OK: A 看到 {peer_in_b.name} @ {peer_in_b.ip}")

        print("[2] 等待控制连接建立 ...")
        connected = wait_for(lambda: a.is_connected(peer_in_b.device_id), timeout=10)
        if not connected:
            print("    FAIL: 控制连接未建立（可能是 Windows 防火墙拦截入站）")
            return 1
        print("    OK: 控制连接已建立")

        print("[3] 文本消息 A -> B ...")
        text = "你好，大司农！hello 123"
        a.send_text(peer_in_b.device_id, text)
        got = wait_for(
            lambda: any(
                m.kind == MsgKind.TEXT and m.text == text for m in cb.messages
            ),
            timeout=10,
        )
        if not got:
            print(f"    FAIL: B 未收到文本，收到 {cb.messages}")
            return 1
        print(f"    OK: B 收到 {cb.messages[-1].text!r}")

        print("[4] 回复 B -> A ...")
        reply = "收到！回复测试 456"
        b.send_text(b.peers()[0].device_id, reply)
        got_reply = wait_for(
            lambda: any(
                m.kind == MsgKind.TEXT and m.text == reply for m in ca.messages
            ),
            timeout=10,
        )
        if not got_reply:
            print("    FAIL: A 未收到回复")
            return 1
        print(f"    OK: A 收到 {ca.messages[-1].text!r}")

        # 送达回执
        delivered = wait_for(
            lambda: any(
                s.get("status") == "delivered" for s in ca.status_events
            ),
            timeout=5,
        )
        print("[5] 送达回执：", "OK" if delivered else "FAIL",
              [s.get("status") for s in ca.status_events])
        if not delivered:
            return 1

        print("[6] 文件传输 A -> B (6 MiB) ...")
        payload = os.urandom(6 * 1024 * 1024)
        src = tmp / "payload.bin"
        src.write_bytes(payload)
        if not a.send_files(a.peers()[0].device_id, [str(src)]):
            print("    FAIL: send_files 返回空")
            return 1
        dst = b.cfg.receive_dir / "payload.bin"
        ok = wait_for(lambda: dst.exists() and dst.stat().st_size == len(payload), timeout=40)
        if not ok or hashlib.sha256(dst.read_bytes()).hexdigest() != hashlib.sha256(payload).hexdigest():
            print("    FAIL: 文件未送达或校验不一致")
            return 1
        print(f"    OK: 收到 {dst.name} ({dst.stat().st_size} 字节), sha256 一致")

        print("[7] 文件夹传输 A -> B ...")
        folder = tmp / "myfolder"
        (folder / "sub").mkdir(parents=True, exist_ok=True)
        (folder / "a.txt").write_text("你好，大司农 folder", encoding="utf-8")
        (folder / "sub" / "b.bin").write_bytes(os.urandom(200_000))
        if not a.send_files(a.peers()[0].device_id, [str(folder)]):
            print("    FAIL: 文件夹 send_files 返回空")
            return 1
        fa = b.cfg.receive_dir / "myfolder" / "a.txt"
        fb = b.cfg.receive_dir / "myfolder" / "sub" / "b.bin"
        ok = wait_for(lambda: fa.exists() and fb.exists(), timeout=40)
        if not ok:
            print("    FAIL: 文件夹未完整送达")
            return 1
        if fa.read_text(encoding="utf-8") != "你好，大司农 folder":
            print("    FAIL: 文本文件内容不一致")
            return 1
        if hashlib.sha256(fb.read_bytes()).hexdigest() != hashlib.sha256((folder / "sub" / "b.bin").read_bytes()).hexdigest():
            print("    FAIL: 二进制文件校验不一致")
            return 1
        print("    OK: 目录树重建正确，内容一致")

        print("[8] 断点续传 A -> B ...")
        from dasinong_core.network import resume as resumemod
        from dasinong_core.protocol.constants import CHUNK_SIZE

        big = os.urandom(4 * 1024 * 1024)
        big_name = "big.bin"
        big_sha = hashlib.sha256(big).hexdigest()
        half = len(big) // 2
        # 预置 B 的半成品：前半已收，后半为零（稀疏）
        part = b.cfg.receive_dir / f"{big_name}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "wb") as f:
            f.truncate(len(big))
            f.seek(0)
            f.write(big[:half])
        resumemod.save(part, resumemod.PartMeta(
            transfer_id="", file_id="preseed", name=big_name, size=len(big),
            sha256=big_sha, chunk_size=CHUNK_SIZE,
            total_chunks=(len(big) + CHUNK_SIZE - 1) // CHUNK_SIZE,
            received=[[0, half - 1]],
        ))
        srcbig = tmp / big_name
        srcbig.write_bytes(big)
        cb.transfers.clear()
        a.send_files(a.peers()[0].device_id, [str(srcbig)])
        final_big = b.cfg.receive_dir / big_name
        ok = wait_for(lambda: final_big.exists() and final_big.stat().st_size == len(big), timeout=40)
        if not ok or hashlib.sha256(final_big.read_bytes()).hexdigest() != big_sha:
            print("    FAIL: 续传最终文件不一致")
            return 1
        # 首个 recv 事件应显示已收 >= half（证明复用了已下载区间）
        first_recv = next(
            (t for t in cb.transfers if t.direction == "recv" and t.file and t.file.name == big_name),
            None,
        )
        resumed = first_recv is not None and first_recv.received >= half
        if not resumed:
            got = first_recv.received if first_recv else None
            print(f"    FAIL: 未走续传（首个 recv received={got}, 期望 >= {half}）")
            return 1
        print(f"    OK: 复用 {half} 字节，续传完成且 sha256 一致")

        print("[9] 多设备并发：A 与 C 同时把**同名**文件发给 B ...")
        # 三台设备：A、C 各自把 `same.bin`（内容不同）同时发给 B。
        # B 必须落成 two.bin 与 two (1).bin 两个文件，且内容分别对应 ——
        # 这正是「两台手机同时发同名照片给电脑」的场景。
        c2 = make_engine("Win-C2", 37033, tmp / "C2")
        c2.set_passphrase(passphrase)
        c2.start()
        try:
            if not wait_for(
                lambda: b.is_connected(a.cfg.device_id)
                and b.is_connected(c2.cfg.device_id)
                and c2.is_connected(b.cfg.device_id),
                timeout=30,
            ):
                print("    FAIL: 三台设备没两两建立控制连接")
                return 1
            payload_a = os.urandom(3 * 1024 * 1024)
            payload_c = os.urandom(3 * 1024 * 1024)
            dir_a, dir_c = tmp / "srcA", tmp / "srcC"
            dir_a.mkdir(parents=True, exist_ok=True)
            dir_c.mkdir(parents=True, exist_ok=True)
            (dir_a / "same.bin").write_bytes(payload_a)
            (dir_c / "same.bin").write_bytes(payload_c)
            print(f"      C2 发现 {[p.name for p in c2.peers()]} | 与 B 会话={c2.is_connected(b.cfg.device_id)}")
            if (not a.send_files(b.cfg.device_id, [str(dir_a / "same.bin")])
                    or not c2.send_files(b.cfg.device_id, [str(dir_c / "same.bin")])):
                print("    FAIL: send_files 返回空")
                return 1

            want = {hashlib.sha256(payload_a).hexdigest(),
                    hashlib.sha256(payload_c).hexdigest()}

            def both_arrived() -> bool:
                files = [p for p in b.cfg.receive_dir.glob("same*.bin") if p.is_file()]
                if len(files) != 2:
                    return False
                return {hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == want

            if not wait_for(both_arrived, timeout=60):
                got = sorted(p.name for p in b.cfg.receive_dir.glob("same*"))
                print(f"    FAIL: 同名并发未正确落盘，B 收到 {got}")
                for eng, tag in ((a, "A"), (c2, "C"), (b, "B")):
                    for t in eng.transfers():
                        if t.file and t.file.name == "same.bin":
                            print(f"      [{tag}] {t.direction} {t.state} "
                                  f"sent={t.sent} recv={t.received} err={t.error}")
                return 1
            names = sorted(p.name for p in b.cfg.receive_dir.glob("same*.bin"))
            print(f"    OK: {names[0]} / {names[1]} 内容各自正确（未互相覆盖）")
        finally:
            c2.stop()

        print("\n[10] 口令不一致必须互相看不见（防冒充）...")
        if plain:
            print("    （明文模式跳过）")
        else:
            # 用另一个 UDP 端口隔离，避免和 A/B 互相发现
            NP = 37040
            c = make_engine("Win-C", 37041, tmp / "C", udp_port=NP)
            d = make_engine("Win-D", 37042, tmp / "D", udp_port=NP)
            c.set_passphrase("完全不同的口令-xyz")
            d.set_passphrase(passphrase)
            try:
                c.start()
                d.start()
                time.sleep(8)
                seen = len(c.peers()) + len(d.peers())
                if seen != 0:
                    print(f"    FAIL: 错口令的两端不应互相发现，实际看到 {seen} 台")
                    return 1
                print("    OK: 口令不一致 → 双方都看不到对方（广播签名被拒）")
            finally:
                c.stop()
                d.stop()

        print("\nALL PASSED")
        return 0
    finally:
        a.stop()
        b.stop()
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
