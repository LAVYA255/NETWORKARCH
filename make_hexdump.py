#!/usr/bin/env python3
"""Regenerate HEXDUMP.txt and HEXDUMP.md from a real bcurl <-> bserve exchange.

1. Starts bserve on port 9000, the brief's port. If 9000 is busy it falls
   back to a free port; the host value then changes, and HEXDUMP.md says so.
2. Serves a one-file root whose index.html has a fixed mtime, so
   last-modified and etag are the same on every run.
3. Runs the real `bcurl -v localhost:PORT/index.html` and rebuilds both byte
   streams from bcurl's own -v hexdump, which prints every frame bcurl sends
   and receives. Every byte in the output crossed the wire, the date included.

Before writing anything it checks: bcurl exited 0 and printed the body; the
bytes bcurl sent equal the codec-built preface + request + GOAWAY(0) for that
host; the response equals the codec-built response once the 29-byte date
value at offset 0x19 is masked (the date is the only field that changes
between runs). Every annotation is derived from the bytes, with assertions:
Length fields equal payload sizes, value lengths equal value sizes, header
indexes map through the SPEC section 7 table, and END_STREAM sits where
SPEC section 6 says.
"""
import email.utils, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bhttp import (PREFACE, HEADERS, DATA, GOAWAY, FLAG_END_STREAM, METHOD_GET, ERR_NONE,
                   STATIC_TABLE, encode_request, encode_response, build_frame, build_goaway)

BSERVE = os.path.join(HERE, "bserve")
BCURL = os.path.join(HERE, "bcurl")
PREFERRED_PORT = 9000
MASK = "Thu, 24 Sep 2026 10:54:34 GMT"  # stands in for the live date in comparisons only
DATE_AT = 0x19                          # response offset of the 29-byte date value
LM_EPOCH = 1790247000                   # index.html mtime: Thu, 24 Sep 2026 10:50:00 GMT
LAST_MODIFIED = email.utils.formatdate(LM_EPOCH, usegmt=True)
BODY = b"<h1>hi</h1>\n"
ETAG = '"%x-%x"' % (len(BODY), LM_EPOCH)

# SPEC section 7, written out again so the annotator does not trust the codec.
TABLE = {1: "host", 2: "user-agent", 3: "accept", 4: "content-type",
         5: "content-length", 6: "server", 7: "date", 8: "etag",
         9: "last-modified", 10: "cache-control"}
TYPES = {HEADERS: "HEADERS", DATA: "DATA", GOAWAY: "GOAWAY"}
METHOD = {1: "GET", 2: "HEAD"}


def golden(host):
    """The exchange as the codec builds it for `host`, date set to MASK."""
    request = PREFACE + build_frame(HEADERS, FLAG_END_STREAM, 1, encode_request(
        METHOD_GET, "/index.html", [("host", host), ("user-agent", "bcurl/1"), ("accept", "*/*")]))
    pairs = [("server", "bserve/1"), ("date", MASK), ("content-length", str(len(BODY))),
             ("content-type", "text/html"), ("last-modified", LAST_MODIFIED),
             ("etag", ETAG), ("cache-control", "no-cache")]
    response = build_frame(HEADERS, 0, 1, encode_response(200, pairs)) + \
        build_frame(DATA, FLAG_END_STREAM, 1, BODY)
    return request, response, build_goaway(ERR_NONE)


def mask_date(response):
    """The response with its live date value replaced by MASK (comparison only)."""
    assert response[DATE_AT - 3:DATE_AT] == b"\x07\x00\x1d", "no 29-byte date entry at 0x16"
    return response[:DATE_AT] + MASK.encode() + response[DATE_AT + 29:]


