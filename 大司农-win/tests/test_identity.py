"""每设备长期身份：固定向量（跨语言锚点）+ 签名/验签 + 加密落盘。"""
from dasinong_core.security import identity as ident_mod
from dasinong_core.security.identity import Identity
from dasinong_core.security.secretstore import PlaintextStore

SEED = bytes(range(32))

#: ⚠ **跨语言锚点**：必须与 Android `IdentityTest.kt` 里的同名校验值一致
#: （它证明两端的 P-256 + SPKI DER 编码完全一致）
FP = "2c69f1bdbe45a8a6ab3b6abd66f8d4632abfb434998d4f3b6686972d5ea3c881"
SPKI_LEN = 91
SPKI_PREFIX = "3059301306072a8648ce3d02"


class _XorStore:
    """假 store：证明私钥确实经过了"加密存储"这一层，而不是裸写 PEM。"""

    name = "xor"

    @staticmethod
    def protect(plain: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in plain)

    @staticmethod
    def unprotect(blob: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in blob)


def test_from_seed_vector():
    ident = Identity.from_seed(SEED)
    assert len(ident.spki_der) == SPKI_LEN
    assert ident.spki_der[:12].hex() == SPKI_PREFIX
    assert ident.fingerprint == FP


def test_identity_is_stable_and_distinct():
    assert Identity.from_seed(SEED).fingerprint == FP          # 同种子 → 同身份
    assert Identity.generate().fingerprint != FP               # 随机身份各不相同


def test_sign_and_verify():
    a = Identity.from_seed(SEED)
    b = Identity.generate()
    sig = a.sign(b"hello")
    assert Identity.verify(a.spki_der, b"hello", sig) is True
    assert Identity.verify(a.spki_der, b"hello!", sig) is False                 # 改消息
    assert Identity.verify(b.spki_der, b"hello", sig) is False                  # 换公钥
    assert Identity.verify(a.spki_der, b"hello", sig[:-1] + b"\x00") is False   # 改签名
    assert Identity.verify(b"not-a-key", b"hello", sig) is False                # 垃圾公钥不抛异常


def test_load_or_create_roundtrip(tmp_path):
    store = PlaintextStore()
    a, reused = ident_mod.load_or_create(tmp_path, store)
    assert reused is False
    b, reused2 = ident_mod.load_or_create(tmp_path, store)
    assert reused2 is True
    assert a.fingerprint == b.fingerprint


def test_private_key_is_wrapped_by_store(tmp_path):
    a, _ = ident_mod.load_or_create(tmp_path, _XorStore())
    raw = (tmp_path / ident_mod.KEY_FILE).read_bytes()
    assert b"PRIVATE KEY" not in raw          # 落盘的是密文，不是裸 PEM
    b, reused = ident_mod.load_or_create(tmp_path, _XorStore())
    assert reused is True and a.fingerprint == b.fingerprint


def test_broken_identity_file_regenerates(tmp_path):
    store = PlaintextStore()
    ident_mod.load_or_create(tmp_path, store)
    (tmp_path / ident_mod.KEY_FILE).write_bytes(b"garbage")
    _, reused = ident_mod.load_or_create(tmp_path, store)
    assert reused is False                    # 解不开 → 生成新身份（需重新配对）
