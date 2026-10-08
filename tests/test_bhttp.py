"""BHTTP/1 test suite: server, client, codec, timeouts, limits, shutdown.

Run: python3 -m unittest discover -s tests -v
Server tests run the real bserve as a subprocess and talk raw sockets;
in-process tests load bserve/bcurl as modules over socketpairs.
Client tests run the real bcurl against a scripted fake server; several of
its error paths are built by hand (hand_frame, hand_headers), not by the codec.
"""
import io, os, socket, struct, sys, tempfile, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import (ROOT, BSERVE, BCURL, load_script, start_server, stop,
                     connect, read_all, run, FakeServer, goaway_from)
import bhttp
from bhttp import (PREFACE, MAX_PAYLOAD, HEADERS, DATA, GOAWAY, FLAG_END_STREAM,
                   METHOD_GET, METHOD_HEAD, ERR_NONE, ERR_PROTOCOL, ERR_FRAME_SIZE, ERR_PREFACE,
                   ERR_INTERNAL, STATIC_TABLE, build_frame, build_goaway, recv_frame,
                   encode_request, decode_request, encode_response, decode_response,
                   encode_headers, decode_headers, parse_content_length,
                   Malformed, ProtocolError, GoawayReceived)

AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
INDEX = b"<h1>hi</h1>\n"
BLOB = (bytes(range(256)) * 157)[:40000]


def request(path, method=METHOD_GET, stream=1, pairs=None, flags=FLAG_END_STREAM):
    pairs = pairs if pairs is not None else [("host", "x"), ("user-agent", "t"), ("accept", "*/*")]
    return build_frame(HEADERS, flags, stream, encode_request(method, path, pairs))


def read_response(c, stream=None):
    """Read one whole response. Returns (status, headers dict, body, frames)."""
    status, hdrs, body, frames = None, {}, b"", []
    while True:
        ftype, flags, sid, payload = recv_frame(c, idle=5)
        frames.append((ftype, flags, sid, len(payload)))
        if ftype == GOAWAY:
            return ("GOAWAY", struct.unpack(">H", payload)[0]), hdrs, body, frames
        if ftype == HEADERS:
            status, pairs = decode_response(payload)
            hdrs = dict(pairs)
        elif ftype == DATA:
            body += payload
        if flags & FLAG_END_STREAM:
            return status, hdrs, body, frames


def write_file(path, text):
    with open(path, "w") as f:
        f.write(text)