def start_bserve(www):
    """bserve on 9000 if it binds there, else on a free port. (proc, port)."""
    for port in (str(PREFERRED_PORT), "0"):
        proc = subprocess.Popen([BSERVE, www, port], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        line = proc.stdout.readline().decode()
        if "listening on port" in line:
            return proc, int(line.split()[-1])
        proc.kill()
        proc.wait()
        proc.stdout.close()
    sys.exit("bserve did not start")


def streams_from_v(stderr):
    """Rebuild (sent, received) from bcurl -v output: every '-> NNNN  hex'
    and '<- NNNN  hex' line, in order. The bare '->' / '<-' label lines and
    the '< status' summary line are not hexdump lines and are skipped."""
    out = {"->": bytearray(), "<-": bytearray()}
    for line in stderr.decode("ascii", "replace").splitlines():
        d = line[:2]
        if d in out and line[2:3] == " " and line[7:9] == "  " and \
                all(ch in "0123456789abcdef" for ch in line[3:7]):
            out[d] += bytes.fromhex(line[9:9 + 47])
    return bytes(out["->"]), bytes(out["<-"])


def live_exchange():
    www = tempfile.mkdtemp()
    index = os.path.join(www, "index.html")
    with open(index, "wb") as f:
        f.write(BODY)
    os.utime(index, (LM_EPOCH, LM_EPOCH))
    proc, port = start_bserve(www)
    try:
        r = subprocess.run([BCURL, "-v", "localhost:%d/index.html" % port],
                           capture_output=True, timeout=30)
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
    if r.returncode != 0 or r.stdout != BODY:
        sys.exit("bcurl failed (exit %d):\n%s" % (r.returncode,
                 r.stderr.decode("ascii", "replace")[-600:]))
    sent, received = streams_from_v(r.stderr)
    return sent, received, port


class Annotator(object):
    def __init__(self, data, base=0):
        self.data, self.pos, self.lines, self.base = data, 0, [], base

    def take(self, n, note):
        chunk = self.data[self.pos:self.pos + n]
        assert len(chunk) == n, "ran out of bytes at %d" % self.pos
        for i in range(0, max(n, 1), 16):
            part = chunk[i:i + 16]
            label = note if i == 0 else "  (continued)"
            self.lines.append("%04x  %-47s  # %s" % (self.base + self.pos + i,
                              " ".join("%02x" % b for b in part), label))
        self.pos += n
        return chunk

    def num(self, n, note_fmt):
        raw = self.data[self.pos:self.pos + n]
        val = int.from_bytes(raw, "big")
        self.take(n, note_fmt % {"v": val, "x": raw.hex()})
        return val

    def frame_header(self, want_type, want_flags):
        start = self.pos
        length = self.num(3, "Length = 0x%(x)s = %(v)d payload bytes")
        assert start + 8 + length <= len(self.data), "Length runs past the capture"
        ftype = self.data[self.pos]
        assert ftype == want_type, (ftype, want_type)
        self.take(1, "Type 0x%02x = %s" % (ftype, TYPES[ftype]))
        flags = self.data[self.pos]
        assert flags == want_flags, (flags, want_flags)
        if flags & FLAG_END_STREAM:
            what = "END_STREAM"
        else:
            what = "none (DATA frames follow)" if ftype == HEADERS else "none"
        self.take(1, "Flags 0x%02x = %s" % (flags, what))
        sid = self.num(3, "Stream ID %(v)d")
        return length, sid, self.pos + length

    def entries(self):
        count = self.num(1, "Header count = %(v)d")
        names = []
        for _ in range(count):
            idx = self.data[self.pos]
            assert 1 <= idx <= 10, "only static names in this exchange"
            vlen = int.from_bytes(self.data[self.pos + 1:self.pos + 3], "big")
            self.take(3, "index %d (%s), value length %d" % (idx, TABLE[idx], vlen))
            val = self.data[self.pos:self.pos + vlen].decode("ascii")
            extra = ""
            if TABLE[idx] == "content-length":
                extra = " (ASCII decimal text, not a binary number)"
            elif TABLE[idx] == "date":
                extra = " (live: the server's clock during this run)"
            if TABLE[idx] == "etag":
                self.take(vlen, "%s (the quotes are part of the value)" % val)
            else:
                self.take(vlen, '"%s"%s' % (val, extra))
            names.append((TABLE[idx], val))
        return names


def annotate(request, response, goaway):
    assert [TABLE[i + 1] for i in range(10)] == STATIC_TABLE, "SPEC table and codec disagree"
    out = ["CLIENT -> SERVER (offsets count from the first byte the client sends)"]
    a = Annotator(request)
    a.take(4, 'preface "BHT1" (SPEC section 2)')
    length, sid, end = a.frame_header(HEADERS, FLAG_END_STREAM)
    assert sid == 1
    m = a.data[a.pos]
    a.take(1, "Method 0x%02x = %s" % (m, METHOD[m]))
    plen = a.num(2, "Path length = %(v)d")
    a.take(plen, 'Path "%s"' % a.data[a.pos:a.pos + plen].decode())
    a.entries()
    assert a.pos == end == len(request), "request payload does not end where Length says"
    out += a.lines

    out += ["", "SERVER -> CLIENT (offsets count from the first byte the server sends)"]
    b = Annotator(response)
    b.lines.append("      -- frame 1: response HEADERS (SPEC section 6) --")
    length, sid, end = b.frame_header(HEADERS, 0)
    assert sid == 1
    status = b.num(2, "Status 0x%(x)s = %(v)d")
    assert status == 200
    names = dict(b.entries())
    assert b.pos == end, "HEADERS payload does not end where Length says"
    b.lines.append("      -- frame 2: response DATA, the body --")
    length, sid, end = b.frame_header(DATA, FLAG_END_STREAM)
    assert length == int(names["content-length"]), "DATA length != content-length"
    b.take(length, '"%s"' % b.data[b.pos:end].decode().replace("\n", "\\n"))
    assert b.pos == len(response)
    out += b.lines

    c = Annotator(goaway, base=len(request))
    length, sid, end = c.frame_header(GOAWAY, 0)
    assert (length, sid) == (2, 0)
    code = c.num(2, "Error code %(v)d = normal")
    assert code == ERR_NONE and c.pos == len(goaway)
    out += ["", "The connection stays open; a next request would use stream 2.",
            "CLIENT -> SERVER, when done (then FIN; SPEC section 10, graceful shutdown)"]
    out += c.lines
    return out


def write_md(lines, request, response, host, port, date):
    hl = int.from_bytes(response[0:3], "big")
    rl = int.from_bytes(request[4:7], "big")
    # The sums printed in the table, checked against the Length fields.
    assert 1 + 2 + 11 + 1 + (3 + len(host)) + (3 + 7) + (3 + 3) == rl == len(request) - 12
    assert 2 + 1 + (3 + 8) + (3 + 29) + (3 + 2) + (3 + 9) + (3 + 29) + (3 + 12) + (3 + 8) == hl
    port_note = "" if port == PREFERRED_PORT else (
        "\n- **Port:** 9000 was busy when this copy was generated, so bserve ran on "
        "port %d and the host value says so. Free port 9000 and rerun `make_hexdump.py` "
        "before submitting." % port)
    md = """# Annotated hexdump: one GET /index.html, one 200 response

Captured by `make_hexdump.py`: the real `bcurl -v %(host)s/index.html` against a
live `bserve` on port %(port)d. Both byte streams are rebuilt from bcurl's own
`-v` hexdump, so every byte below crossed the wire, the date included.
`tests/test_golden.py` checks this file again without importing the codec.

- **Date:** `%(date)s`, the server's clock during this run, and the only
  value that changes between runs. The tests mask exactly these 29 bytes
  (response offset 0x19) and compare every other byte.
- **Last-modified and etag:** the generator sets `index.html`'s mtime to
  %(lm_epoch)d (`%(lm)s`), so etag is `%(etag)s`: the size (12 = 0x%(size)x)
  and the mtime (0x%(lm_epoch)x), both in hex.
- **Host:** `%(host)s`, because bserve was bound to port %(port)d.%(port_note)s

| Frame | Bytes | Length field | Check |
|---|---|---|---|
| preface | 4 | n/a | `BHT1` |
| request HEADERS | 8 + %(rl)d | 0x%(rl)06x | 1 + 2 + 11 + 1 + (3+%(hostlen)d) + (3+7) + (3+3) = %(rl)d |
| response HEADERS | 8 + %(hl)d | 0x%(hl)06x | 2 + 1 + (3+8) + (3+29) + (3+2) + (3+9) + (3+29) + (3+12) + (3+8) = %(hl)d |
| response DATA | 8 + 12 | 0x00000c | equals content-length "12" |
| GOAWAY | 8 + 2 | 0x000002 | SPEC section 4: Length 2, stream 0 |

Every request entry uses a static index (SPEC section 7: 1 host, 2 user-agent,
3 accept); every response entry too (6 server, 7 date, 5 content-length,
4 content-type, 9 last-modified, 8 etag, 10 cache-control). A receiver that
cannot annotate its bytes cannot parse them: every length here is checked
against the bytes that follow (SPEC section 7, parsing rule).

```
%(dump)s
```
""" % {"date": date, "lm_epoch": LM_EPOCH, "lm": LAST_MODIFIED, "size": len(BODY),
       "etag": ETAG, "rl": rl, "hl": hl, "host": host, "hostlen": len(host),
       "port": port, "port_note": port_note, "dump": "\n".join(lines)}
    with open(os.path.join(HERE, "HEXDUMP.md"), "w") as f:
        f.write(md)


def main():
    sent, received, port = live_exchange()
    host = "localhost:%d" % port
    request, response, goaway = golden(host)
    if sent != request + goaway:
        sys.exit("bcurl's bytes differ from the codec-built preface + request + GOAWAY(0)")
    if mask_date(received) != response:
        sys.exit("bserve's response differs from the codec-built response (date masked)")
    date = received[DATE_AT:DATE_AT + 29].decode("ascii")
    lines = annotate(sent[:len(request)], received, sent[len(request):])
    with open(os.path.join(HERE, "HEXDUMP.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    write_md(lines, sent[:len(request)], received, host, port, date)
    print("HEXDUMP.txt and HEXDUMP.md written from bcurl -v against bserve on port %d "
          "(live date %s)" % (port, date))
    if port != PREFERRED_PORT:
        sys.stderr.write("make_hexdump: port 9000 was busy; free it and rerun "
                         "before submitting\n")


if __name__ == "__main__":
    main()
