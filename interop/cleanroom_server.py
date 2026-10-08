#!/usr/bin/env python3
"""cleanroom_server.py ROOT PORT: a minimal BHTTP/1 server written from SPEC.md.

The server half of the spec-only evidence: it gives bcurl a peer that is not
bserve. No shared code: it imports only os, socket, struct, sys and time
(time only for the date header), never bhttp.py or bserve, and run.sh and the
tests start it with `python3 -I`, so it cannot import project code by
accident. Same author as bserve and bcurl (see README): spec-only code, not
a second person. One connection at a time, on 127.0.0.1. Prints
"cleanroom-server: listening on port N" (PORT 0 picks one).
"""
import os, socket, struct, sys, time

TABLE = [None, "host", "user-agent", "accept", "content-type", "content-length",
         "server", "date", "etag", "last-modified", "cache-control"]   # SPEC section 7
INDEX = {name: i for i, name in enumerate(TABLE) if name}
CAP = 16384                                                           # SPEC section 3
NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TYPES = {".html": "text/html", ".htm": "text/html", ".txt": "text/plain",
         ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
         ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif"}


class Bad(Exception):
    """Bad payload, framing intact: 400 on that stream (SPEC section 9)."""


class Fatal(Exception):
    """Send GOAWAY(code) and close."""
    def __init__(self, code):
        super().__init__("GOAWAY(%d)" % code)
        self.code = code


class Closed(Exception):
    """The client closed cleanly on a frame boundary."""


def frame(ftype, flags, sid, payload=b""):                   # 24/8/8/24
    return struct.pack(">I", len(payload))[1:] + bytes([ftype, flags]) + \
        struct.pack(">I", sid)[1:] + payload


def imf_date(t):                                             # RFC 9110 IMF-fixdate
    g = time.gmtime(t)
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        DAYS[g.tm_wday], g.tm_mday, MONTHS[g.tm_mon - 1], g.tm_year,
        g.tm_hour, g.tm_min, g.tm_sec)


def recv_n(sock, n):
    out = b""
    while len(out) < n:
        piece = sock.recv(n - len(out))
        if not piece:
            raise EOFError()
        out += piece
    return out


def next_frame(sock):
    try:
        first = sock.recv(1)
    except socket.timeout:
        raise Fatal(0)                      # idle connection: GOAWAY(0)
    if not first:
        raise Closed()
    try:
        head = first + recv_n(sock, 7)
        length = int.from_bytes(head[:3], "big")
        if length > CAP:
            raise Fatal(2)                  # Length before Type, on any type
        return head[3], head[4], int.from_bytes(head[5:], "big"), recv_n(sock, length)
    except (EOFError, socket.timeout):
        raise Fatal(1)                      # cut or stalled inside a frame


def take(buf, at, n):
    if at + n > len(buf):
        raise Bad("field runs past the payload")
    return buf[at:at + n], at + n


