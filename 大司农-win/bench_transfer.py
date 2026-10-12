"""大文件并发传输实测：对比「TCP 并发」= 1 / 3 / 6 的耗时。

⚠ 这是**本机（127.0.0.1）**压测：瓶颈在磁盘与 Python 分块，不在网线，
所以并发带来的提升会比真机局域网**小**；但能验证「并发跑得通、且文件逐字节一致」。

用法（在 大司农-win 目录下）：
    python bench_transfer.py            # 默认 200 MB
    python bench_transfer.py 500        # 500 MB
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "后端"))
os.environ.setdefault("DASINONG_PAIR_AUTO", "1")          # 无头环境：自动接受配对

from dasinong_core.config import Config                    # noqa: E402
from dasinong_core.events import EventBus                  # noqa: E402
from dasinong_core.node import NodeEngine                  # noqa: E402
from dasinong_core.store import Store                      # noqa: E402


def _engine(tmp: str, tag: str, name: str, tcp: int, udp: int, parallel: int) -> NodeEngine:
    data = Path(tmp) / f"{tag}{name}"
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        udp_port=udp, tcp_port=tcp, data_dir=data,
        device_name=name, device_id=f"bench-{tag}-{name}-0000",
    )
    cfg.ensure_dirs()
    e = NodeEngine(cfg, bus=EventBus())
    e.attach_store(Store(data / "t.db"))
    e._known_peers = {}                 # 隔离：别用上次记住的地址
    e.transfer.parallel = parallel      # ★ 被测项：并发数据连接数
    return e


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def run(mb: int, parallel: int, base: int) -> tuple:
    tmp = tempfile.mkdtemp(prefix=f"bench-p{parallel}-")
    a = _engine(tmp, "z", "A", base, base + 1, parallel)
    b = _engine(tmp, "z", "B", base + 2, base + 3, parallel)
    a.start()
    b.start()
    try:
        ok, msg = a.connect_by_ip("127.0.0.1", b.cfg.tcp_port)
        if not ok:
            raise RuntimeError(f"连不上：{msg}")
        deadline = time.time() + 20
        while not a.is_paired(b.cfg.device_id) and time.time() < deadline:
            time.sleep(0.1)

        src = Path(tmp) / "big.bin"
        blk = os.urandom(1 << 20)
        left = mb * 1024 * 1024
        with open(src, "wb") as f:
            while left > 0:
                n = min(len(blk), left)
                f.write(blk[:n])
                left -= n
        size = src.stat().st_size

        t0 = time.time()
        if not a.send_files(b.cfg.device_id, [str(src)]):
            raise RuntimeError("send_files 返回空（没连上/没配对）")

        dst = Path(b.cfg.receive_dir) / "big.bin"
        while time.time() - t0 < 600:
            if dst.is_file() and dst.stat().st_size == size:
                time.sleep(0.5)          # 等 fsync / sha256 校验走完
                break
            time.sleep(0.05)
        dt = time.time() - t0
        same = dst.is_file() and _sha(dst) == _sha(src)
        return dt, size, same, (dst.stat().st_size if dst.is_file() else -1)
    finally:
        for e in (a, b):
            try:
                e.stop()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    mb = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    print(f"== 本机大文件传输实测：{mb} MB ==")
    print(f"{'并发':>4}  {'耗时(秒)':>9}  {'速度(MB/s)':>10}  完整(sha256)")
    results = {}
    for i, par in enumerate((1, 3, 6)):
        dt, size, same, got = run(mb, par, 48700 + i * 10)
        speed = (size / (1024 * 1024)) / dt if dt > 0 else 0
        results[par] = (dt, speed, same)
        print(f"{par:>4}  {dt:>9.2f}  {speed:>10.1f}  {'一致 ✅' if same else f'不一致 ❌ (收到 {got} B)'}")
    print()
    if 1 in results and 3 in results:
        d1, s1, _ = results[1]
        d3, s3, _ = results[3]
        if s1 > 0:
            print(f"并发 3 / 并发 1 速度比：{s3 / s1:.2f}×")
    if 1 in results and 6 in results:
        d1, s1, _ = results[1]
        d6, s6, _ = results[6]
        if s1 > 0:
            print(f"并发 6 / 并发 1 速度比：{s6 / s1:.2f}×")
    print("⚠ 本机回环：瓶颈是磁盘/CPU，不是网线 → 真机局域网里并发的收益通常比这里更明显。")
    all_ok = all(v[2] for v in results.values())
    print("结论：", "所有并发档位文件均逐字节一致 ✅" if all_ok else "有档位文件不一致 ❌")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