def make_root():
    w = tempfile.mkdtemp()
    with open(os.path.join(w, "index.html"), "wb") as f:
        f.write(INDEX)
    with open(os.path.join(w, "blob.bin"), "wb") as f:
        f.write(BLOB)
    return w


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.www = w = make_root()
        with open(os.path.join(w, "exact.bin"), "wb") as f:
            f.write(b"e" * 16384)
        write_file(os.path.join(w, "empty.txt"), "")
        write_file(os.path.join(w, "a%20b.txt"), "literal\n")
        os.mkdir(os.path.join(w, "sub"))
        write_file(os.path.join(w, "sub", "index.html"), "sub index\n")
        os.mkdir(os.path.join(w, "noindex"))
        os.mkdir(os.path.join(w, "dironly"))
        os.mkfifo(os.path.join(w, "fifo"))
        write_file(os.path.join(w, "locked.txt"), "secret")
        os.chmod(os.path.join(w, "locked.txt"), 0)
        cls.outside_dir = tempfile.mkdtemp()
        cls.outside = os.path.join(cls.outside_dir, "secret.txt")
        write_file(cls.outside, "outside the root\n")
        os.symlink(cls.outside, os.path.join(w, "escape.txt"))
        os.symlink(cls.outside_dir, os.path.join(w, "escape-dir"))
        os.symlink(cls.outside, os.path.join(w, "noindex", "index.html"))
        os.symlink(os.path.join(w, "index.html"), os.path.join(w, "inside-link.html"))
        cls.proc, cls.port = start_server(w)

    @classmethod
    def tearDownClass(cls):
        stop(cls.proc)
        os.chmod(os.path.join(cls.www, "locked.txt"), 0o600)

    def get(self, path, **kw):
        c = connect(self.port)
        c.sendall(request(path, **kw))
        r = read_response(c)
        c.close()
        return r

    def assert_400_then_alive(self, frame, stream=1):
        """The brief: 400 if malformed, and keep the connection open."""
        c = connect(self.port)
        c.sendall(frame)
        status, hdrs, body, frames = read_response(c)
        self.assertEqual(status, 400)
        self.assertEqual(body, b"")
        self.assertEqual(frames, [(HEADERS, FLAG_END_STREAM, stream, frames[0][3])])
        self.assertEqual(hdrs["content-length"], "0")
        c.sendall(request("/index.html", stream=stream + 1))
        status, _, body, _ = read_response(c)
        self.assertEqual((status, body), (200, INDEX))
        c.close()

    # ---- happy paths and boundaries (SPEC section 8 examples) ----

    def test_get_200_exact_body_and_headers(self):
        status, hdrs, body, frames = self.get("/index.html")
        self.assertEqual((status, body), (200, INDEX))
        st = os.stat(os.path.join(self.www, "index.html"))
        self.assertEqual(hdrs["content-length"], "12")
        self.assertEqual(hdrs["content-type"], "text/html")
        self.assertEqual(hdrs["server"], "bserve/1")
        self.assertEqual(hdrs["cache-control"], "no-cache")
        self.assertEqual(hdrs["etag"], '"c-%x"' % int(st.st_mtime))
        self.assertRegex(hdrs["date"], r"^[A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d GMT$")
        self.assertRegex(hdrs["last-modified"], r" GMT$")
        self.assertEqual([f[:2] for f in frames], [(HEADERS, 0), (DATA, FLAG_END_STREAM)])

    def test_404_then_200_same_connection(self):
        c = connect(self.port)
        c.sendall(request("/missing.txt", stream=1))
        status, hdrs, body, frames = read_response(c)
        self.assertEqual((status, body, len(frames)), (404, b"", 1))
        c.sendall(request("/index.html", stream=2))
        self.assertEqual(read_response(c)[:3:2], (200, INDEX))
        c.close()

    def test_three_requests_one_connection(self):
        c = connect(self.port)
        for n in (1, 2, 3):
            c.sendall(request("/index.html", stream=n))
            status, _, body, frames = read_response(c)
            self.assertEqual((status, body), (200, INDEX))
            self.assertTrue(all(f[2] == n for f in frames))
        c.close()

    def test_40000_byte_file_three_data_frames(self):
        status, _, body, frames = self.get("/blob.bin")
        self.assertEqual(body, BLOB)
        self.assertEqual([f[3] for f in frames if f[0] == DATA], [16384, 16384, 7232])
        self.assertEqual([f[1] for f in frames], [0, 0, 0, FLAG_END_STREAM])

    def test_exactly_16384_byte_file_one_frame(self):
        _, _, body, frames = self.get("/exact.bin")
        self.assertEqual([f[3] for f in frames if f[0] == DATA], [16384])

    def test_headers_payload_exactly_16384_accepted(self):
        base = encode_request(METHOD_GET, "/index.html", [("host", "x")])
        # second entry: idx 0x00 (1) + name length (1) + "x-padding" (9) + value length (2)
        value = "v" * (16384 - len(base) - 1 - 1 - 9 - 2)
        payload = encode_request(METHOD_GET, "/index.html", [("host", "x"), ("x-padding", value)])
        self.assertEqual(len(payload), 16384)
        c = connect(self.port)
        c.sendall(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))
        self.assertEqual(read_response(c)[0], 200)
        c.close()

    def test_head_one_headers_frame_no_data(self):
        status, hdrs, body, frames = self.get("/blob.bin", method=METHOD_HEAD)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(hdrs["content-length"], "40000")
        self.assertEqual([f[:2] for f in frames], [(HEADERS, FLAG_END_STREAM)])

    def test_empty_file_200_no_data(self):
        status, hdrs, body, frames = self.get("/empty.txt")
        self.assertEqual((status, hdrs["content-length"]), (200, "0"))
        self.assertEqual([f[:2] for f in frames], [(HEADERS, FLAG_END_STREAM)])

    def test_304_on_matching_etag_and_star(self):
        etag = self.get("/blob.bin", method=METHOD_HEAD)[1]["etag"]
        for tag in (etag, "*"):
            status, hdrs, body, frames = self.get(
                "/blob.bin", pairs=[("host", "x"), ("if-none-match", tag)])
            self.assertEqual(status, 304)
            self.assertEqual(hdrs["content-length"], "40000")
            self.assertEqual(hdrs["etag"], etag)
            self.assertEqual([f[:2] for f in frames], [(HEADERS, FLAG_END_STREAM)])
        status, _, body, _ = self.get("/blob.bin", pairs=[("if-none-match", '"other"')])
        self.assertEqual((status, body), (200, BLOB))

    def test_if_none_match_on_head_304(self):
        etag = self.get("/blob.bin", method=METHOD_HEAD)[1]["etag"]
        status, hdrs, body, frames = self.get(
            "/blob.bin", method=METHOD_HEAD, pairs=[("host", "x"), ("if-none-match", etag)])
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(hdrs["content-length"], "40000")
        self.assertEqual([f[:2] for f in frames], [(HEADERS, FLAG_END_STREAM)])

    def test_root_and_directory_map_to_index(self):
        self.assertEqual(self.get("/")[:3:2], (200, INDEX))
        self.assertEqual(self.get("/sub/")[:3:2], (200, b"sub index\n"))
        self.assertEqual(self.get("/sub")[:3:2], (200, b"sub index\n"))
        self.assertEqual(self.get("/dironly/")[0], 404)

    def test_empty_and_dot_segments_ignored(self):
        for p in ("//index.html", "/./index.html", "/sub/./index.html"):
            self.assertEqual(self.get(p)[0], 200, p)

    def test_trailing_slash_on_file_404(self):
        # SPEC section 5: dot segments go first, then the trailing-slash rule
        # looks at the path as sent.
        self.assertEqual(self.get("/index.html/")[0], 404)
        self.assertEqual(self.get("/index.html/.")[:3:2], (200, INDEX))

    def test_path_used_literally_no_percent_decoding(self):
        self.assertEqual(self.get("/a%20b.txt")[:3:2], (200, b"literal\n"))
        self.assertEqual(self.get("/a b.txt")[0], 404)
        self.assertEqual(self.get("/%2e%2e/index.html")[0], 404)

    def test_overlong_path_component_404_connection_alive(self):
        c = connect(self.port)
        c.sendall(request("/" + "a" * 300, stream=1))
        self.assertEqual(read_response(c)[0], 404)
        c.sendall(request("/sub/" + "b" * 300, stream=2))
        self.assertEqual(read_response(c)[0], 404)
        c.sendall(request("/index.html", stream=3))
        self.assertEqual(read_response(c)[:3:2], (200, INDEX))
        c.close()

    def test_fifo_is_not_served(self):
        self.assertEqual(self.get("/fifo")[0], 404)

    @unittest.skipIf(AS_ROOT, "root can read mode-000 files; see InProcessServerTests")
    def test_unreadable_file_500_connection_stays_open(self):
        c = connect(self.port)
        c.sendall(request("/locked.txt"))
        self.assertEqual(read_response(c)[0], 500)
        c.sendall(request("/index.html", stream=2))
        self.assertEqual(read_response(c)[0], 200)
        c.close()

    def test_request_without_end_stream_is_served(self):
        self.assertEqual(self.get("/index.html", flags=0)[0], 200)

    def test_unknown_frame_types_skipped_any_stream(self):
        c = connect(self.port)
        c.sendall(build_frame(0x55, 0, 9, b"future v2 frame"))
        c.sendall(build_frame(0xFF, 0xFF, 0, b""))
        c.sendall(build_frame(0x03, 0, 1, b"x" * MAX_PAYLOAD))   # at the cap: skip, not error
        c.sendall(request("/index.html"))
        self.assertEqual(read_response(c)[:3:2], (200, INDEX))
        c.close()

    def test_unknown_flag_bits_ignored(self):
        self.assertEqual(self.get("/index.html", flags=FLAG_END_STREAM | 0x80)[0], 200)

    def test_data_from_client_skipped(self):
        c = connect(self.port)
        c.sendall(build_frame(DATA, 0, 1, b"stray body bytes"))
        c.sendall(request("/index.html"))
        self.assertEqual(read_response(c)[0], 200)
        c.close()

    # ---- malformed payload: 400 and the connection stays open ----

    def test_400_unknown_method(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, b"\x07\x00\x01/\x00"))

    def test_400_path_without_slash(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, encode_request(METHOD_GET, "index.html", [])))

    def test_400_dotdot(self):
        self.assert_400_then_alive(request("/../etc/passwd"))

    def test_400_dotdot_deeper(self):
        self.assert_400_then_alive(request("/a/../../b"))

    def test_400_nul_in_path(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, encode_request(METHOD_GET, "/a\x00b", [])))

    def test_400_reserved_header_index(self):
        for idx in (0x0B, 0xFF):
            payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + bytes([1, idx, 0, 1]) + b"v"
            self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_uppercase_and_non_ascii_names(self):
        for name in (b"Host", b"x_y", "\u00e4".encode()):
            payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + \
                bytes([1, 0x00, len(name)]) + name + struct.pack(">H", 1) + b"x"
            self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_empty_literal_name(self):
        payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + \
            bytes([1, 0x00, 0x00]) + struct.pack(">H", 1) + b"x"
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_bad_utf8_header_value(self):
        payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + \
            bytes([1, 0x01]) + struct.pack(">H", 1) + b"\xff"
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_duplicate_header(self):
        payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + bytes([2]) + \
            (bytes([1]) + struct.pack(">H", 1) + b"a") * 2
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_literal_equal_to_static_is_duplicate(self):
        payload = bytes([METHOD_GET]) + struct.pack(">H", 1) + b"/" + \
            bytes([2, 0x01]) + struct.pack(">H", 1) + b"a" + \
            bytes([0x00, 4]) + b"host" + struct.pack(">H", 1) + b"b"
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, payload))

    def test_400_trailing_bytes_overrun(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1,
                                              encode_request(METHOD_GET, "/index.html", []) + b"junk"))

    def test_400_length_past_frame_underrun(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1,
                                              bytes([METHOD_GET]) + struct.pack(">H", 100) + b"/short"))

    def test_400_bad_utf8_path(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1,
                                              bytes([METHOD_GET]) + struct.pack(">H", 2) + b"/\xff" + bytes([0])))

    def test_400_empty_payload(self):
        self.assert_400_then_alive(build_frame(HEADERS, FLAG_END_STREAM, 1, b""))

    def test_non_increasing_and_repeated_stream_id_400(self):
        c = connect(self.port)
        c.sendall(request("/index.html", stream=5))
        self.assertEqual(read_response(c)[0], 200)
        for sid in (3, 5):                      # lower, and a repeat of the same ID
            c.sendall(request("/index.html", stream=sid))
            self.assertEqual(read_response(c)[0], 400)
        c.sendall(request("/index.html", stream=6))
        self.assertEqual(read_response(c)[0], 200)
        c.close()

    # ---- path safety ----

    def test_symlink_escape_404(self):
        self.assertEqual(self.get("/escape.txt")[0], 404)

    def test_symlinked_directory_escape_404(self):
        self.assertEqual(self.get("/escape-dir/secret.txt")[0], 404)

    def test_symlinked_index_html_escape_404(self):
        self.assertEqual(self.get("/noindex/")[0], 404)

    def test_symlink_inside_root_200(self):
        self.assertEqual(self.get("/inside-link.html")[:3:2], (200, INDEX))

    # ---- connection errors: GOAWAY, then FIN (clean EOF), never RST ----

    def assert_goaway_then_clean_eof(self, c, code):
        raw = read_all(c)                       # ConnectionResetError here = RST
        self.assertEqual(goaway_from(raw), code)
        c.close()

    def test_stream_zero_request_protocol_error(self):
        c = connect(self.port)
        c.sendall(request("/index.html", stream=0) + request("/index.html", stream=1))
        self.assert_goaway_then_clean_eof(c, ERR_PROTOCOL)

    def test_bad_preface_goaway3(self):
        c = connect(self.port, preface=False)
        c.sendall(b"XXXX")
        self.assert_goaway_then_clean_eof(c, ERR_PREFACE)

    def test_http1_client_gets_goaway3_not_rst(self):
        for _ in range(10):
            c = connect(self.port, preface=False)
            c.sendall(b"GET / HTTP/1.1\r\nHost: localhost:9000\r\nUser-Agent: curl/8\r\n"
                      b"Accept: */*\r\n\r\n")
            self.assert_goaway_then_clean_eof(c, ERR_PREFACE)

    def test_oversize_length_goaway2_with_unread_payload_no_rst(self):
        # The old close() with unread input sent RST and lost the GOAWAY.
        for _ in range(20):
            c = connect(self.port)
            c.sendall((MAX_PAYLOAD + 1).to_bytes(3, "big") + bytes((HEADERS, 0)) +
                      (1).to_bytes(3, "big") + b"p" * 20000)
            self.assert_goaway_then_clean_eof(c, ERR_FRAME_SIZE)

    def test_oversize_length_on_unknown_type_goaway2(self):
        c = connect(self.port)
        c.sendall((MAX_PAYLOAD + 1).to_bytes(3, "big") + bytes((0x77, 0)) + (0).to_bytes(3, "big"))
        self.assert_goaway_then_clean_eof(c, ERR_FRAME_SIZE)

    def test_cut_mid_frame_goaway1(self):
        c = connect(self.port)
        c.sendall((100).to_bytes(3, "big") + bytes((HEADERS, 0)) + (1).to_bytes(3, "big") + b"partial")
        c.shutdown(socket.SHUT_WR)
        self.assert_goaway_then_clean_eof(c, ERR_PROTOCOL)

    def test_goaway_bad_length_protocol_error(self):
        c = connect(self.port)
        c.sendall(build_frame(GOAWAY, 0, 0, b"\x00"))
        self.assert_goaway_then_clean_eof(c, ERR_PROTOCOL)

    def test_goaway_nonzero_stream_protocol_error(self):
        c = connect(self.port)
        c.sendall(build_frame(GOAWAY, 0, 7, b"\x00\x00"))
        self.assert_goaway_then_clean_eof(c, ERR_PROTOCOL)

    def test_clean_goaway_server_sends_nothing_more(self):
        c = connect(self.port)
        c.sendall(build_goaway(ERR_NONE) + request("/index.html", stream=1))
        self.assertEqual(read_all(c), b"")      # no reply, no response to the late request
        c.close()


