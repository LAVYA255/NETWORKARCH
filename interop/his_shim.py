#!/usr/bin/env python3
"""Harness shim for the independent client (his_client.py, UNMODIFIED).

Adapts it to interop/run.sh's client contract: [-flags] 127.0.0.1:PORT/PATH
[PATH...]. The client's own log goes to stderr; on success the shim prints
the received body bytes (request order) to stdout so run.sh can compare.
HEAD (-I) is unsupported: the independent client is GET-only, so run.sh
skips that row and prints N/A for it."""
import contextlib, importlib.util, io, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    args = [a for a in argv if a != "-v"]
    target, more = args[0], args[1:]
    hostport, _, first = target.partition("/")
    host, _, port = hostport.partition(":")
    paths = ["/" + first] + more
    spec = importlib.util.spec_from_file_location(
        "his_client", os.path.join(HERE, "his_client.py"))
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    hc.HOST, hc.PORT = host, int(port)
    log = io.StringIO()
    os.chdir(tempfile.mkdtemp(prefix="his_client_"))
    with contextlib.redirect_stdout(log):
        rc = hc.main(paths)
    sys.stderr.write(log.getvalue())
    if rc == 0:
        out = sys.stdout.buffer
        for p in paths:
            name = p.strip("/").replace("/", "_") or "index.html"
            with open(os.path.join("received", name), "rb") as fh:
                out.write(fh.read())
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
