"""Golden-vector tests: hard-coded wire bytes, checked without the codec.

These never import bhttp.py for building or parsing, so a symmetric mistake
in the codec cannot pass them. GOLDEN_RESPONSE is bserve's exact response to
GOLDEN_REQUEST with one stand-in: the 29-byte date value at DATE_AT, which is
the server's wall clock and changes every run. Every comparison checks that
those 29 bytes are an IMF-fixdate, masks exactly them, and compares every
other byte. HEXDUMP.txt and HEXDUMP.md (a real capture, live date included)
and the SPEC.md examples are checked against the same bytes, so the
deliverables cannot drift apart.
"""
import os, re, socket, struct, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, BCURL, start_server, stop, read_all

PREFACE = b"BHT1"
MASK = b"Thu, 24 Sep 2026 10:54:34 GMT"           # stand-in for the live date
LAST_MODIFIED = b"Thu, 24 Sep 2026 10:50:00 GMT"  # mtime 1790247000
LM_EPOCH = 1790247000
ETAG = b'"c-6ab50058"'                            # size 0xc, mtime 0x6ab50058
BODY = b"<h1>hi</h1>\n"
IMF = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d\d "
                 r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} "
                 r"\d\d:\d\d:\d\d GMT$")

# Client -> server, for host "localhost:9000" (the brief's port).
GOLDEN_REQUEST = bytes.fromhex(
    "42485431"                                   # preface
    "000030" "00" "01" "000001"                  # Length 48, HEADERS, END_STREAM, stream 1
    "01" "000b" "2f696e6465782e68746d6c"         # GET, path length 11, "/index.html"
    "03"                                         # 3 headers
    "01000e" "6c6f63616c686f73743a39303030"      # host "localhost:9000"
    "020007" "626375726c2f31"                    # user-agent "bcurl/1"
    "030003" "2a2f2a")                           # accept "*/*"

# Server -> client, byte for byte except the date value (MASK).
GOLDEN_RESPONSE = (
    bytes.fromhex("000079" "00" "00" "000001")   # Length 121, HEADERS, flags 0, stream 1
    + bytes.fromhex("00c8") + b"\x07"            # status 200, 7 headers
    + bytes.fromhex("060008") + b"bserve/1"      # 6 server
    + bytes.fromhex("07001d") + MASK             # 7 date (live value masked)
    + bytes.fromhex("050002") + b"12"            # 5 content-length, ASCII decimal
    + bytes.fromhex("040009") + b"text/html"     # 4 content-type
    + bytes.fromhex("09001d") + LAST_MODIFIED    # 9 last-modified
    + bytes.fromhex("08000c") + ETAG             # 8 etag
    + bytes.fromhex("0a0008") + b"no-cache"      # 10 cache-control
    + bytes.fromhex("00000c" "01" "01" "000001")  # Length 12, DATA, END_STREAM, stream 1
    + BODY)

GOLDEN_GOAWAY = bytes.fromhex("00000202000000000000")   # Length 2, GOAWAY, stream 0, code 0

STATIC = ["host", "user-agent", "accept", "content-type", "content-length",
          "server", "date", "etag", "last-modified", "cache-control"]

DATE_AT = 8 + 2 + 1 + 3 + 8 + 3                  # 0x19: offset of the date value


def mask_date(response):
    """Check the date entry and its live value, then replace the value with MASK."""
    if response[DATE_AT - 3:DATE_AT] != b"\x07\x00\x1d":
        raise AssertionError("no 29-byte date entry before offset %d" % DATE_AT)
    live = response[DATE_AT:DATE_AT + 29].decode("ascii", "replace")
    if not IMF.match(live):
        raise AssertionError("date value %r is not an IMF-fixdate" % live)
    return response[:DATE_AT] + MASK + response[DATE_AT + 29:]


def hand_request(host_value):
    """The request frame bcurl must send, built by hand from the spec."""
    path = b"/index.html"
    payload = b"\x01" + struct.pack(">H", len(path)) + path
    entries = [(1, host_value), (2, b"bcurl/1"), (3, b"*/*")]
    payload += bytes([len(entries)])
    for idx, value in entries:
        payload += bytes([idx]) + struct.pack(">H", len(value)) + value
    return (len(payload).to_bytes(3, "big") + b"\x00\x01" +
            (1).to_bytes(3, "big") + payload)


def hand_parse_headers(payload):
    """Parse a response HEADERS payload by hand. Returns (status, [(name, value)])."""
    status = struct.unpack(">H", payload[:2])[0]
    pos, pairs = 3, []
    for _ in range(payload[2]):
        idx = payload[pos]; pos += 1
        if idx == 0:
            nl = payload[pos]; pos += 1
            name = payload[pos:pos + nl].decode(); pos += nl
        else:
            name = STATIC[idx - 1]
        (vl,) = struct.unpack(">H", payload[pos:pos + 2]); pos += 2
        pairs.append((name, payload[pos:pos + vl].decode())); pos += vl
    assert pos == len(payload)
    return status, pairs