class TimeoutTests(unittest.TestCase):
    def test_idle_connection_gets_goaway0_then_fin(self):
        proc, port = start_server(make_root(), BSERVE_IDLE_TIMEOUT=0.5)
        try:
            c = connect(port)
            self.assertEqual(goaway_from(read_all(c)), ERR_NONE)
            c.close()
        finally:
            stop(proc)

    def test_idle_timer_ignores_unknown_frames(self):
        # SPEC 10: idle means no request; unknown frames must not refresh it
        proc, port = start_server(make_root(), BSERVE_IDLE_TIMEOUT=0.5)
        try:
            c = connect(port)
            t0 = time.monotonic()
            for _ in range(4):
                c.sendall((4).to_bytes(3, "big") + bytes((0x99, 0)) + (0).to_bytes(3, "big") + b"junk")
                time.sleep(0.3)
            self.assertEqual(goaway_from(read_all(c)), ERR_NONE)
            self.assertLess(time.monotonic() - t0, 1.5)  # 0.5 s budget, not 1.2 s of frames
            c.close()
        finally:
            stop(proc)

    def test_slow_preface_closed_without_goaway(self):
        proc, port = start_server(make_root(), BHTTP_FRAME_TIMEOUT=0.5)
        try:
            c = connect(port, preface=False)
            c.sendall(b"BH")
            self.assertEqual(read_all(c), b"")
            c.close()
        finally:
            stop(proc)

    def test_frame_too_slow_goaway1(self):
        proc, port = start_server(make_root(), BHTTP_FRAME_TIMEOUT=0.5)
        try:
            c = connect(port)
            c.sendall((100).to_bytes(3, "big") + bytes((HEADERS, 0)) + (1).to_bytes(3, "big") + b"x" * 10)
            t0 = time.monotonic()
            self.assertEqual(goaway_from(read_all(c)), ERR_PROTOCOL)
            self.assertLess(time.monotonic() - t0, 4)
            c.close()
        finally:
            stop(proc)

    def test_bad_environment_values_exit_2(self):
        for env in ({"BSERVE_IDLE_TIMEOUT": "nan"}, {"BSERVE_IDLE_TIMEOUT": "-1"},
                    {"BHTTP_FRAME_TIMEOUT": "soon"}, {"BSERVE_MAX_CONNECTIONS": "0"},
                    {"BSERVE_MAX_CONNECTIONS": "lots"}):
            r = run([BSERVE, make_root(), "0"], env)
            self.assertEqual(r.returncode, 2, env)
            self.assertNotIn(b"Traceback", r.stderr)

    def test_bad_cli_exit_2(self):
        www = make_root()
        for argv in ([], [www], [www, "port"], [www, "70000"], ["/no/such/dir", "0"]):
            self.assertEqual(run([BSERVE] + argv).returncode, 2, argv)


