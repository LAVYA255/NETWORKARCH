"""Frame codec for BHTTP/1. bserve and bcurl both import this.

Quick recap of the wire format (SPEC.md has the full rules): one TCP
connection, the 4-byte preface "BHT1", then frames. Each frame is an 8-byte
header, Length (24 bits) | Type (8) | Flags (8) | Stream ID (24), followed by
Length bytes of payload. Integers are big-endian, strings are UTF-8 and always
length-prefixed.
"""
import math, os, socket, struct, time

PREFACE = b"BHT1"
MAX_PAYLOAD = 16384
MAX_STREAM_ID = 0xFFFFFF

HEADERS, DATA, GOAWAY = 0x0, 0x1, 0x2
FLAG_END_STREAM = 0x01

METHOD_GET, METHOD_HEAD = 0x01, 0x02
METHOD_NAMES = {METHOD_GET: "GET", METHOD_HEAD: "HEAD"}

ERR_NONE, ERR_PROTOCOL, ERR_FRAME_SIZE, ERR_PREFACE, ERR_INTERNAL = 0, 1, 2, 3, 4
GOAWAY_REASONS = {ERR_NONE: "normal", ERR_PROTOCOL: "protocol error",
                  ERR_FRAME_SIZE: "frame too large", ERR_PREFACE: "bad preface",
                  ERR_INTERNAL: "internal error"}

# How long (and how many bytes) we keep reading and throwing away input after
# a GOAWAY. If we close with unread input the kernel sends RST, not FIN.
DRAIN_TIME = 1.0
DRAIN_MAX_BYTES = 65536

STATIC_TABLE = ["host", "user-agent", "accept", "content-type", "content-length",
                "server", "date", "etag", "last-modified", "cache-control"]
STATIC_INDEX = {name: i + 1 for i, name in enumerate(STATIC_TABLE)}
NAME_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class Malformed(Exception):
    """The payload is wrong but the framing is fine. A server answers 400 on
    that stream and keeps going; a client treats it as a protocol error."""


class ProtocolError(Exception):
    """Framing is broken (or the peer broke a rule we can't recover from).
    Send GOAWAY(.code) and close."""
    def __init__(self, msg, code=ERR_PROTOCOL):
        super().__init__(msg)
        self.code = code


class GoawayReceived(ProtocolError):
    """The peer sent a valid GOAWAY. .code is their code, with unknown codes
    mapped to 1. Don't reply to it, just close."""
    def __init__(self, code):
        if code not in GOAWAY_REASONS:
            code = ERR_PROTOCOL
        super().__init__("peer sent GOAWAY(%d): %s" % (code, GOAWAY_REASONS[code]), code)


def seconds_from_env(name, default):
    """Read a timeout in seconds from the environment. Must be > 0 and finite."""
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        secs = float(raw)
    except ValueError:
        raise ValueError("%s must be a number of seconds, got %r" % (name, raw))
    if not math.isfinite(secs) or secs <= 0:
        raise ValueError("%s must be positive and finite, got %r" % (name, raw))
    return secs


def int_from_env(name, default, lo, hi):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit()) or not lo <= int(raw) <= hi:
        raise ValueError("%s must be an integer in %d..%d, got %r" % (name, lo, hi, raw))
    return int(raw)


# Once the first byte of a frame shows up, the rest has this long to arrive.
FRAME_TIMEOUT = seconds_from_env("BHTTP_FRAME_TIMEOUT", 10)


def read_exactly(sock, n, deadline=None):
    """Read exactly n bytes. Raises EOFError if the peer closed, or
    ProtocolError if the deadline passes first."""
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("frame not completed in time")
            sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise ProtocolError("frame not completed in time")
        if not chunk:
            raise EOFError("closed")
        buf += chunk
    return bytes(buf)


def recv_frame(sock, trace=None, idle=None):
    """Read one frame and return (type, flags, stream_id, payload).

    `idle` is how long to wait for the first byte (None = forever). We set it
    on every call so an old per-frame deadline can't carry over.

    Raises EOFError on a clean close between frames, socket.timeout if
    nothing arrives within `idle`, and ProtocolError if the frame is cut off,
    too slow, or longer than 16384. Length is checked before we even look at
    Type, so an oversized frame is an error even when the type is unknown.
    That way we never hold more than 8 + 16384 bytes for a single frame.
    """
    sock.settimeout(idle)
    first = sock.recv(1)
    if not first:
        raise EOFError("closed")
    deadline = time.monotonic() + FRAME_TIMEOUT
    try:
        header = first + read_exactly(sock, 7, deadline)
    except EOFError:
        raise ProtocolError("cut mid-header")
    length = int.from_bytes(header[0:3], "big")
    if length > MAX_PAYLOAD:
        if trace:
            trace("<-", header)
        raise ProtocolError("length %d over limit" % length, ERR_FRAME_SIZE)
    try:
        payload = read_exactly(sock, length, deadline) if length else b""
    except EOFError:
        raise ProtocolError("cut mid-frame")
    if trace:
        trace("<-", header + payload)
    return header[3], header[4], int.from_bytes(header[5:8], "big"), payload


