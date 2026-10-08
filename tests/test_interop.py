"""Interop tests: the spec-only programs against the real ones.

- interop/cleanroom_client.py against the real bserve (directly and through
  the fault-injecting proxy interop/inject.py), next to bcurl.
- interop/cleanroom_server.py against the real bcurl: bcurl's only peer that
  is not bserve.
Both spec-only programs run under `python3 -I`, so they cannot import
bhttp.py even by accident, and an AST check pins their imports. This file
does not import the codec either.
"""
import ast, hashlib, os, struct, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import (BCURL, CLEANROOM, INJECT, INTEROP, FakeServer, goaway_from,
                     read_all, spawn_listening, start_server, stop, run)

CLEANSERVER = os.path.join(INTEROP, "cleanroom_server.py")
BLOB = (bytes(range(256)) * 157)[:40000]
INDEX = b"<h1>hi</h1>\n"
CLEAN = [sys.executable, "-I", CLEANROOM]


def make_root():
    w = tempfile.mkdtemp()
    with open(os.path.join(w, "index.html"), "wb") as f:
        f.write(INDEX)
    with open(os.path.join(w, "blob.bin"), "wb") as f:
        f.write(BLOB)
    os.mkdir(os.path.join(w, "sub"))
    with open(os.path.join(w, "sub", "index.html"), "wb") as f:
        f.write(b"sub index\n")
    return w


def proxy(test, target, *opts):
    """Start inject.py in front of `target`; stopped when the test ends."""
    proc, port = spawn_listening([sys.executable, INJECT, "0", str(target)] + list(opts))
    test.addCleanup(stop, proc)
    return port


def recv_exactly(c, n):
    out = b""
    while len(out) < n:
        piece = c.recv(n - len(out))
        if not piece:
            raise EOFError("closed")
        out += piece
    return out


class CleanRoomSourceTests(unittest.TestCase):
    def test_imports_nothing_from_this_project(self):
        allowed = ((CLEANROOM, {"os", "socket", "struct", "sys"}),
                   (CLEANSERVER, {"os", "socket", "struct", "sys", "time"}))
        for path, want in allowed:
            with open(path) as f:
                tree = ast.parse(f.read())
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    mods.add((node.module or "").split(".")[0])
            self.assertEqual(mods, want, path)


class InteropTests(unittest.TestCase):
    """The real bserve against both clients."""
    @classmethod
    def setUpClass(cls):
        cls.proc, cls.port = start_server(make_root())

    @classmethod
    def tearDownClass(cls):
        stop(cls.proc)

    def test_cleanroom_get_head_404_multi(self):
        base = "127.0.0.1:%d" % self.port
        r = run(CLEAN + [base + "/blob.bin"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hashlib.sha256(r.stdout).hexdigest(), hashlib.sha256(BLOB).hexdigest())
        r = run(CLEAN + ["-I", base + "/blob.bin"])
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        self.assertEqual(run(CLEAN + [base + "/missing"]).returncode, 4)
        r = run(CLEAN + [base + "/index.html", "/missing", "/blob.bin"])
        self.assertEqual((r.returncode, r.stdout), (4, INDEX + BLOB), r.stderr)

    def test_unknown_frames_injected_both_ways_are_skipped(self):
        for client in (CLEAN, [BCURL]):
            port = proxy(self, self.port, "--inject-unknown")
            r = run(client + ["127.0.0.1:%d/blob.bin" % port, "/index.html"])
            self.assertEqual(r.returncode, 0, (client, r.stderr))
            self.assertEqual(r.stdout, BLOB + INDEX)

    def test_flipped_method_byte_gets_400(self):
        for client in (CLEAN, [BCURL]):
            port = proxy(self, self.port, "--flip-byte", "12")     # method 0x01 -> 0xfe
            self.assertEqual(run(client + ["127.0.0.1:%d/index.html" % port]).returncode, 4)

    def test_flipped_preface_is_refused(self):
        port = proxy(self, self.port, "--flip-byte", "0")
        r = run([BCURL, "127.0.0.1:%d/index.html" % port])
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"GOAWAY(3)", r.stderr)

    def test_flipped_response_length_is_caught_by_client(self):
        for client in (CLEAN, [BCURL]):
            port = proxy(self, self.port, "--direction", "s2c", "--flip-byte", "0")   # Length 0xff0079
            r = run(client + ["127.0.0.1:%d/index.html" % port])
            self.assertEqual(r.returncode, 1, client)
            self.assertEqual(r.stdout, b"")

    def test_truncated_response_is_caught_by_client(self):
        for client in (CLEAN, [BCURL]):
            port = proxy(self, self.port, "--direction", "s2c", "--truncate", "140")  # 140 = HEADERS len + 12: inside DATA for bserve; the DATA header for cleanroom_server
            r = run(client + ["127.0.0.1:%d/index.html" % port])
            self.assertEqual(r.returncode, 1, client)

    def test_slow_link_still_works(self):
        port = proxy(self, self.port, "--delay", "100")
        r = run(CLEAN + ["127.0.0.1:%d/blob.bin" % port])
        self.assertEqual((r.returncode, r.stdout), (0, BLOB), r.stderr)