class LimitTests(unittest.TestCase):
    def test_full_server_refuses_without_goaway(self):
        proc, port = start_server(make_root(), BSERVE_MAX_CONNECTIONS=1)
        try:
            c1 = connect(port)                  # holds the only slot
            c1.sendall(request("/index.html"))
            self.assertEqual(read_response(c1)[0], 200)
            c2 = connect(port, preface=False)
            self.assertEqual(read_all(c2, 3), b"")      # refused: closed, no GOAWAY
            c2.close()
            c1.close()
        finally:
            stop(proc)

    @unittest.skipUnless(socket.has_dualstack_ipv6(), "host has no dual-stack IPv6")
    def test_dual_stack_ipv6_and_ipv4(self):
        proc, port = start_server(make_root())
        try:
            for host in ("::1", "127.0.0.1"):
                c = connect(port, host=host)
                c.sendall(request("/index.html"))
                self.assertEqual(read_response(c)[0], 200, host)
                c.close()
        finally:
            stop(proc)


class InProcessServerTests(unittest.TestCase):
    """bserve loaded as a module; one serve_connection() per socketpair."""
    @classmethod
    def setUpClass(cls):
        cls.bs = load_script(BSERVE, "bserve_mod")
        cls.www = os.path.realpath(make_root())

    def serve(self):
        a, b = socket.socketpair()
        self.errors = []

        def target():
            try:
                self.bs.serve_connection(a, self.www)
            except BaseException as e:          # serve_connection() must never raise
                self.errors.append(e)
        t = threading.Thread(target=target, daemon=True)
        t.start()
        b.settimeout(5)
        b.sendall(PREFACE)
        return b, t

    def test_500_on_oserror_then_connection_alive(self):
        b, t = self.serve()
        with mock.patch.object(self.bs, "open_in_root", side_effect=PermissionError("denied")):
            b.sendall(request("/index.html", stream=1))
            status, hdrs, body, frames = read_response(b)
        self.assertEqual((status, body, len(frames)), (500, b"", 1))
        b.sendall(request("/index.html", stream=2))
        self.assertEqual(read_response(b)[:3:2], (200, INDEX))
        b.close()
        t.join(3)
        self.assertEqual(self.errors, [])

    def test_goaway4_when_file_fails_mid_response(self):
        class Shrinks(object):
            def __init__(self):
                self.calls = 0
            def read(self, n):
                self.calls += 1
                return b"z" * n if self.calls == 1 else b""
            def close(self):
                pass
        st = os.stat(os.path.join(self.www, "blob.bin"))
        b, t = self.serve()
        with mock.patch.object(self.bs, "open_in_root",
                               return_value=(Shrinks(), st, "/x/blob.bin")):
            b.sendall(request("/blob.bin"))
            ftype, flags, sid, payload = recv_frame(b, idle=5)
            self.assertEqual((ftype, flags, decode_response(payload)[0]), (HEADERS, 0, 200))
            ftype, flags, sid, payload = recv_frame(b, idle=5)
            self.assertEqual((ftype, len(payload)), (DATA, 16384))
            self.assertEqual(goaway_from(read_all(b)), ERR_INTERNAL)
        b.close()
        t.join(3)
        self.assertEqual(self.errors, [])

    def test_peer_reset_mid_response_is_quiet(self):
        b, t = self.serve()
        b.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        b.sendall(request("/blob.bin"))
        b.close()                               # RST while the server sends
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.errors, [])

    def test_respond_validates_status_and_sets_length(self):
        a, b = socket.socketpair()
        for bad in (99, 600, "200", None):
            with self.assertRaises(ValueError):
                self.bs.respond(a, 1, bad)
        with self.assertRaises(ValueError):
            self.bs.respond(a, 1, 304, body=b"x")
        self.bs.respond(a, 1, 200, body="h\u00e9")      # str body, UTF-8 encoded
        b.settimeout(5)
        status, hdrs, body, _ = read_response(b)
        self.assertEqual((status, hdrs["content-length"], body), (200, "3", "h\u00e9".encode()))
        a.close(); b.close()

    def test_toctou_symlink_swapped_after_realpath_is_refused(self):
        root = os.path.realpath(tempfile.mkdtemp())
        outside = tempfile.mkdtemp()
        os.mkdir(os.path.join(root, "d"))
        write_file(os.path.join(root, "d", "f.txt"), "inside")
        write_file(os.path.join(outside, "f.txt"), "outside")
        real = os.path.realpath(os.path.join(root, "d", "f.txt"))   # checked: inside
        os.rename(os.path.join(root, "d"), os.path.join(root, "old"))
        os.symlink(outside, os.path.join(root, "d"))              # the swap
        self.assertIsNone(self.bs.open_without_symlinks(root, real))
        fd = self.bs.open_without_symlinks(root, os.path.join(root, "old", "f.txt"))
        self.assertIsNotNone(fd)
        os.close(fd)