def build_frame(ftype, flags, stream_id, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload over the 16384 cap")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError("stream ID out of range")
    return len(payload).to_bytes(3, "big") + bytes((ftype, flags)) + \
        stream_id.to_bytes(3, "big") + payload


def build_goaway(code):
    return build_frame(GOAWAY, 0, 0, struct.pack(">H", code))


def parse_goaway(stream_id, payload):
    """Check a GOAWAY we received and return its code (unknown codes -> 1)."""
    if stream_id != 0 or len(payload) != 2:
        raise ProtocolError("malformed GOAWAY")
    (code,) = struct.unpack(">H", payload)
    return code if code in GOAWAY_REASONS else ERR_PROTOCOL


def send_goaway_and_close(sock, code, trace=None, linger=None):
    """Send GOAWAY(code), then shut down cleanly.

    The problem this solves: if you close() a socket that still has unread
    data in its receive buffer, the kernel sends RST instead of FIN, and the
    RST can wipe out our GOAWAY before the other side reads it. So we send
    FIN first and read until the peer closes or the drain limits run out.
    """
    frame = build_goaway(code)
    try:
        sock.sendall(frame)
        if trace:
            trace("->", frame)
    except OSError:
        sock.close()                    # peer is already gone
        return
    drain_and_close(sock, linger)


def drain_and_close(sock, linger=None):
    """Half-close, then discard whatever the peer still sends until it closes
    (up to `linger` seconds or DRAIN_MAX_BYTES), then close. Ends in FIN."""
    linger = DRAIN_TIME if linger is None else linger
    try:
        sock.shutdown(socket.SHUT_WR)
        stop_at = time.monotonic() + linger
        drained = 0
        while drained < DRAIN_MAX_BYTES:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass                            # peer is already gone
    finally:
        sock.close()


def _decode_utf8(raw, what):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("%s is not valid UTF-8" % what)


def _field(buf, pos, n, what):
    """buf[pos:pos+n], or Malformed if that runs off the end of the payload."""
    if pos + n > len(buf):
        raise Malformed("%s runs past the end of the frame" % what)
    return buf[pos:pos + n]


def validate_name(name):
    if not name or not set(name) <= NAME_ALPHABET:
        raise Malformed("header name %r is not lowercase ASCII [a-z0-9-]" % name)


def encode_headers(headers):
    """Header block: a count byte, then for each header either its static
    index or 0x00 + a literal name, then Value length (2) + Value."""
    if len(headers) > 255:
        raise ValueError("too many headers")
    out = bytearray([len(headers)])
    for name, value in headers:
        validate_name(name)
        value_bytes = value.encode("utf-8")
        if len(value_bytes) > 0xFFFF:
            raise ValueError("header value too long")
        if name in STATIC_INDEX:
            out.append(STATIC_INDEX[name])
        else:
            name_bytes = name.encode("ascii")
            if len(name_bytes) > 255:
                raise ValueError("header name too long")
            out += bytes([0, len(name_bytes)]) + name_bytes
        out += struct.pack(">H", len(value_bytes)) + value_bytes
    return bytes(out)


def decode_headers(buf, pos):
    """Parse a header block starting at buf[pos]. Returns (headers, end_pos).
    Every length is checked against what's left in the payload. A literal
    name that matches a static name is treated as that name (for dup checks)."""
    count = _field(buf, pos, 1, "header count")[0]; pos += 1
    headers = []
    seen = set()
    for _ in range(count):
        index = _field(buf, pos, 1, "header index")[0]; pos += 1
        if index == 0:
            name_len = _field(buf, pos, 1, "literal name length")[0]; pos += 1
            if name_len < 1:
                raise Malformed("empty literal name")
            name = _decode_utf8(_field(buf, pos, name_len, "literal name"), "header name")
            pos += name_len
        elif index <= len(STATIC_TABLE):
            name = STATIC_TABLE[index - 1]
        else:
            raise Malformed("reserved header index 0x%02x" % index)
        validate_name(name)
        if name in seen:
            raise Malformed("duplicate header %r" % name)
        seen.add(name)
        (value_len,) = struct.unpack(">H", _field(buf, pos, 2, "value length")); pos += 2
        value = _decode_utf8(_field(buf, pos, value_len, "header value"), "header value")
        pos += value_len
        headers.append((name, value))
    return headers, pos


def validate_path(path):
    """Path rules from SPEC section 5. No percent-decoding, ever."""
    if not path.startswith("/"):
        raise Malformed("path does not start with /")
    if "\x00" in path:
        raise Malformed("NUL in path")
    if any(seg == ".." for seg in path.split("/")):
        raise Malformed('".." segment in path')


def encode_request(method, path, headers):
    raw_path = path.encode("utf-8")
    return bytes([method]) + struct.pack(">H", len(raw_path)) + raw_path + encode_headers(headers)


def decode_request(buf):
    method = _field(buf, 0, 1, "method")[0]
    if method not in METHOD_NAMES:
        raise Malformed("unknown method 0x%02x" % method)
    (path_len,) = struct.unpack(">H", _field(buf, 1, 2, "path length"))
    path = _decode_utf8(_field(buf, 3, path_len, "path"), "path")
    validate_path(path)
    headers, pos = decode_headers(buf, 3 + path_len)
    if pos != len(buf):
        raise Malformed("trailing bytes")      # leftover bytes after the last header
    return method, path, headers


def encode_response(status, headers):
    if not 100 <= status <= 599:
        raise ValueError("status %d outside 100-599" % status)
    return struct.pack(">H", status) + encode_headers(headers)


def decode_response(buf):
    (status,) = struct.unpack(">H", _field(buf, 0, 2, "status"))
    headers, pos = decode_headers(buf, 2)
    if pos != len(buf):
        raise Malformed("trailing bytes")
    return status, headers


def parse_content_length(value):
    """1 to 18 ASCII digits. Anything else (signs, spaces, Unicode digits) is rejected."""
    if not value or len(value) > 18 or not (value.isascii() and value.isdigit()):
        raise Malformed("bad content-length %r" % value)
    return int(value)


def hexdump(data, prefix=""):
    lines = []
    for offset in range(0, len(data), 16):
        row = data[offset:offset + 16]
        hex_part = " ".join("%02x" % b for b in row)
        text_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append("%s%04x  %-47s  %s" % (prefix, offset, hex_part, text_part))
    return "\n".join(lines)
