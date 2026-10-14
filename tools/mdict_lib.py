"""Minimal, dependency-free MDict (.mdx/.mdd, engine 1.2/2.0) reader.

Supports Encrypted="0"/"2" (key-info scrambling) and zlib/uncompressed blocks.
Regcode-locked (Encrypted bit 0) and LZO-compressed files raise MDictError.
"""
from __future__ import annotations

import html
import re
import struct
import zlib

__all__ = ["MDict", "MDictError"]


class MDictError(Exception):
    pass


# ---------------- RIPEMD-128 (key for the Encrypted="2" key-block-info) ----------------
_R = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
      7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
      3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
      1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2]
_RP = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
       6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
       15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
       8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14]
_S = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
      7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
      11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
      11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12]
_SP = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
       9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
       9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
       15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8]
_M = 0xFFFFFFFF


def _f(j, x, y, z):
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (z & ~x & _M)
    if j < 48:
        return (x | (~y & _M)) ^ z
    return (x & z) | (y & ~z & _M)


def _rol(s, x):
    return ((x << s) | (x >> (32 - s))) & _M


def ripemd128(msg: bytes) -> bytes:
    k = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC)
    kp = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x00000000)
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    ml = len(msg)
    msg = msg + b"\x80" + b"\x00" * ((55 - ml) % 64) + struct.pack("<Q", ml * 8)
    for off in range(0, len(msg), 64):
        x = struct.unpack("<16L", msg[off:off + 64])
        a, b, c, d = h
        ap, bp, cp, dp = h
        for j in range(64):
            t = _rol(_S[j], (a + _f(j, b, c, d) + x[_R[j]] + k[j // 16]) & _M)
            a, d, c, b = d, c, b, t
            t = _rol(_SP[j], (ap + _f(63 - j, bp, cp, dp) + x[_RP[j]] + kp[j // 16]) & _M)
            ap, dp, cp, bp = dp, cp, bp, t
        t = (h[1] + c + dp) & _M
        h[1] = (h[2] + d + ap) & _M
        h[2] = (h[3] + a + bp) & _M
        h[3] = (h[0] + b + cp) & _M
        h[0] = t
    return struct.pack("<4L", *h)


assert ripemd128(b"").hex() == "cdf26213a150dc3ecb610f18f6b38b46"
assert ripemd128(b"abc").hex() == "c14a12199c66e4ba84636b0f69144c77"


def _descramble(block: bytes) -> bytes:
    key = ripemd128(block[4:8] + struct.pack("<L", 0x3695))
    data = bytearray(block[8:])
    prev = 0x36
    for i, byte in enumerate(data):
        t = ((byte >> 4) | (byte << 4)) & 0xFF
        t = t ^ prev ^ (i & 0xFF) ^ key[i % len(key)]
        prev = byte
        data[i] = t
    return block[:8] + bytes(data)


def _decompress(block: bytes) -> bytes:
    ctype = block[:4]
    if ctype == b"\x02\x00\x00\x00":
        return zlib.decompress(block[8:])
    if ctype == b"\x00\x00\x00\x00":
        return block[8:]
    if ctype == b"\x01\x00\x00\x00":
        raise MDictError("LZO-compressed block (needs python-lzo)")
    raise MDictError(f"unknown block compression {ctype.hex()}")


class MDict:
    """Random-free sequential reader. `items()` streams (key, payload) in file order."""

    def __init__(self, path: str):
        self.path = path
        self.is_mdd = path.lower().endswith(".mdd")
        with open(path, "rb") as f:
            hlen = struct.unpack(">I", f.read(4))[0]
            htext = f.read(hlen).decode("utf-16le", errors="replace").rstrip("\x00")
            f.read(4)
            self.header = {k: html.unescape(v) for k, v in re.findall(r'(\w+)="(.*?)"', htext, re.S)}
            version = float(self.header.get("GeneratedByEngineVersion", "2.0") or "2.0")
            enc_attr = self.header.get("Encrypted", "0") or "0"
            enc = {"Yes": 1, "No": 0}.get(enc_attr)
            self.encrypted = int(enc_attr) if enc is None else enc
            if self.encrypted & 1:
                raise MDictError("regcode-locked MDict (Encrypted bit 0)")
            encoding = (self.header.get("Encoding") or "UTF-8").upper()
            if encoding in ("GBK", "GB2312"):
                encoding = "GB18030"
            self.utf16 = self.is_mdd or encoding.startswith("UTF-16")
            self.encoding = "utf-16le" if self.utf16 else encoding
            v2 = version >= 2.0
            self._nfmt, self._nw = (">Q", 8) if v2 else (">I", 4)
            nfmt2 = ">" + self._nfmt[1] * 2

            if v2:
                nkb, self.num_entries, _dsz, kbi_size, kb_size = struct.unpack(">5Q", f.read(40))
                f.read(4)
            else:
                nkb, self.num_entries, kbi_size, kb_size = struct.unpack(">4I", f.read(16))
            kbi = f.read(kbi_size)
            if v2:
                if self.encrypted & 2:
                    kbi = _descramble(kbi)
                kbi = _decompress(kbi)
            bfmt, bw, term = (">H", 2, 1) if v2 else (">B", 1, 0)
            mult = 2 if self.utf16 else 1
            sizes, i = [], 0
            for _ in range(nkb):
                i += self._nw
                hs = struct.unpack(bfmt, kbi[i:i + bw])[0]
                i += bw + (hs + term) * mult
                ts = struct.unpack(bfmt, kbi[i:i + bw])[0]
                i += bw + (ts + term) * mult
                sizes.append(struct.unpack(nfmt2, kbi[i:i + 2 * self._nw])[0])
                i += 2 * self._nw
            kb = f.read(kb_size)
            self.keys: list[tuple[int, str]] = []
            pos = 0
            for cs in sizes:
                self.keys.extend(self._split_keys(_decompress(kb[pos:pos + cs])))
                pos += cs

            nrb = struct.unpack(self._nfmt, f.read(self._nw))[0]
            f.read(self._nw * 3)
            self._rinfo = [struct.unpack(nfmt2, f.read(2 * self._nw)) for _ in range(nrb)]
            self._rdata_start = f.tell()

    def _split_keys(self, block: bytes):
        out, i, n, nw, nfmt = [], 0, len(block), self._nw, self._nfmt
        while i < n:
            kid = struct.unpack(nfmt, block[i:i + nw])[0]
            j = i + nw
            if self.utf16:
                k = j
                while True:
                    k = block.find(b"\x00\x00", k)
                    if k == -1:
                        k = n
                        break
                    if (k - j) % 2 == 0:
                        break
                    k += 1
                out.append((kid, block[j:k].decode("utf-16le", errors="replace")))
                i = k + 2
            else:
                k = block.find(b"\x00", j)
                if k == -1:
                    k = n
                out.append((kid, block[j:k].decode(self.encoding, errors="replace")))
                i = k + 1
        return out

    def __len__(self):
        return len(self.keys)

    def items(self):
        """Yield (key, bytes) in record order; MDX payloads keep their trailing NUL stripped."""
        keys = sorted(self.keys)
        total = sum(d for _c, d in self._rinfo)
        with open(self.path, "rb") as f:
            f.seek(self._rdata_start)
            blocks = iter(self._rinfo)
            buf, buf_start = b"", 0
            for idx, (off, key) in enumerate(keys):
                end = keys[idx + 1][0] if idx + 1 < len(keys) else total
                while buf_start + len(buf) < end:
                    cs, _ds = next(blocks)
                    drop = off - buf_start
                    if drop > 0:
                        buf, buf_start = buf[drop:], off
                    buf += _decompress(f.read(cs))
                payload = buf[off - buf_start:end - buf_start]
                if not self.is_mdd:
                    payload = payload.rstrip(b"\x00")
                yield key, payload