class ClientTests(unittest.TestCase):
    def fake(self, script):
        fs = FakeServer(script)
        self.addCleanup(fs.close)
        return fs

    def test_exit_0_on_200_body_on_stdout(self):
        def script(c):
            read_preface(c)
            stream, method, path = read_request(c)
            send_response(c, stream)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/index.html" % fs.port])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, INDEX)

    def test_default_path_is_root(self):
        seen = []
        def script(c):
            read_preface(c)
            stream, method, path = read_request(c)
            seen.append(path)
            send_response(c, stream)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d" % fs.port])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(seen, ["/"])

    def test_exit_4_on_404_and_5_on_500(self):
        for status, code in ((404, 4), (500, 5)):
            def script(c, status=status):
                read_preface(c)
                stream, method, path = read_request(c)
                send_response(c, stream, status=status, body=b"")
                read_goaway(c)
            fs = self.fake(script)
            r = run([BCURL, "127.0.0.1:%d/x" % fs.port])
            self.assertEqual(r.returncode, code, r.stderr)

    def test_exit_1_on_connection_refused_and_bad_name(self):
        from support import free_port
        self.assertEqual(run([BCURL, "127.0.0.1:%d/" % free_port()]).returncode, 1)
        r = run([BCURL, "no-such-host.invalid:9000/"], {"BCURL_TIMEOUT": 5})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(b"Traceback", r.stderr)

    def test_exit_2_on_bad_usage_and_numbers(self):
        for argv in ([], ["-x", "h:1/"], [":9000/"], ["h:abc/"], ["h:0/"], ["h:70000/"],
                     ["h:+80/"], ["h:9000/a", "b"], ["[::1/"]):
            r = run([BCURL] + argv)
            self.assertEqual(r.returncode, 2, argv)
            self.assertNotIn(b"Traceback", r.stderr)
        for val in ("nan", "-1", "0", "inf", "soon"):
            self.assertEqual(run([BCURL, "h:1/"], {"BCURL_TIMEOUT": val}).returncode, 2, val)

    def test_overlong_path_exit_2_before_connecting(self):
        fs = self.fake(lambda c: None)
        r = run([BCURL, "127.0.0.1:%d/%s" % (fs.port, "a" * 16400)])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(fs.accepts, 0)

    def test_one_connection_for_many_urls(self):
        def script(c):
            read_preface(c)
            for _ in range(3):
                stream, method, path = read_request(c)
                send_response(c, stream, body=b"body-%d\n" % stream)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/a" % fs.port, "/b", "/c"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fs.accepts, 1)
        self.assertEqual(r.stdout, b"body-1\nbody-2\nbody-3\n")

    def test_response_timeout_exit_1_and_goaway0(self):
        got = []
        def script(c):
            read_preface(c)
            read_request(c)
            got.append(read_all(c, 10))         # say nothing; record what bcurl sends
        fs = self.fake(script)
        t0 = time.monotonic()
        r = run([BCURL, "127.0.0.1:%d/" % fs.port], {"BCURL_TIMEOUT": 1})
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(r.returncode, 1)
        fs.close()
        self.assertEqual(goaway_from(got[0]), ERR_NONE)

    def protocol_error(self, script, args=(), code=ERR_PROTOCOL, env=None):
        """Run bcurl; assert exit 1, no traceback, and a GOAWAY(code) reply."""
        got = []
        def wrapped(c):
            script(c)
            got.append(read_all(c, 10))
        fs = self.fake(wrapped)
        r = run([BCURL] + list(args) + ["127.0.0.1:%d/index.html" % fs.port], env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        fs.close()
        self.assertEqual(goaway_from(got[0]), code, r.stderr)
        return r

    def answer(self, frames_after):
        def script(c):
            read_preface(c)
            stream, method, path = read_request(c)
            c.sendall(frames_after(stream))
        return script

    def test_wrong_stream_response_is_protocol_error(self):
        r = self.protocol_error(self.answer(lambda s: response_bytes(s + 1)))
        self.assertIn(b"stream", r.stderr)

    def test_stray_frame_between_responses_is_protocol_error(self):
        def script(c):
            read_preface(c)
            s1 = read_request(c)[0]
            c.sendall(response_bytes(s1) + build_frame(DATA, FLAG_END_STREAM, s1, b"late"))
            read_request(c)
        got = []
        fs = self.fake(lambda c: (script(c), got.append(read_all(c, 10))))
        r = run([BCURL, "127.0.0.1:%d/a" % fs.port, "/b"])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, INDEX)
        fs.close()
        self.assertEqual(goaway_from(got[0]), ERR_PROTOCOL)

    def test_stray_frame_after_final_response_is_not_written(self):
        # SPEC section 9: after its last response a client MAY send GOAWAY(0)
        # without reading further. The stray bytes must never reach stdout.
        got = []
        def script(c):
            read_preface(c)
            stream = read_request(c)[0]
            c.sendall(response_bytes(stream) + hand_frame(DATA, FLAG_END_STREAM, stream, b"stray"))
            got.append(read_all(c, 10))
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/index.html" % fs.port])
        self.assertEqual((r.returncode, r.stdout), (0, INDEX), r.stderr)
        fs.close()
        self.assertEqual(goaway_from(got[0]), ERR_NONE)

    def test_data_before_headers_is_protocol_error(self):
        self.protocol_error(self.answer(lambda s: build_frame(DATA, 0, s, b"early")))

    def test_second_headers_is_protocol_error(self):
        hdr = hand_headers(200, [(5, b"1")])            # built by hand, not by the codec
        self.protocol_error(self.answer(lambda s: hand_frame(HEADERS, 0, s, hdr) * 2))

    def test_status_out_of_range_is_protocol_error(self):
        bad = hand_headers(99, [(5, b"0")])
        self.protocol_error(self.answer(lambda s: hand_frame(HEADERS, FLAG_END_STREAM, s, bad)))

    def test_malformed_response_payload_is_protocol_error(self):
        bad = struct.pack(">H", 200) + bytes([1, 0x0B, 0, 0])
        self.protocol_error(self.answer(lambda s: hand_frame(HEADERS, FLAG_END_STREAM, s, bad)))

    def test_bad_content_length_values_are_protocol_errors(self):
        for cl in ("\u0661\u0662", "\u00b2", "+12", "", "12 ", "0x0c", "-1", "1e3"):
            hdr = struct.pack(">H", 200) + encode_headers_by_hand([("content-length", cl)])
            self.protocol_error(self.answer(
                lambda s, hdr=hdr: build_frame(HEADERS, 0, s, hdr) + build_frame(DATA, FLAG_END_STREAM, s, INDEX)))

    def test_content_length_18_digits_ok_19_rejected(self):
        ok = hand_headers(200, [(5, b"0" * 16 + b"12")])        # 18 digits, leading zeros
        def script(c):
            read_preface(c)
            stream = read_request(c)[0]
            c.sendall(hand_frame(HEADERS, 0, stream, ok) + hand_frame(DATA, FLAG_END_STREAM, stream, INDEX))
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/" % fs.port])
        self.assertEqual((r.returncode, r.stdout), (0, INDEX), r.stderr)
        bad = hand_headers(200, [(5, b"0" * 17 + b"12")])       # 19 digits
        self.protocol_error(self.answer(
            lambda s: hand_frame(HEADERS, 0, s, bad) + hand_frame(DATA, FLAG_END_STREAM, s, INDEX)))

    def test_missing_content_length_is_protocol_error(self):
        hdr = hand_headers(200, [(6, b"x")])            # server only
        self.protocol_error(self.answer(lambda s: hand_frame(HEADERS, FLAG_END_STREAM, s, hdr)))

    def test_short_body_is_protocol_error(self):
        r = self.protocol_error(self.answer(lambda s: response_bytes(s, body=b"short", cl=12)))
        self.assertIn(b"content-length", r.stderr)

    def test_over_length_stops_before_writing_excess(self):
        r = self.protocol_error(self.answer(lambda s: response_bytes(s, body=b"x" * 20, cl=12)))
        self.assertEqual(r.stdout, b"")
        two = lambda s: (build_frame(HEADERS, 0, s, encode_response(200, [("content-length", "12")])) +
                         build_frame(DATA, 0, s, b"a" * 12) + build_frame(DATA, FLAG_END_STREAM, s, b"b" * 8))
        r = self.protocol_error(self.answer(two))
        self.assertEqual(r.stdout, b"a" * 12)

    def test_data_on_head_is_protocol_error(self):
        # HEADERS (no END_STREAM) + DATA on a HEAD response: caught at the HEADERS.
        r = self.protocol_error(self.answer(lambda s: response_bytes(s)), args=["-I"])
        self.assertEqual(r.stdout, b"")

    def test_head_headers_without_end_stream_fails_fast(self):
        hdr = hand_headers(200, [(5, b"12")])
        t0 = time.monotonic()
        r = self.protocol_error(self.answer(lambda s: hand_frame(HEADERS, 0, s, hdr)),
                                args=["-I"], env={"BCURL_TIMEOUT": 20})
        self.assertLess(time.monotonic() - t0, 10)      # no wait for the 20 s timeout
        self.assertIn(b"END_STREAM", r.stderr)

    def test_head_exempt_from_length_check(self):
        def script(c):
            read_preface(c)
            stream, method, path = read_request(c)
            assert method == METHOD_HEAD
            send_response(c, stream, body=b"", cl=12)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "-I", "127.0.0.1:%d/index.html" % fs.port])
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)

    def test_304_no_body_ok_and_304_without_end_stream_is_error(self):
        hdr = encode_response(304, [("content-length", "12"), ("etag", '"c-1"')])
        def script(c):
            read_preface(c)
            stream = read_request(c)[0]
            c.sendall(build_frame(HEADERS, FLAG_END_STREAM, stream, hdr))
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/" % fs.port])
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        r = self.protocol_error(self.answer(
            lambda s: build_frame(HEADERS, 0, s, hdr) + build_frame(DATA, FLAG_END_STREAM, s, INDEX)))
        self.assertEqual(r.stdout, b"")

    def test_oversize_frame_from_server_goaway2(self):
        big = (MAX_PAYLOAD + 1).to_bytes(3, "big") + bytes((DATA, 0)) + (1).to_bytes(3, "big")
        self.protocol_error(self.answer(lambda s: big + b"x" * 100), code=ERR_FRAME_SIZE)

    def test_server_goaway_ends_client_without_reply(self):
        for code, text in ((ERR_NONE, b"normal"), (9, b"protocol error")):
            got = []
            def script(c, code=code):
                read_preface(c)
                read_request(c)
                c.sendall(build_goaway(code))
                c.shutdown(socket.SHUT_WR)
                got.append(read_all(c, 10))
            fs = self.fake(script)
            r = run([BCURL, "127.0.0.1:%d/" % fs.port])
            self.assertEqual(r.returncode, 1)
            self.assertIn(text, r.stderr)
            fs.close()
            self.assertEqual(got, [b""])            # no GOAWAY sent back

    def test_server_close_mid_response_exit_1(self):
        def script(c):
            read_preface(c)
            stream = read_request(c)[0]
            c.sendall(build_frame(HEADERS, 0, stream, encode_response(200, [("content-length", "12")])))
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/" % fs.port])
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(b"Traceback", r.stderr)

    def test_unknown_types_skipped_on_any_stream(self):
        def script(c):
            read_preface(c)
            stream = read_request(c)[0]
            c.sendall(build_frame(0x60, 0, 0, b"v2") + build_frame(0x61, 1, 77, b"") +
                      build_frame(0x62, 0, stream, b"x" * MAX_PAYLOAD))
            send_response(c, stream)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "127.0.0.1:%d/" % fs.port])
        self.assertEqual((r.returncode, r.stdout), (0, INDEX), r.stderr)

    def test_v_dumps_every_frame_including_error_goaway(self):
        def script(c):
            read_preface(c)
            stream, method, path = read_request(c)
            c.sendall(build_frame(0x60, 0, 0, b"v2 frame"))   # unknown type
            send_response(c, stream)
            read_goaway(c)
        fs = self.fake(script)
        r = run([BCURL, "-v", "127.0.0.1:%d/index.html" % fs.port])
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stderr.splitlines()
        # sent: preface, request, closing GOAWAY. received: unknown, HEADERS, DATA
        self.assertEqual(sum(1 for l in lines if l.strip() == b"->"), 3, r.stderr)
        self.assertEqual(sum(1 for l in lines if l.strip() == b"<-"), 3, r.stderr)
        self.assertIn(b"BHT1", r.stderr)
        r = self.protocol_error(self.answer(lambda s: build_frame(DATA, 0, s, b"early")), args=["-v"])
        self.assertIn(b"-> 0000  00 00 02 02 00 00 00 00 00 01", r.stderr)


