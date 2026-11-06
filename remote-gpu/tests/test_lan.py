from urllib.parse import parse_qs, urlsplit

import cv2
import pytest

from rgpu.lan import (checked_port, is_lan_ip, pairing_url, parse_addresses,
                      qr_image, rotate_key)
from rgpu.lan_selftest import exercise
from rgpu.pairing import ConnectionKey, load_identity


@pytest.mark.parametrize("address", ["192.168.1.9", "10.10.1.2", "172.16.0.5", "172.31.255.2", "169.254.1.2"])
def test_accept_lan_addresses(address):
    assert is_lan_ip(address)


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "100.123.190.3", "8.8.8.8", "172.32.0.1", "localhost", "192.168.1.2:8767"])
def test_do_not_advertise_public_vpn_or_loopback_addresses(address):
    assert not is_lan_ip(address)


@pytest.mark.parametrize("sample", range(5))
def test_phone_qr_schema_and_actual_decode(tmp_path, sample):
    identity = load_identity(tmp_path)
    addresses = parse_addresses("192.168.1.3,10.0.0.5,192.168.1.3")
    url = pairing_url(addresses, 8767, identity.connection_key, "工作室电脑")
    assert urlsplit(url).scheme == "aimoyan" and urlsplit(url).netloc == "connect"
    query = parse_qs(urlsplit(url).query)
    assert query["addr"] == ["192.168.1.3:8767,10.0.0.5:8767"]
    assert query["key"] == [identity.connection_key] and query["mode"] == ["lan"]
    assert query["name"] == ["工作室电脑"]
    assert cv2.QRCodeDetector().detectAndDecode(qr_image(url, size=500))[0] == url
    assert qr_image(url).shape[0] <= 270


@pytest.mark.parametrize("port", ["abc", 0, 65536, 80, None])
def test_invalid_port(port):
    with pytest.raises(ValueError):
        checked_port(port)


def test_rotation_invalidates_old_key_but_keeps_certificate(tmp_path):
    identity = load_identity(tmp_path)
    updated = rotate_key(identity)
    assert updated.fingerprint == identity.fingerprint and updated.key != identity.key
    assert ConnectionKey.decode(updated.connection_key).key == updated.key
    assert load_identity(tmp_path).key == updated.key


@pytest.mark.parametrize("size", [(720, 1280), (1080, 1920)])
async def test_phone_protocol_tls_decode_auth_busy_and_reconnect(tmp_path, size):
    result = await exercise(tmp_path, *size)
    assert result["ok"] and result["input_frames"] == 6
