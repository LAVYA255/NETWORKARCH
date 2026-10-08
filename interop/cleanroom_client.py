#!/usr/bin/env python3
"""cleanroom_client.py [-I] host[:port][/path] [/path ...]: a minimal BHTTP/1 client.

Implemented from SPEC.md alone, no shared code with bhttp.py. It imports
nothing from this project; frame packing, parsing and the static table are
written inline from the spec text. Run it with `python3 -I` to prove it
cannot import bhttp.py. Same author as bserve/bcurl (see README), so this is
spec-only evidence, not an independent implementer.

Exit codes: 0 for 1xx-3xx, 4 for 4xx, 5 for 5xx, 1 for a connection or
protocol error, 2 for bad usage. BCURL_TIMEOUT (seconds) bounds each wait.
"""
import os, socket, struct, sys

# SPEC section 7, static table, indexes 1..10.
TABLE = [None, "host", "user-agent", "accept", "content-type", "content-length",
         "server", "date", "etag", "last-modified", "cache-control"]
NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")
CAP = 16384                                 # SPEC section 3, Length cap


class ProtoError(Exception):
    """A protocol error: answer with GOAWAY(code), or nothing if code is None."""
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def frame(ftype, flags, sid, payload=b""):  # SPEC section 3: 24/8/8/24
    return struct.pack(">I", len(payload))[1:] + bytes([ftype, flags]) + \
        struct.pack(">I", sid)[1:] + payload


def recv_n(sock, n):
    out = b""
    while len(out) < n:
        piece = sock.recv(n - len(out))
        if not piece:
            raise ProtoError("connection closed inside a frame")
        out += piece
    return out


def next_frame(sock):
    head = recv_n(sock, 8)
    length = int.from_bytes(head[:3], "big")
    if length > CAP:                        # checked before Type (section 3)
        raise ProtoError("Length %d over the cap" % length, 2)
    return head[3], head[4], int.from_bytes(head[5:], "big"), recv_n(sock, length)


def take(buf, at, n):
    if at + n > len(buf):
        raise ProtoError("field runs past the payload")
    return buf[at:at + n], at + n


def response_headers(p):                    # SPEC section 6 and 7
    raw, at = take(p, 0, 2)
    status = int.from_bytes(raw, "big")
    raw, at = take(p, at, 1)
    names = {}
    for _ in range(raw[0]):
        raw, at = take(p, at, 1)
        if raw[0] == 0:
            nlen, at = take(p, at, 1)
            if nlen[0] == 0:
                raise ProtoError("empty literal name")
            nm, at = take(p, at, nlen[0])
            nm = nm.decode("ascii", "replace")
            if not set(nm) <= NAME_OK:
                raise ProtoError("header name outside a-z 0-9 -")
        elif raw[0] <= 10:
            nm = TABLE[raw[0]]
        else:
            raise ProtoError("reserved index %d" % raw[0])
        if nm in names:
            raise ProtoError("duplicate header " + nm)
        vlen, at = take(p, at, 2)
        val, at = take(p, at, int.from_bytes(vlen, "big"))
        try:
            names[nm] = val.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtoError("header value is not UTF-8")
    if at != len(p):
        raise ProtoError("trailing bytes in HEADERS")
    return status, names


def fetch(sock, sid, path, host, head, out):
    path_b = path.encode("utf-8")
    req = bytes([2 if head else 1]) + struct.pack(">H", len(path_b)) + path_b
    hdrs = [(1, host.encode()), (2, b"cleanroom/1"), (3, b"*/*")]
    req += bytes([len(hdrs)]) + b"".join(
        bytes([i]) + struct.pack(">H", len(v)) + v for i, v in hdrs)
    sock.sendall(frame(0, 1, sid, req))     # HEADERS + END_STREAM
    status, clen, got = None, 0, 0
    bodiless = False
    while True:
        ftype, flags, fsid, payload = next_frame(sock)
        if ftype == 2:                      # GOAWAY: validate, then stop without answering
            if fsid != 0 or len(payload) != 2:
                raise ProtoError("malformed GOAWAY")
            raise ProtoError("server sent GOAWAY(%d)" % struct.unpack(">H", payload)[0], None)
        if ftype not in (0, 1):
            continue                        # unknown type: MUST skip
        if fsid != sid:
            raise ProtoError("frame on stream %d, expected %d" % (fsid, sid))
        if ftype == 0:
            if status is not None:
                raise ProtoError("second HEADERS")
            status, names = response_headers(payload)
            if not 100 <= status <= 599:
                raise ProtoError("status out of range")
            cl = names.get("content-length", "")
            if not 1 <= len(cl) <= 18 or not all("0" <= ch <= "9" for ch in cl):
                raise ProtoError("bad content-length")
            clen = int(cl)
            bodiless = head or status == 304
            if bodiless and not flags & 1:
                raise ProtoError("HEADERS without END_STREAM on a bodiless response")
        else:
            if status is None:
                raise ProtoError("DATA before HEADERS")
            got += len(payload)
            if got > clen:
                raise ProtoError("more DATA than content-length")
            out.write(payload)
        if flags & 1:
            break
    if not bodiless and got != clen:
        raise ProtoError("DATA short of content-length")
    return status


def goaway_and_close(sock, code):
    """SPEC section 10: GOAWAY, half-close, discard input until the peer
    closes (at most 64 KiB, 1 s per read), so the close is a FIN, not an RST."""
    try:
        sock.sendall(frame(2, 0, 0, struct.pack(">H", code)))
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(1)
        got = 0
        while got < 65536:
            piece = sock.recv(4096)
            if not piece:
                break
            got += len(piece)
    except OSError:
        pass


def main(argv):
    args = argv[1:]
    head = "-I" in args
    args = [a for a in args if a != "-I"]
    if not args:
        sys.stderr.write(__doc__.splitlines()[0] + "\n")
        return 2
    hostport, _, rest = args[0].partition("/")
    host, _, port = hostport.partition(":")
    paths = ["/" + rest] + args[1:]
    port_ok = port == "" or (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535)
    if not host or not port_ok or any(p[:1] != "/" for p in paths):
        sys.stderr.write("cleanroom: bad target\n")
        return 2
    try:
        wait = float(os.environ.get("BCURL_TIMEOUT", "30"))
    except ValueError:
        wait = -1.0
    if not 0 < wait < float("inf"):         # rejects nan, inf, zero, negatives
        sys.stderr.write("cleanroom: BCURL_TIMEOUT must be a positive number of seconds\n")
        return 2
    worst, sock = 0, None
    try:
        sock = socket.create_connection((host, int(port or 9000)), timeout=wait)
        sock.sendall(b"BHT1")
        for n, path in enumerate(paths, 1):
            worst = max(worst, fetch(sock, n, path, hostport, head, sys.stdout.buffer))
        goaway_and_close(sock, 0)
    except ProtoError as e:
        sys.stderr.write("cleanroom: %s\n" % e)
        if e.code is not None:
            goaway_and_close(sock, e.code)
        return 1
    except socket.timeout:
        sys.stderr.write("cleanroom: timed out\n")
        if sock is not None:
            goaway_and_close(sock, 0)       # section 10: give up with GOAWAY(0)
        return 1
    except OSError as e:
        sys.stderr.write("cleanroom: %s\n" % e)
        return 1
    finally:
        if sock:
            sock.close()
    return 4 if 400 <= worst < 500 else 5 if worst >= 500 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