class InProcessClientTests(unittest.TestCase):
    """bcurl.read_one_response against a socketpair standing in for the server."""
    @classmethod
    def setUpClass(cls):
        cls.bc = load_script(BCURL, "bcurl_mod")

    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.out = io.BytesIO()
        fake_stdout = mock.Mock()
        fake_stdout.buffer = self.out
        p = mock.patch.object(sys, "stdout", fake_stdout)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def read(self, head=False, timeout=2):
        return self.bc.read_one_response(self.a, 1, head, None, False, timeout)

    def test_unsolicited_data_on_fresh_stream_refused(self):
        self.b.sendall(build_frame(DATA, 0, 1, b"unsolicited"))
        with self.assertRaisesRegex(ProtocolError, "DATA before HEADERS"):
            self.read()
        self.assertEqual(self.out.getvalue(), b"")

    def test_data_on_other_stream_refused(self):
        self.b.sendall(build_frame(DATA, FLAG_END_STREAM, 2, b"x"))
        with self.assertRaisesRegex(ProtocolError, "stream 2"):
            self.read()

    def test_peer_goaway_raised_with_code(self):
        self.b.sendall(build_goaway(ERR_NONE))
        with self.assertRaises(GoawayReceived) as cm:
            self.read()
        self.assertEqual(cm.exception.code, ERR_NONE)

    def test_malformed_goaway_is_protocol_error_not_peer_goaway(self):
        self.b.sendall(build_frame(GOAWAY, 0, 1, b"\x00\x00"))
        with self.assertRaises(ProtocolError) as cm:
            self.read()
        self.assertNotIsInstance(cm.exception, GoawayReceived)
        self.assertEqual(cm.exception.code, ERR_PROTOCOL)

    def test_read_timeout(self):
        t0 = time.monotonic()
        with self.assertRaises(ProtocolError) as cm:
            self.read(timeout=0.3)
        self.assertEqual(cm.exception.code, ERR_NONE)
        self.assertLess(time.monotonic() - t0, 2)

    def test_frame_deadline_does_not_leak_into_next_wait(self):
        # A trickled first frame used to leave a tiny socket timeout behind,
        # so the next frame's first-byte wait could fail at random.
        hdr = build_frame(HEADERS, 0, 1, encode_response(200, [("content-length", "2")]))
        def server():
            for i in range(len(hdr)):
                self.b.sendall(hdr[i:i + 1])
                time.sleep(0.01)
            time.sleep(1.2)                     # longer than any leftover deadline
            self.b.sendall(build_frame(DATA, FLAG_END_STREAM, 1, b"ok"))
        t = threading.Thread(target=server)
        t.start()
        with mock.patch.object(bhttp, "FRAME_TIMEOUT", 1.0):
            self.assertEqual(self.read(timeout=3), 200)
        t.join()
        self.assertEqual(self.out.getvalue(), b"ok")

    def test_parse_target(self):
        pt = self.bc.split_target
        self.assertEqual(pt("localhost:9000/index.html"), ("localhost", 9000, "localhost:9000", "/index.html"))
        self.assertEqual(pt("localhost"), ("localhost", 9000, "localhost", "/"))
        self.assertEqual(pt("[::1]:8080/a"), ("::1", 8080, "[::1]:8080", "/a"))
        self.assertEqual(pt("[::1]/"), ("::1", 9000, "[::1]", "/"))
        for bad in ("[::1", "[::1]x/", ":1/", "h:0/", "h:65536/", "h:\u0661/"):
            with self.assertRaises(ValueError):
                pt(bad)


