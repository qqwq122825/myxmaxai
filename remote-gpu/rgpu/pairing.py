"""Account-free pairing: endpoint, random access key and pinned TLS identity."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re
import secrets
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import check_token
from .paths import reports_directory


@dataclass(frozen=True, repr=False)
class ConnectionKey:
    """One copyable credential, independent of the server's address.

    Carries the bearer secret and pinned TLS identity together so the user only
    enters an address and a Key. No certificate bootstrap or skipped validation.
    """
    key: str
    fingerprint: str

    def __post_init__(self):
        check_token(self.key)
        if not isinstance(self.fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise ValueError("连接 Key 中的服务器身份无效。")

    def encode(self):
        payload = bytes.fromhex(self.fingerprint) + self.key.encode("ascii")
        return "RGK1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value):
        try:
            value = value.strip()
            if not value.startswith("RGK1.") or len(value) > 512:
                raise ValueError()
            raw = value[5:]
            payload = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
            return cls(payload[32:].decode("ascii"), payload[:32].hex())
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise ValueError("连接 Key 无效，请复制新版服务器的完整 Key（RGK1. 开头）。") from None


@dataclass(frozen=True, repr=False)
class Pairing:
    host: str
    port: int
    fingerprint: str
    key: str

    def __post_init__(self):
        if not isinstance(self.host, str):
            raise ValueError("服务器地址必须是文字。")
        host = self.host.strip()
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_unspecified or ip.is_multicast:
                raise ValueError("配对地址须为可连接的服务器地址，不能填 0.0.0.0。")
        except ValueError as ex:
            if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                raise ValueError("请填服务器公网 IP 或域名，不含 http:// 和端口。") from ex
            # Strings resembling an IP should not silently become a DNS name.
            if re.fullmatch(r"[0-9.]+", host):
                raise ValueError("服务器 IP 不正确。") from ex
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("公网端口须为 1–65535。")
        if not isinstance(self.fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise ValueError("配对码的服务器证书指纹不正确。")
        check_token(self.key)
        object.__setattr__(self, "host", host)

    @property
    def endpoint(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"https://{host}:{self.port}"

    def encode(self):
        data = {"v": 1, "h": self.host, "p": self.port, "f": self.fingerprint, "k": self.key}
        raw = json.dumps(data, separators=(",", ":")).encode("ascii")
        return "RGPU1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value):
        try:
            value = value.strip()
            if not value.startswith("RGPU1.") or len(value) > 2048:
                raise ValueError()
            raw = value[6:]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
            if data["v"] != 1:
                raise ValueError()
            return cls(data["h"], data["p"], data["f"], data["k"])
        except (ValueError, TypeError, KeyError, UnicodeError) as ex:
            # Do not include the bearer key / full pasted code in errors.
            raise ValueError("配对码无效，请复制远程端生成的完整 RGPU1 配对码。") from None


@dataclass(repr=False)
class ServerIdentity:
    certificate: Path
    private_key: Path
    fingerprint: str
    key: str

    def ssl_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certificate, self.private_key)
        return context

    def pairing(self, host, port):
        return Pairing(host, port, self.fingerprint, self.key)

    @property
    def connection_key(self):
        return ConnectionKey(self.key, self.fingerprint).encode()


def load_identity(directory=None):
    root = Path(directory) if directory else reports_directory().parent / "direct-identity"
    root.mkdir(parents=True, exist_ok=True)
    certificate, private_key, access_key = root / "server.pem", root / "server-key.pem", root / "access.key"
    if certificate.exists() != private_key.exists():
        raise RuntimeError("直连证书文件不完整，请恢复该服务器的 direct-identity 目录后重试。")
    if not certificate.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FaceLive Remote GPU")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
        private_key.chmod(0o600)
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(certificate.read_bytes())
    if not access_key.exists():
        with access_key.open("x", encoding="ascii") as file:
            file.write(secrets.token_urlsafe(32))
        access_key.chmod(0o600)
    return ServerIdentity(certificate, private_key, cert.fingerprint(hashes.SHA256()).hex(),
                          check_token(access_key.read_text(encoding="ascii")))