class CleanClientTests(unittest.TestCase):
    """The spec-only client's own error handling (SPEC sections 4, 9, 10)."""
    def test_bad_timeout_exit_2_without_traceback(self):
        for val in ("soon", "nan", "-1", "0", "inf"):
            r = run(CLEAN + ["127.0.0.1:1/"], {"BCURL_TIMEOUT": val})
            self.assertEqual(r.returncode, 2, val)
            self.assertNotIn(b"Traceback", r.stderr)

    def test_malformed_goaway_gets_goaway1(self):
        got = []
        def script(c):
            recv_exactly(c, 4)                              # preface
            head = recv_exactly(c, 8)
            recv_exactly(c, int.from_bytes(head[:3], "big"))
            # GOAWAY on stream 1: malformed (SPEC section 4), so a protocol error
            c.sendall(bytes.fromhex("0000020200000001") + struct.pack(">H", 0))
            got.append(read_all(c, 10))
        fs = FakeServer(script)
        self.addCleanup(fs.close)
        r = run(CLEAN + ["127.0.0.1:%d/" % fs.port])
        self.assertEqual(r.returncode, 1, r.stderr)
        fs.close()
        self.assertEqual(goaway_from(got[0]), 1)


class CleanServerTests(unittest.TestCase):
    """bcurl against the spec-only server: bcurl's only peer that is not bserve."""
    @classmethod
    def setUpClass(cls):
        cls.proc, cls.port = spawn_listening([sys.executable, "-I", CLEANSERVER, make_root(), "0"])

    @classmethod
    def tearDownClass(cls):
        stop(cls.proc)

    def test_bcurl_get_head_404_multi(self):
        base = "127.0.0.1:%d" % self.port
        r = run([BCURL, base + "/blob.bin"])
        self.assertEqual((r.returncode, r.stdout), (0, BLOB), r.stderr)
        r = run([BCURL, "-I", base + "/blob.bin"])
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        self.assertEqual(run([BCURL, base + "/missing"]).returncode, 4)
        r = run([BCURL, base + "/index.html", "/missing", "/sub/", "/blob.bin"])
        self.assertEqual((r.returncode, r.stdout), (4, INDEX + b"sub index\n" + BLOB), r.stderr)

    def test_bcurl_skips_unknown_frames_from_the_spec_only_server(self):
        port = proxy(self, self.port, "--inject-unknown")
        r = run([BCURL, "127.0.0.1:%d/blob.bin" % port, "/index.html"])
        self.assertEqual((r.returncode, r.stdout), (0, BLOB + INDEX), r.stderr)

    def test_spec_only_server_400_and_goaway3(self):
        port = proxy(self, self.port, "--flip-byte", "12")
        r = run([BCURL, "127.0.0.1:%d/index.html" % port])
        self.assertEqual((r.returncode, r.stdout), (4, b""), r.stderr)
        port = proxy(self, self.port, "--flip-byte", "0")
        r = run([BCURL, "127.0.0.1:%d/index.html" % port])
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"GOAWAY(3)", r.stderr)


if __name__ == "__main__":
    unittest.main()