class CodecTests(unittest.TestCase):
    def test_entries_roundtrip_static_and_literal(self):
        pairs = [("host", "example.com"), ("x-custom", "v1"), ("content-length", "12")]
        buf = encode_headers(pairs)
        self.assertEqual(decode_headers(buf, 0), (pairs, len(buf)))
        self.assertEqual(buf[:2], b"\x03\x01")
        lit = 1 + 1 + 2 + len("example.com")
        self.assertEqual(buf[lit:lit + 2], b"\x00\x08")

    def test_static_table_index_bounds(self):
        self.assertEqual(len(STATIC_TABLE), 10)
        self.assertEqual(STATIC_TABLE[7], "etag")
        for idx in range(1, 11):
            buf = bytes([1, idx, 0, 0])
            self.assertEqual(decode_headers(buf, 0)[0], [(STATIC_TABLE[idx - 1], "")])
        for idx in (11, 0x80, 0xFF):
            with self.assertRaises(Malformed):
                decode_headers(bytes([1, idx, 0, 0]), 0)

    def test_every_truncation_is_underrun(self):
        payload = encode_request(METHOD_GET, "/index.html", [("host", "x"), ("x-a", "yz")])
        for n in range(len(payload)):
            with self.assertRaises(Malformed, msg=n):
                decode_request(payload[:n])
        resp = encode_response(200, [("content-length", "12"), ("x-b", "q")])
        for n in range(len(resp)):
            with self.assertRaises(Malformed, msg=n):
                decode_response(resp[:n])

    def test_overrun_is_malformed(self):
        with self.assertRaisesRegex(Malformed, "trailing"):
            decode_request(encode_request(METHOD_GET, "/", []) + b"\x00")
        with self.assertRaisesRegex(Malformed, "trailing"):
            decode_response(encode_response(200, []) + b"\x00")

    def test_names_must_be_lowercase_ascii(self):
        for name in ("Host", "x_y", "\u00e4", "x y", ""):
            with self.assertRaises(Malformed):
                encode_headers([(name, "v")])

    def test_content_length_parser(self):
        self.assertEqual(parse_content_length("0"), 0)
        self.assertEqual(parse_content_length("40000"), 40000)
        self.assertEqual(parse_content_length("0" * 16 + "12"), 12)     # 18 digits
        for bad in ("", "\u0661\u0662", "\u00b2", "+1", "-1", " 1", "1 ", "1.0", "9" * 19,
                    "0" * 17 + "12"):
            with self.assertRaises(Malformed, msg=bad):
                parse_content_length(bad)

    def test_frame_header_layout_and_bounds(self):
        f = build_frame(DATA, FLAG_END_STREAM, 0x010203, b"ab")
        self.assertEqual(f, bytes.fromhex("0000020101010203") + b"ab")
        self.assertEqual(build_goaway(ERR_NONE), bytes.fromhex("00000202000000000000"))
        with self.assertRaises(ValueError):
            build_frame(DATA, 0, 1 << 24)
        with self.assertRaises(ValueError):
            build_frame(DATA, 0, 1, b"x" * (MAX_PAYLOAD + 1))
        with self.assertRaises(ValueError):
            encode_response(600, [])

    def test_peer_goaway_unknown_code_reads_as_protocol_error(self):
        self.assertEqual(GoawayReceived(0).code, 0)
        self.assertEqual(GoawayReceived(77).code, ERR_PROTOCOL)
        self.assertEqual(bhttp.parse_goaway(0, b"\x00\x09"), ERR_PROTOCOL)
        with self.assertRaises(ProtocolError):
            bhttp.parse_goaway(0, b"\x00")

    def test_env_validation(self):
        with mock.patch.dict(os.environ, {"X_T": "2.5"}):
            self.assertEqual(bhttp.seconds_from_env("X_T", 1), 2.5)
        for bad in ("nan", "inf", "0", "-3", "x"):
            with mock.patch.dict(os.environ, {"X_T": bad}):
                with self.assertRaises(ValueError):
                    bhttp.seconds_from_env("X_T", 1)
        with mock.patch.dict(os.environ, {"X_N": "\u0661"}):
            with self.assertRaises(ValueError):
                bhttp.int_from_env("X_N", 1, 1, 9)

    def test_goaway_close_sends_fin_not_rst_with_unread_input(self):
        for _ in range(10):
            a, b = socket.socketpair()
            b.sendall(b"u" * 50000)             # unread by a
            t = threading.Thread(target=bhttp.send_goaway_and_close, args=(a, ERR_PROTOCOL))
            t.start()
            self.assertEqual(goaway_from(read_all(b)), ERR_PROTOCOL)
            b.close()
            t.join(3)
            self.assertFalse(t.is_alive())


