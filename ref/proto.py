"""BHTTP/1 shared framing helpers (reference, used by bserve and bcurl)."""
import struct

PREFACE = b"BHT1"
HDR_LEN = 8
MAX_PAYLOAD = 16384

T_HEADERS, T_DATA, T_GOAWAY = 0x0, 0x1, 0x2
F_END_STREAM = 0x01

M_GET, M_HEAD = 0x01, 0x02
METHODS = {M_GET: "GET", M_HEAD: "HEAD"}

STATIC = ["host", "user-agent", "accept", "content-type", "content-length",
          "server", "date", "connection", "last-modified", "cache-control"]
IDX = {n: i + 1 for i, n in enumerate(STATIC)}


class Malformed(Exception):
    pass


def pack_frame(ftype, flags, stream, payload=b""):
    n = len(payload)
    return struct.pack(">II", (n << 8) | ftype, (flags << 24) | (stream & 0xFFFFFF)) + payload


def unpack_header(h):
    a, b = struct.unpack(">II", h)
    return a >> 8, a & 0xFF, b >> 24, b & 0xFFFFFF


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_frame(sock):
    """Return (type, flags, stream, payload), or None on clean EOF."""
    h = read_exact(sock, HDR_LEN)
    if h is None:
        return None
    length, ftype, flags, stream = unpack_header(h)
    if length > MAX_PAYLOAD:
        raise Malformed("frame too large")
    payload = read_exact(sock, length) if length else b""
    if payload is None:
        raise Malformed("truncated frame")
    return ftype, flags, stream, payload, h


def enc_headers(pairs):
    out = bytearray([len(pairs)])
    for name, value in pairs:
        v = value.encode()
        name = name.lower()
        if name in IDX:
            out.append(IDX[name])
        else:
            nb = name.encode()
            out += bytes([0, len(nb)]) + nb
        out += struct.pack(">H", len(v)) + v
    return bytes(out)


def dec_headers(buf, pos):
    if pos >= len(buf):
        raise Malformed("missing header count")
    count = buf[pos]; pos += 1
    pairs = []
    for _ in range(count):
        if pos >= len(buf):
            raise Malformed("short header block")
        idx = buf[pos]; pos += 1
        if idx == 0:
            if pos >= len(buf):
                raise Malformed("short literal name")
            nl = buf[pos]; pos += 1
            name = buf[pos:pos + nl]
            if len(name) != nl:
                raise Malformed("short literal name")
            name = name.decode(); pos += nl
        elif idx <= len(STATIC):
            name = STATIC[idx - 1]
        else:
            raise Malformed("reserved header index %d" % idx)
        if pos + 2 > len(buf):
            raise Malformed("short value length")
        (vl,) = struct.unpack(">H", buf[pos:pos + 2]); pos += 2
        val = buf[pos:pos + vl]
        if len(val) != vl:
            raise Malformed("short value")
        pairs.append((name, val.decode())); pos += vl
    return pairs, pos


def enc_request(method, path, pairs):
    p = path.encode()
    return bytes([method]) + struct.pack(">H", len(p)) + p + enc_headers(pairs)


def dec_request(buf):
    if len(buf) < 3:
        raise Malformed("short request")
    method = buf[0]
    if method not in METHODS:
        raise Malformed("unknown method")
    (pl,) = struct.unpack(">H", buf[1:3])
    path = buf[3:3 + pl]
    if len(path) != pl:
        raise Malformed("short path")
    pairs, pos = dec_headers(buf, 3 + pl)
    if pos != len(buf):
        raise Malformed("trailing bytes")
    return method, path.decode(), pairs


def enc_response(status, pairs):
    return struct.pack(">H", status) + enc_headers(pairs)


def dec_response(buf):
    if len(buf) < 3:
        raise Malformed("short response")
    (status,) = struct.unpack(">H", buf[:2])
    pairs, pos = dec_headers(buf, 2)
    if pos != len(buf):
        raise Malformed("trailing bytes")
    return status, pairs


def hexdump(data, prefix=""):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("%s%04x  %-47s  %s" % (prefix, i, hx, asc))
    return "\n".join(lines)
