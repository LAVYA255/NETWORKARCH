#!/usr/bin/env python3
"""Minimal BHTTP/1 (BHT1) client, written from SPEC.md.

usage: python3 client.py [PATH ...]      (default: /)

One TCP connection to localhost:9000, lockstep GET requests on streams
1, 2, 3..., then GOAWAY(0). Text bodies are printed; every body is also
saved under ./received/ so it can be checksummed against the original.
Exit 0 on success, 1 on 404 / protocol error / connection failure.
"""
import os, socket, struct, sys, time

HOST, PORT = "localhost", 9000
MAX_PAYLOAD = 16384
HEADERS, DATA, GOAWAY = 0x0, 0x1, 0x2
END_STREAM = 0x01
GET = 0x01
STATIC = ["host", "user-agent", "accept", "content-type", "content-length",
          "server", "date", "etag", "last-modified", "cache-control"]
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")
GOAWAY_TEXT = {0: "normal", 1: "protocol error", 2: "frame too large",
               3: "bad preface", 4: "internal error"}
TIMEOUT = 30


class ProtocolError(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


class PeerGoaway(Exception):
    pass


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed the connection mid-frame")
        buf += chunk
    return buf


def frame(ftype, flags, stream, payload=b""):
    return len(payload).to_bytes(3, "big") + bytes([ftype, flags]) + \
        stream.to_bytes(3, "big") + payload


def read_frame(sock):
    head = recv_exact(sock, 8)
    length = int.from_bytes(head[0:3], "big")
    ftype, flags, stream = head[3], head[4], int.from_bytes(head[5:8], "big")
    if length > MAX_PAYLOAD:              # checked before Type, payload unread
        raise ProtocolError("frame Length %d > 16384" % length, code=2)
    payload = recv_exact(sock, length)
    print("  <- frame type=0x%x flags=0x%02x stream=%d len=%d"
          % (ftype, flags, stream, length))
    return ftype, flags & END_STREAM, stream, payload


def take(buf, pos, n):
    if pos + n > len(buf):
        raise ProtocolError("malformed response: field runs past end of payload")
    return buf[pos:pos + n], pos + n


def parse_response(buf):
    """Status (2) | count (1) | entries. Returns (status, {name: value})."""
    raw, pos = take(buf, 0, 2)
    status = struct.unpack(">H", raw)[0]
    (count,), pos = take(buf, pos, 1)
    hdrs = {}
    for _ in range(count):
        (idx,), pos = take(buf, pos, 1)
        if idx == 0:
            (nl,), pos = take(buf, pos, 1)
            if nl < 1:
                raise ProtocolError("malformed response: empty literal name")
            name, pos = take(buf, pos, nl)
            name = name.decode("ascii", "replace")
        elif idx <= len(STATIC):
            name = STATIC[idx - 1]
        else:
            raise ProtocolError("malformed response: reserved index 0x%02x" % idx)
        if not set(name) <= NAME_CHARS or name in hdrs:
            raise ProtocolError("malformed response: bad or duplicate name %r" % name)
        raw, pos = take(buf, pos, 2)
        value, pos = take(buf, pos, struct.unpack(">H", raw)[0])
        try:
            hdrs[name] = value.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("malformed response: header value not UTF-8")
    if pos != len(buf):
        raise ProtocolError("malformed response: trailing bytes after headers")
    if not 100 <= status <= 599:
        raise ProtocolError("status %d outside 100-599" % status)
    cl = hdrs.get("content-length", "")
    if not (cl.isascii() and cl.isdigit()):
        raise ProtocolError("missing or bad content-length %r" % cl)
    return status, hdrs, int(cl)


def request(sock, stream, path):
    """Send one GET and read its response through END_STREAM."""
    p = path.encode("utf-8")
    payload = bytes([GET]) + struct.pack(">H", len(p)) + p + bytes([0])
    print("-> HEADERS stream=%d GET %s (END_STREAM)" % (stream, path))
    sock.sendall(frame(HEADERS, END_STREAM, stream, payload))
    status = None
    body = b""
    while True:
        ftype, end, sid, payload = read_frame(sock)
        if ftype == GOAWAY:
            if sid != 0 or len(payload) != 2:
                raise ProtocolError("malformed GOAWAY")
            code = struct.unpack(">H", payload)[0]
            raise PeerGoaway("server sent GOAWAY(%d): %s"
                             % (code, GOAWAY_TEXT.get(code, "protocol error")))
        if ftype not in (HEADERS, DATA):
            print("     skipping unknown frame type 0x%x" % ftype)
            continue
        if sid != stream:
            raise ProtocolError("frame for stream %d, open stream is %d" % (sid, stream))
        if ftype == HEADERS:
            if status is not None:
                raise ProtocolError("second HEADERS on stream %d" % stream)
            status, hdrs, clen = parse_response(payload)
            print("     HEADERS: status %d, %s" % (status, hdrs))
        else:
            if status is None:
                raise ProtocolError("DATA before HEADERS")
            if len(body) + len(payload) > clen:
                raise ProtocolError("DATA exceeds content-length %d" % clen)
            body += payload
            print("     DATA: %d/%d bytes" % (len(body), clen))
        if end:
            if len(body) != clen:
                raise ProtocolError("body %d bytes, content-length %d" % (len(body), clen))
            print("     END_STREAM on stream %d" % stream)
            return status, hdrs, body


def goaway_close(sock, code):
    """Send GOAWAY(code), half-close, drain until the server closes, close."""
    print("-> GOAWAY(%d) stream=0" % code)
    try:
        sock.sendall(frame(GOAWAY, 0, 0, struct.pack(">H", code)))
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            sock.settimeout(max(0.01, end - time.monotonic()))
            if not sock.recv(4096):
                break
    except OSError:
        pass
    sock.close()
    print("   connection closed")


def save(path, body):
    os.makedirs("received", exist_ok=True)
    name = path.strip("/").replace("/", "_") or "index.html"
    out = os.path.join("received", name)
    with open(out, "wb") as f:
        f.write(body)
    return out


def main(paths):
    print("connecting to %s:%d" % (HOST, PORT))
    try:
        sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    except OSError as e:
        print("error: cannot connect: %s" % e)
        return 1
    print("-> preface BHT1")
    sock.sendall(b"BHT1")
    stream, rc = 0, 0
    try:
        for path in paths:
            stream += 1
            status, hdrs, body = request(sock, stream, path)
            if status == 404:
                print("error: 404 Not Found: %s" % path)
                rc = 1
                break
            out = save(path, body)
            print("   %s -> %d, %d bytes saved to %s" % (path, status, len(body), out))
            if hdrs.get("content-type", "").startswith("text/"):
                print(body.decode("utf-8", "replace"))
            if status != 200:
                rc = 1
                break
    except ProtocolError as e:
        print("protocol error: %s" % e)
        goaway_close(sock, e.code)
        return 1
    except PeerGoaway as e:
        print("error: %s" % e)
        sock.close()
        return 1
    except socket.timeout:
        print("error: timed out after %ds waiting for a response" % TIMEOUT)
        goaway_close(sock, 0)
        return 1
    except (ConnectionError, OSError) as e:
        print("error: connection failed: %s" % e)
        sock.close()
        return 1
    goaway_close(sock, 0)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["/"]))
