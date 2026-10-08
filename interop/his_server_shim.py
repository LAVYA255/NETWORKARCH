#!/usr/bin/env python3
"""Harness shim for the independent server (his_server.py, UNMODIFIED).

Adapts it to interop/run.sh's server contract: ROOT PORT. his_server serves
./www relative to its working directory, so the shim runs it from a scratch
directory whose www symlink is the matrix root."""
import importlib.util, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    root, port = os.path.abspath(argv[0]), argv[1]
    work = tempfile.mkdtemp(prefix="his_server_")
    os.symlink(root, os.path.join(work, "www"))
    os.chdir(work)                      # ROOT is fixed at import time: chdir first
    spec = importlib.util.spec_from_file_location(
        "his_server", os.path.join(HERE, "his_server.py"))
    hs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hs)
    sys.argv = ["his_server.py", port]
    hs.main()


if __name__ == "__main__":
    main(sys.argv[1:])