def dump_streams(text):
    """Rebuild the byte streams from an annotated dump: the hex columns of
    every "OFFSET  hex ...  # note" line, split at the direction headers."""
    streams, cur = {}, None
    for line in text.splitlines():
        if line.startswith("CLIENT -> SERVER"):
            cur = "c2s"
        elif line.startswith("SERVER -> CLIENT"):
            cur = "s2c"
        m = re.match(r"^([0-9a-f]{4})  ((?:[0-9a-f]{2} ?)+?)\s+#", line)
        if m and cur:
            streams.setdefault(cur, bytearray()).extend(bytes.fromhex(m.group(2)))
    return {k: bytes(v) for k, v in streams.items()}


class GoldenTests(unittest.TestCase):
    def test_golden_bytes_are_self_consistent(self):
        self.assertEqual(int.from_bytes(GOLDEN_REQUEST[4:7], "big"), len(GOLDEN_REQUEST) - 12)
        hl = int.from_bytes(GOLDEN_RESPONSE[0:3], "big")
        self.assertEqual(hl, 121)
        self.assertEqual(GOLDEN_RESPONSE[8 + hl:8 + hl + 8], bytes.fromhex("00000c0101000001"))
        self.assertEqual(GOLDEN_RESPONSE[DATE_AT:DATE_AT + 29], MASK)
        self.assertEqual(mask_date(GOLDEN_RESPONSE), GOLDEN_RESPONSE)
        self.assertEqual(GOLDEN_REQUEST[4:], hand_request(b"localhost:9000"))

    def test_hexdump_files_carry_a_real_exchange(self):
        streams = {}
        for name in ("HEXDUMP.txt", "HEXDUMP.md"):
            with open(os.path.join(ROOT, name)) as f:
                s = dump_streams(f.read())
            streams[name] = s
            c2s, s2c = s["c2s"], s["s2c"]
            self.assertEqual(c2s[0x1b], 1, name)            # host is the first entry
            hlen = int.from_bytes(c2s[0x1c:0x1e], "big")
            host = c2s[0x1e:0x1e + hlen]
            self.assertRegex(host.decode("ascii"), r"^localhost:\d+$", name)
            self.assertEqual(c2s, PREFACE + hand_request(host) + GOLDEN_GOAWAY, name)
            self.assertEqual(mask_date(s2c), GOLDEN_RESPONSE, name)
        self.assertEqual(streams["HEXDUMP.txt"], streams["HEXDUMP.md"])

    def test_spec_examples_match_golden_frame_headers(self):
        with open(os.path.join(ROOT, "SPEC.md")) as f:
            spec = f.read()
        for frame_header in (GOLDEN_REQUEST[4:12], GOLDEN_RESPONSE[:8],
                             GOLDEN_RESPONSE[129:137]):
            self.assertIn(" ".join("%02x" % b for b in frame_header), spec)
        self.assertIn("8 etag", spec)

    def test_bcurl_sends_exact_golden_bytes_and_accepts_golden_response(self):
        state = {}
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def script():
            c, _ = srv.accept()
            c.settimeout(10)
            buf = b""
            while len(buf) < 12:
                buf += c.recv(4096)
            want = 12 + int.from_bytes(buf[4:7], "big")
            while len(buf) < want:
                buf += c.recv(4096)
            state["request"] = buf[:want]
            c.sendall(GOLDEN_RESPONSE)
            state["rest"] = buf[want:] + read_all(c, 10)
            c.close()
        t = threading.Thread(target=script, daemon=True)
        t.start()
        r = subprocess.run([BCURL, "127.0.0.1:%d/index.html" % port],
                           capture_output=True, timeout=30)
        t.join(10)
        srv.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, BODY)
        host = ("127.0.0.1:%d" % port).encode()
        self.assertEqual(state["request"], PREFACE + hand_request(host))
        self.assertEqual(state["rest"], GOLDEN_GOAWAY)      # then a clean FIN

    def test_bserve_answers_golden_request_byte_for_byte(self):
        www = tempfile.mkdtemp()
        index = os.path.join(www, "index.html")
        with open(index, "wb") as f:
            f.write(BODY)
        os.utime(index, (LM_EPOCH, LM_EPOCH))
        proc, port = start_server(www)
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(GOLDEN_REQUEST)
            buf = b""
            while len(buf) < len(GOLDEN_RESPONSE):
                chunk = c.recv(4096)
                self.assertTrue(chunk, "server closed early")
                buf += chunk
            c.sendall(GOLDEN_GOAWAY)
            c.shutdown(socket.SHUT_WR)
            self.assertEqual(read_all(c), b"")            # nothing more, FIN not RST
            c.close()
        finally:
            stop(proc)
        self.assertEqual(mask_date(buf), GOLDEN_RESPONSE)
        # and every header value, parsed by hand
        live_date = buf[DATE_AT:DATE_AT + 29].decode()
        hl = int.from_bytes(buf[0:3], "big")
        status, pairs = hand_parse_headers(buf[8:8 + hl])
        self.assertEqual(status, 200)
        self.assertEqual(pairs, [("server", "bserve/1"), ("date", live_date),
                                 ("content-length", "12"), ("content-type", "text/html"),
                                 ("last-modified", LAST_MODIFIED.decode()),
                                 ("etag", ETAG.decode()), ("cache-control", "no-cache")])


if __name__ == "__main__":
    unittest.main()
