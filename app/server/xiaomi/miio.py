# -*- coding: utf-8 -*-
"""手写 miIO 局域网协议（UDP 54321）。

包结构（32B 头部约定）:
  header 16B: magic u16=0x2131 | length u16 | unknown u32 | device_id u32 | timestamp u32
  data:       checksum 16B | AES-128-CCM 密文（尾部附 16B MAC）
  checksum = MD5(token || 密文含MAC)；空负载时 checksum = token
  nonce = MD5(token)[:4] + 8字节零 + timestamp(4B big-endian)
  length = 32 + len(密文含MAC)

参考 rytilahti/python-miio 协议实现（GPLv3 项目，此处为独立实现）。
"""
import hashlib
import json
import logging
import socket
import struct
import time

from Crypto.Cipher import AES

log = logging.getLogger(__name__)

MAGIC = 0x2131
DEFAULT_PORT = 54321


class MiIOError(Exception):
    pass


def _encrypt(token: bytes, plaintext: bytes, ts: int) -> bytes:
    nonce = hashlib.md5(token).digest()[:4] + b"\x00" * 8 + struct.pack(">I", ts)
    cipher = AES.new(token, AES.MODE_CCM, nonce=nonce, mac_len=16)
    return cipher.encrypt(plaintext) + cipher.digest()


def _decrypt(token: bytes, payload_with_mac: bytes, ts: int) -> bytes:
    nonce = hashlib.md5(token).digest()[:4] + b"\x00" * 8 + struct.pack(">I", ts)
    cipher = AES.new(token, AES.MODE_CCM, nonce=nonce, mac_len=16)
    return cipher.decrypt_and_verify(payload_with_mac[:-16], payload_with_mac[-16:])


def _checksum(token: bytes, encrypted: bytes) -> bytes:
    m = hashlib.md5()
    m.update(token)
    m.update(encrypted)
    return m.digest()


def _token_bytes(token: str) -> bytes:
    if isinstance(token, bytes):
        return token
    t = token.strip()
    try:
        return bytes.fromhex(t)
    except ValueError:
        return t.encode()  # 32 字符非 hex 的 token 罕见，兜底


def hello(sock: socket.socket, addr: str, port: int = DEFAULT_PORT):
    """发 hello 包，返回 (device_id, timestamp) 或 None。"""
    ts = int(time.time())
    packet = struct.pack(">HHIII", MAGIC, 32, 0, 0, ts)
    sock.sendto(packet, (addr, port))
    sock.settimeout(2.5)
    try:
        data, _ = sock.recvfrom(1024)
    except socket.timeout:
        return None
    magic, length, unknown, device_id, _ts = struct.unpack(">HHIII", data[:16])
    return device_id


def send(sock: socket.socket, addr: str, token: str, method: str,
         params=None, device_id: int = None, port: int = DEFAULT_PORT,
         timeout: float = 4.0, request_id: int = None) -> dict:
    """发送 miIO 命令并等待响应（解密后的 JSON dict）。"""
    ts = int(time.time())
    payload = json.dumps({"id": request_id or ts % 100000,
                          "method": method, "params": params or []},
                         separators=(",", ":")).encode()
    tb = _token_bytes(token)
    encrypted = _encrypt(tb, payload, ts)
    length = 32 + len(encrypted)
    header = struct.pack(">HHIII", MAGIC, length, 0, device_id or 0, ts)
    packet = header + _checksum(tb, encrypted) + encrypted
    sock.sendto(packet, (addr, port))
    sock.settimeout(timeout)
    try:
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        raise MiIOError("设备无响应（%s:%s）" % (addr, port))
    magic, rlen, _unknown, _did, rts = struct.unpack(">HHIII", data[:16])
    body = data[16:]
    if len(body) == 16:  # 只有 checksum，无负载
        raise MiIOError("设备返回空负载")
    checksum, encrypted = body[:16], body[16:]
    if _checksum(tb, encrypted) != checksum:
        raise MiIOError("校验和不匹配（token 可能错误）")
    try:
        plaintext = _decrypt(tb, encrypted, rts)
    except ValueError as e:
        raise MiIOError("解密失败: %s（token 或时钟问题）" % e)
    try:
        return json.loads(plaintext)
    except json.JSONDecodeError:
        raise MiIOError("响应不是 JSON: %r" % plaintext[:80])


def discover(timeout: float = 2.0, broadcast: str = "255.255.255.255"):
    """广播发现。返回 [(device_id, ip)]。"""
    results = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        s.sendto(struct.pack(">HHIII", MAGIC, 32, 0, 0, int(time.time())),
                 (broadcast, DEFAULT_PORT))
        while True:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except socket.timeout:
                break
            _magic, _length, _unknown, device_id, _ts = struct.unpack(">HHIII", data[:16])
            results[ip] = device_id
    finally:
        s.close()
    return results
