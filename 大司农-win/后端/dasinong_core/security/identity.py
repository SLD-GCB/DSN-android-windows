"""本机**长期身份**：随机 P-256 私钥 + 自签证书，私钥经 `SecretStore` 加密后落盘。

与 v3 的关键差别：v3 的身份由**口令派生**（同口令 = 同身份、可互相冒充）；v4 起是
**每设备随机**、彼此不同 —— 这样「配对」（识别 / 授权 / 吊销某台具体设备）才有意义。

- 指纹 = 公钥 **SPKI DER 的 SHA-256**（小写 hex）—— 与 v3 同一算法，便于复用与比对。
- 身份丢失（换机 / 清数据 / 换 Windows 用户 / 系统密钥库被重置）→ 生成新身份，
  调用方应提示"本机身份已更换，对端需要重新配对"。
"""
from __future__ import annotations

import datetime
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .secretstore import SecretStore, SecretStoreError

#: P-256 曲线阶（n）
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)

#: 私钥文件名（内容经 SecretStore 加密）
KEY_FILE = "identity.key"
#: 自签证书 CN
CERT_CN = "dasinong-identity"
#: 证书有效期（天）
CERT_DAYS = 3650


def seed_to_key(seed: bytes) -> ec.EllipticCurvePrivateKey:
    """种子 → P-256 私钥：`d = (int(seed) mod (n-1)) + 1`。

    刻意沿用 v3 口令派生用的同一条取模路径（只是材料来源从口令变成随机种子）：
    曲线代码不变、跨语言一致、且能给出确定的测试向量。
    """
    d = (int.from_bytes(seed, "big") % (_P256_ORDER - 1)) + 1
    return ec.derive_private_key(d, ec.SECP256R1())


def spki_of(public_key) -> bytes:
    """公钥的 SPKI DER 编码（跨语言一致的入参与指纹原料）。"""
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def fingerprint_of(public_key) -> str:
    """公钥指纹 = SPKI DER 的 SHA-256（小写 hex）。⚠ 不是整张证书的哈希。"""
    return hashlib.sha256(spki_of(public_key)).hexdigest()


def _build_cert(key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_CN)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .sign(key, hashes.SHA256())
    )


@dataclass
class Identity:
    """本机身份（私钥 + 由它签出的自签证书）。"""

    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_seed(cls, seed: bytes) -> "Identity":
        """由固定种子构造（测试向量的锚点）。"""
        key = seed_to_key(seed)
        return cls(key=key, cert=_build_cert(key))

    @classmethod
    def generate(cls) -> "Identity":
        """随机生成一台新设备的身份。"""
        return cls.from_seed(os.urandom(32))

    # ---------------------------------------------------------------- 属性
    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.key.public_key())

    @property
    def spki_der(self) -> bytes:
        return spki_of(self.key.public_key())

    def private_pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    # ---------------------------------------------------------------- 签名
    def sign(self, message: bytes) -> bytes:
        """用本机私钥签名（ECDSA-SHA256，DER 编码签名；与 Kotlin 侧一致）。"""
        return self.key.sign(message, ec.ECDSA(hashes.SHA256()))

    @staticmethod
    def verify(spki_der: bytes, message: bytes, signature: bytes) -> bool:
        """用对方声明的公钥验签。任何异常都当"验证失败"，不往外抛。"""
        try:
            pub = serialization.load_der_public_key(spki_der)
            pub.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False


def load_or_create(data_dir: Path, store: SecretStore) -> Tuple[Identity, bool]:
    """读取已有身份；没有/解不开就新建并落盘。

    返回 `(身份, 是否沿用已有)`。`False` 表示**新生成**：可能是首次运行，也可能是
    老身份丢了（换机/清数据/换 Windows 用户）—— 后者需要提示用户重新配对。
    """
    path = Path(data_dir) / KEY_FILE
    if path.is_file():
        try:
            pem = store.unprotect(path.read_bytes())
            key = serialization.load_pem_private_key(pem, password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return Identity(key=key, cert=_build_cert(key)), True
        except (SecretStoreError, ValueError, TypeError):
            pass  # 解不开/文件损坏 → 当作没有身份
    ident = Identity.generate()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(store.protect(ident.private_pem()))
    except (OSError, SecretStoreError):
        pass  # 存不下来也先能用（本次会话有效），下次启动会再生成
    return ident, False