# ---- fake-server side helpers ----
# read_preface / read_request / read_goaway / response_bytes use the codec
# deliberately: they script the peer of the program under test. hand_frame
# and hand_headers build bytes straight from SPEC sections 3, 6 and 7, so the
# client tests that use them do not share a blind spot with the codec.

def read_preface(c):
    pre = b""
    while len(pre) < 4:
        chunk = c.recv(4 - len(pre))
        if not chunk:
            raise EOFError("no preface")
        pre += chunk
    assert pre == PREFACE, pre


def read_request(c):
    ftype, flags, stream, payload = recv_frame(c, idle=10)
    assert ftype == HEADERS, ftype
    method, path, pairs = decode_request(payload)
    return stream, method, path


def read_goaway(c):
    ftype, flags, stream, payload = recv_frame(c, idle=10)
    assert (ftype, stream, payload) == (GOAWAY, 0, b"\x00\x00"), (ftype, payload)


def hand_frame(ftype, flags, stream, payload):
    """A frame built by hand from SPEC section 3: 24/8/8/24, big-endian."""
    return len(payload).to_bytes(3, "big") + bytes([ftype, flags]) + \
        stream.to_bytes(3, "big") + payload


def hand_headers(status, entries):
    """A response HEADERS payload by hand (SPEC sections 6 and 7):
    entries are (static index, value bytes)."""
    out = struct.pack(">H", status) + bytes([len(entries)])
    for idx, value in entries:
        out += bytes([idx]) + struct.pack(">H", len(value)) + value
    return out


def encode_headers_by_hand(pairs):
    """Like encode_headers but without validation, to send bad values."""
    out = bytearray([len(pairs)])
    for name, value in pairs:
        v = value.encode("utf-8")
        out += bytes([bhttp.STATIC_INDEX[name]]) + struct.pack(">H", len(v)) + v
    return bytes(out)


def response_bytes(stream, status=200, body=INDEX, cl=None):
    pairs = [("server", "fake/1"), ("date", "Thu, 24 Sep 2026 10:54:34 GMT"),
             ("content-length", str(len(body) if cl is None else cl))]
    if status == 200:
        pairs.append(("content-type", "text/html"))
    hdr = encode_response(status, pairs)
    if not body:
        return build_frame(HEADERS, FLAG_END_STREAM, stream, hdr)
    return build_frame(HEADERS, 0, stream, hdr) + build_frame(DATA, FLAG_END_STREAM, stream, body)


def send_response(c, stream, **kw):
    c.sendall(response_bytes(stream, **kw))


if __name__ == "__main__":
    unittest.main()