def utf8(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Bad("not UTF-8")


def parse_request(p):                       # SPEC sections 5 and 7
    raw, at = take(p, 0, 1)
    method = raw[0]
    if method not in (1, 2):
        raise Bad("method")
    raw, at = take(p, at, 2)
    raw, at = take(p, at, int.from_bytes(raw, "big"))
    path = utf8(raw)
    if not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
        raise Bad("path")
    raw, at = take(p, at, 1)
    names = {}
    for _ in range(raw[0]):
        raw, at = take(p, at, 1)
        if raw[0] == 0:
            nlen, at = take(p, at, 1)
            if nlen[0] == 0:
                raise Bad("empty literal name")
            raw, at = take(p, at, nlen[0])
            name = utf8(raw)
        elif raw[0] <= 10:
            name = TABLE[raw[0]]
        else:
            raise Bad("reserved index")
        if not set(name) <= NAME_OK or name in names:
            raise Bad("bad or duplicate name")
        vlen, at = take(p, at, 2)
        raw, at = take(p, at, int.from_bytes(vlen, "big"))
        names[name] = utf8(raw)
    if at != len(p):
        raise Bad("trailing bytes")
    return method, path, names


def resolve(root, path):
    """SPEC section 5: drop empty and "." segments; directories map to
    index.html; a path sent with a trailing "/" that names a file is 404;
    nothing outside the root, symlinks included. None means 404."""
    parts = [s for s in path.split("/") if s not in ("", ".")]
    real = os.path.realpath(os.path.join(root, *parts))
    if os.path.isdir(real):
        real = os.path.realpath(os.path.join(real, "index.html"))
    elif path.endswith("/"):
        return None
    if os.path.commonpath([root, real]) != root or not os.path.isfile(real):
        return None
    return real


def respond(sock, sid, status, extra=(), body=None, size=0):
    """SPEC section 6. Without a body, END_STREAM goes on HEADERS."""
    pairs = [("server", "cleanroom-server/1"), ("date", imf_date(time.time())),
             ("content-length", str(size))] + list(extra)
    payload = struct.pack(">H", status) + bytes([len(pairs)])
    for name, value in pairs:
        v = value.encode("utf-8")
        payload += bytes([INDEX[name]]) + struct.pack(">H", len(v)) + v
    if body is None or size == 0:
        sock.sendall(frame(0, 1, sid, payload))
        return
    sock.sendall(frame(0, 0, sid, payload))
    left = size
    while left:
        chunk = body.read(min(CAP, left))
        if not chunk:
            raise Fatal(4)                  # the file failed after the response started
        left -= len(chunk)
        sock.sendall(frame(1, 1 if left == 0 else 0, sid, chunk))


def serve(sock, sid, payload, root):
    try:
        method, path, names = parse_request(payload)
    except Bad:
        return respond(sock, sid, 400)
    real = resolve(root, path)
    if real is None:
        return respond(sock, sid, 404)
    try:
        f = open(real, "rb")
        st = os.fstat(f.fileno())
    except OSError:
        return respond(sock, sid, 500)
    with f:
        etag = '"%x-%x"' % (st.st_size, int(st.st_mtime))
        ctype = TYPES.get(os.path.splitext(real)[1].lower(), "application/octet-stream")
        meta = [("content-type", ctype), ("last-modified", imf_date(st.st_mtime)),
                ("etag", etag), ("cache-control", "no-cache")]
        inm = names.get("if-none-match")
        if inm is not None and inm in (etag, "*"):
            return respond(sock, sid, 304, meta, size=st.st_size)
        if method == 2:
            return respond(sock, sid, 200, meta, size=st.st_size)
        respond(sock, sid, 200, meta, f, st.st_size)


def drain_close(sock):
    """SPEC section 10: half-close, discard input until the peer closes
    (at most 1 s or 64 KiB), then close."""
    try:
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + 1.0
        got = 0
        while got < 65536 and time.monotonic() < end:
            sock.settimeout(max(end - time.monotonic(), 0.01))
            piece = sock.recv(4096)
            if not piece:
                break
            got += len(piece)
    except OSError:
        pass
    sock.close()


def goaway(sock, code):
    try:
        sock.sendall(frame(2, 0, 0, struct.pack(">H", code)))
    except OSError:
        return
    drain_close(sock)


def handle(sock, root):
    sock.settimeout(10)
    pre = b""
    try:
        while len(pre) < 4:
            piece = sock.recv(4 - len(pre))
            if not piece:
                return                      # closed before the preface
            pre += piece
    except socket.timeout:
        return                              # slow preface: close, no GOAWAY
    if pre != b"BHT1":
        return goaway(sock, 3)
    sock.settimeout(30)
    last = 0
    while True:
        try:
            ftype, flags, sid, payload = next_frame(sock)
            if ftype == 2:
                if sid != 0 or len(payload) != 2:
                    raise Fatal(1)          # malformed GOAWAY
                return drain_close(sock)    # the client is done: send nothing more
            if ftype != 0:
                continue                    # DATA from a client, unknown types: skip
            if sid == 0:
                raise Fatal(1)              # HEADERS on stream 0
            if sid <= last:
                respond(sock, sid, 400)     # reused or lower ID
                continue
            last = sid
            serve(sock, sid, payload, root)
        except Closed:
            return
        except Fatal as e:
            return goaway(sock, e.code)


def main(argv):
    if len(argv) != 3 or not (argv[2].isascii() and argv[2].isdigit()) or int(argv[2]) > 65535:
        sys.stderr.write("usage: cleanroom_server.py ROOT PORT\n")
        return 2
    root = os.path.realpath(argv[1])
    if not os.path.isdir(root):
        sys.stderr.write("cleanroom-server: %s is not a directory\n" % argv[1])
        return 2
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(argv[2])))
    srv.listen(16)
    sys.stdout.write("cleanroom-server: listening on port %d\n" % srv.getsockname()[1])
    sys.stdout.flush()
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        try:
            handle(conn, root)
        except OSError:
            pass                            # the client reset: next connection
        finally:
            conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
