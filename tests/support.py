"""Shared test helpers: start real processes on free ports, fake servers,
socket readers. Nothing here builds or parses BHTTP with shortcuts that the
code under test also uses, except where a test says so."""
import importlib.machinery, importlib.util, os, socket, struct, subprocess, sys
import threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
BSERVE = os.path.join(ROOT, "bserve")
BCURL = os.path.join(ROOT, "bcurl")
INTEROP = os.path.join(ROOT, "interop")
CLEANROOM = os.path.join(INTEROP, "cleanroom_client.py")
INJECT = os.path.join(INTEROP, "inject.py")


def load_script(path, name):
    """Import an extension-less script (bserve, bcurl) as a module."""
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def spawn_listening(argv, env=None, word="port"):
    """Start a process that prints '... listening on port N' first, and
    return (proc, N). No probe connection, so no connection slot is used."""
    e = dict(os.environ)
    e.update({k: str(v) for k, v in (env or {}).items()})
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=e)
    line = p.stdout.readline().decode()
    if word not in line:
        p.kill()
        raise RuntimeError("no listening line from %r: %r" % (argv, line))
    return p, int(line.split()[-1])


def start_server(www, **env):
    return spawn_listening([BSERVE, www, "0"], env)


def stop(proc):
    proc.kill()
    proc.wait()
    if proc.stdout:
        proc.stdout.close()


def connect(port, preface=True, host="127.0.0.1"):
    c = socket.create_connection((host, port), timeout=5)
    if preface:
        c.sendall(b"BHT1")
    return c


def read_all(c, timeout=5):
    """Read until EOF. Raises ConnectionResetError if the peer sent RST."""
    c.settimeout(timeout)
    out = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            return out
        out += chunk


def run(argv, env=None, timeout=30):
    e = dict(os.environ)
    e.update({k: str(v) for k, v in (env or {}).items()})
    return subprocess.run(argv, capture_output=True, timeout=timeout, env=e)


class FakeServer(object):
    """Run script(conn) per connection on a throwaway server; counts accepts."""
    def __init__(self, script):
        self.script = script
        self.errors = []
        self.accepts = 0
        self.done = False
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self.done:
            try:
                c, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepts += 1
            c.settimeout(10)
            try:
                self.script(c)
            except Exception as e:
                self.errors.append(e)
            finally:
                c.close()

    def close(self):
        time.sleep(0.05)
        self.done = True
        self.thread.join(2)
        self.sock.close()


def goaway_from(raw):
    """Parse raw bytes that should be exactly one GOAWAY frame; return its code."""
    assert len(raw) == 10, raw
    assert raw[:8] == bytes.fromhex("0000020200000000"), raw
    return struct.unpack(">H", raw[8:])[0]
