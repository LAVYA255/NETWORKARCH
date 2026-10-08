#!/usr/bin/env python3
"""Minimal BHTTP/1 ("BHT1") file server, written from SPEC.txt alone.

Usage: python3 server.py [port]     serves ./www on 127.0.0.1 (default port 9000)
One connection at a time; requests on it run in lockstep, as the spec requires.
"""
import os, socket, struct, sys, time

PREFACE = b"BHT1"
MAX_LEN = 16384                      # max frame payload (spec section 3)
HEADERS, DATA, GOAWAY = 0x0, 0x1, 0x2
END_STREAM = 0x01
TYPE_NAMES = {HEADERS: "HEADERS", DATA: "DATA", GOAWAY: "GOAWAY"}
STATIC = [None, "host", "user-agent", "accept", "content-type", "content-length",
          "server", "date", "etag", "last-modified", "cache-control"]
NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyz0123456789-")
CTYPES = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
          ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
          ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}
SERVER = "bht1-mini/0.1"
ROOT = os.path.realpath("www")
IDLE_TIMEOUT, FRAME_TIMEOUT, DRAIN_TIME, DRAIN_BYTES = 30, 10, 1, 64 * 1024


class Malformed(Exception):          # bad request payload -> 400, connection stays open
    pass

class GoAway(Exception):             # connection-level error -> GOAWAY(code), then close
    def __init__(self, code):
        self.code = code

class Closed(Exception):             # peer closed cleanly at a frame boundary
    pass


def http_date(t):                    # IMF-fixdate, locale-independent
    g = time.gmtime(t)
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[g.tm_wday]
    mon = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[g.tm_mon - 1]
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        day, g.tm_mday, mon, g.tm_year, g.tm_hour, g.tm_min, g.tm_sec)


def log(arrow, ftype, flags, sid, length, note=""):
    name = TYPE_NAMES.get(ftype, "0x%02x" % ftype)
    print("%s %-7s len=%-5d flags=0x%02x stream=%d %s" % (arrow, name, length, flags, sid, note))


def recv_exact(sock, n, deadline):
    buf = bytearray()
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        sock.settimeout(left)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    sock.settimeout(IDLE_TIMEOUT)
    try:
        first = sock.recv(1)
    except TimeoutError:
        print("   idle for %ds" % IDLE_TIMEOUT)
        raise GoAway(0)
    if not first:
        raise Closed
    deadline = time.monotonic() + FRAME_TIMEOUT     # whole frame must arrive in time
    try:
        a, b = struct.unpack(">II", first + recv_exact(sock, 7, deadline))
        length, ftype, flags, sid = a >> 8, a & 0xFF, b >> 24, b & 0xFFFFFF
        if length > MAX_LEN:                        # checked before Type; payload left unread
            log("<-", ftype, flags, sid, length, "(too large)")
            raise GoAway(2)
        payload = recv_exact(sock, length, deadline)
    except (TimeoutError, EOFError):
        print("   frame cut off or not completed in time")
        raise GoAway(1)
    log("<-", ftype, flags, sid, length)
    return ftype, flags, sid, payload


def send_frame(sock, ftype, flags, sid, payload, note=""):
    log("->", ftype, flags, sid, len(payload), note)
    sock.sendall(struct.pack(">II", len(payload) << 8 | ftype, flags << 24 | sid) + payload)


def goaway(sock, code):
    try:
        send_frame(sock, GOAWAY, 0, 0, struct.pack(">H", code), "code=%d" % code)
        sock.shutdown(socket.SHUT_WR)               # FIN, then drain so close doesn't RST
        deadline, drained = time.monotonic() + DRAIN_TIME, 0
        while drained < DRAIN_BYTES and time.monotonic() < deadline:
            sock.settimeout(deadline - time.monotonic())
            chunk = sock.recv(4096)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass


class Reader:                        # bounds-checked cursor over a payload
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise Malformed("underrun")
        self.pos += n
        return self.data[self.pos - n:self.pos]

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack(">H", self.take(2))[0]


def utf8(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("invalid UTF-8")


def parse_request(payload):
    r = Reader(payload)
    method = r.u8()
    if method not in (1, 2):
        raise Malformed("bad method 0x%02x" % method)
    raw = r.take(r.u16())
    path = utf8(raw)
    if not path.startswith("/") or "\0" in path or len(raw) > 16380:
        raise Malformed("bad path")
    headers = {}
    for _ in range(r.u8()):
        idx = r.u8()
        if idx == 0:                                # literal name
            raw_name = r.take(r.u8())
            if not raw_name or not set(raw_name) <= NAME_CHARS:
                raise Malformed("bad header name")
            name = raw_name.decode("ascii")
        elif idx <= 10:
            name = STATIC[idx]
        else:
            raise Malformed("reserved header index %d" % idx)
        if name in headers:
            raise Malformed("duplicate header " + name)
        headers[name] = utf8(r.take(r.u16()))
    if r.pos != len(payload):
        raise Malformed("overrun")
    return method, path, headers


def resolve(path):                   # -> real file path under ROOT, or None for 404
    segs = [s for s in path.split("/") if s not in ("", ".")]
    if ".." in segs:
        raise Malformed("'..' segment")
    target = os.path.realpath(os.path.join(ROOT, *segs))
    if os.path.isdir(target):
        target = os.path.realpath(os.path.join(target, "index.html"))
    elif path.endswith("/"):                        # trailing slash on a file
        return None
    inside = os.path.commonpath([ROOT, target]) == ROOT
    return target if inside and os.path.isfile(target) else None


def send_response(sock, sid, status, extra=(), length=0, body_follows=False):
    entries = [(5, str(length)), (6, SERVER), (7, http_date(time.time()))] + list(extra)
    out = struct.pack(">HB", status, len(entries))
    for idx, value in entries:
        v = value.encode()
        out += struct.pack(">BH", idx, len(v)) + v
    send_frame(sock, HEADERS, 0 if body_follows else END_STREAM, sid, out, "status=%d" % status)


def handle_request(sock, sid, payload):
    try:
        method, path, headers = parse_request(payload)
        target = resolve(path)
    except Malformed as e:
        print("   malformed request:", e)
        return send_response(sock, sid, 400)
    print("   %s %s" % ("GET" if method == 1 else "HEAD", path))
    if target is None:
        return send_response(sock, sid, 404)
    try:
        f = open(target, "rb")
        st = os.fstat(f.fileno())
    except OSError:
        return send_response(sock, sid, 500)        # failed before the response started
    with f:
        size, etag = st.st_size, '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        ctype = CTYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        meta = [(4, ctype), (8, etag), (9, http_date(st.st_mtime)), (10, "no-cache")]
        if headers.get("if-none-match") in ("*", etag):
            return send_response(sock, sid, 304, meta, size)
        if method == 2 or size == 0:                # HEAD or empty file: no DATA
            return send_response(sock, sid, 200, meta, size)
        send_response(sock, sid, 200, meta, size, body_follows=True)
        sent = 0
        while sent < size:
            try:
                chunk = f.read(min(MAX_LEN, size - sent))
            except OSError:
                chunk = b""
            if not chunk:                           # failed after the response started
                raise GoAway(4)
            sent += len(chunk)
            send_frame(sock, DATA, END_STREAM if sent == size else 0, sid, chunk)


def serve_connection(sock, addr):
    print("== connection from %s:%d" % addr[:2])
    try:
        try:
            preface = recv_exact(sock, 4, time.monotonic() + FRAME_TIMEOUT)
        except (TimeoutError, EOFError):
            print("   no preface; closing without GOAWAY")
            return
        print("<- preface %r" % preface)
        if preface != PREFACE:
            raise GoAway(3)
        last_sid = 0
        while True:
            ftype, flags, sid, payload = read_frame(sock)
            if ftype == GOAWAY:
                if len(payload) != 2 or sid != 0:
                    raise GoAway(1)
                print("   peer sent GOAWAY code=%d; closing" % struct.unpack(">H", payload))
                return
            if ftype != HEADERS:                    # client DATA and unknown types: skip
                print("   skipped")
                continue
            if sid == 0:
                raise GoAway(1)
            if sid <= last_sid:
                print("   malformed request: stream %d not above %d" % (sid, last_sid))
                send_response(sock, sid, 400)
                continue
            last_sid = sid
            handle_request(sock, sid, payload)
    except GoAway as g:
        goaway(sock, g.code)
    except Closed:
        print("   peer closed the connection")
    except OSError as e:
        print("   connection error:", e)
    finally:
        sock.close()
        print("== closed")


def main():
    sys.stdout.reconfigure(line_buffering=True)     # frames show up live even when piped
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    if not os.path.isdir(ROOT):
        sys.exit("no www directory at " + ROOT)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()
    print("BHT1 server on 127.0.0.1:%d serving %s" % (port, ROOT))
    try:
        while True:
            conn, addr = srv.accept()
            serve_connection(conn, addr)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
