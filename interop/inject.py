#!/usr/bin/env python3
"""inject.py LISTEN_PORT TARGET_PORT [options]: a fault-injecting TCP proxy for BHTTP/1.

Listens on 127.0.0.1:LISTEN_PORT and forwards each connection to
127.0.0.1:TARGET_PORT, optionally damaging the byte stream so you can watch
the peer catch it. Python 3 stdlib only; it does not import bhttp.py.

  --flip-byte N        XOR byte N (0-based) of the chosen direction with 0xFF
  --truncate N         forward only the first N bytes of the chosen direction,
                       then half-close that direction
  --delay MS           sleep MS milliseconds before forwarding each chunk
  --direction D        c2s (client to server, the default) or s2c
  --inject-unknown     before every forwarded frame, in both directions, insert
                       a frame of unknown type 0x7F with a random payload
                       (stream ID random). A conforming peer MUST skip it.
  --record PREFIX      write PREFIX.c2s.bin and PREFIX.s2c.bin: the bytes as
                       forwarded (after any damage), one pair per connection
                       (PREFIX.N.* for connection N > 1)
  --once               exit after the first connection ends

On startup prints "inject: listening on port N" (LISTEN_PORT 0 picks one).
Connection count is printed to stderr on each accept.
"""
import argparse, os, random, socket, sys, threading, time

UNKNOWN_TYPE = 0x7F


def unknown_frame(rng):
    payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 33)))
    sid = rng.randrange(0, 1 << 24)
    return len(payload).to_bytes(3, "big") + bytes([UNKNOWN_TYPE, 0]) + \
        sid.to_bytes(3, "big") + payload


class Pipe(object):
    """One direction of one connection."""
    def __init__(self, src, dst, name, opts, record):
        self.src, self.dst, self.name, self.opts = src, dst, name, opts
        self.damaged = opts.direction == name
        self.inject_on = bool(opts.inject_unknown)  # per-Pipe: one oversize frame must not disarm the other direction
        self.record = record
        self.seen = 0                       # bytes read from src
        self.sent = 0                       # bytes forwarded, for --truncate
        self.buf = b""                      # frame reassembly for --inject-unknown
        self.preface_left = 4 if name == "c2s" else 0
        self.rng = random.Random(0xB477 + (name == "s2c"))

    def damage(self, chunk):
        if not self.damaged or self.opts.flip_byte is None:
            return chunk
        n = self.opts.flip_byte - (self.seen - len(chunk))
        if 0 <= n < len(chunk):
            chunk = chunk[:n] + bytes([chunk[n] ^ 0xFF]) + chunk[n + 1:]
        return chunk

    def frames(self, chunk):
        """With --inject-unknown, yield whole frames each preceded by an
        unknown one; otherwise yield the chunk unchanged."""
        if not self.inject_on:
            yield chunk
            return
        self.buf += chunk
        if self.preface_left:
            take = min(self.preface_left, len(self.buf))
            yield self.buf[:take]
            self.buf, self.preface_left = self.buf[take:], self.preface_left - take
            if self.preface_left:
                return
        while len(self.buf) >= 8:
            total = 8 + int.from_bytes(self.buf[:3], "big")
            if total > 8 + 16384:           # oversize: pass raw, stop framing
                yield self.buf
                self.buf = b""
                self.inject_on = False
                return
            if len(self.buf) < total:
                return
            yield unknown_frame(self.rng) + self.buf[:total]
            self.buf = self.buf[total:]

    def send(self, data):
        if self.damaged and self.opts.truncate is not None:
            room = self.opts.truncate - self.sent
            if room <= 0:
                return False
            data = data[:room]
        self.dst.sendall(data)
        self.sent += len(data)
        if self.record:
            self.record.write(data)
            self.record.flush()
        return not (self.damaged and self.opts.truncate is not None
                    and self.sent >= self.opts.truncate)

    def run(self):
        try:
            while True:
                chunk = self.src.recv(65536)
                if not chunk:
                    break
                self.seen += len(chunk)
                chunk = self.damage(chunk)
                if self.opts.delay:
                    time.sleep(self.opts.delay / 1000.0)
                keep = True
                for piece in self.frames(chunk):
                    if not self.send(piece):
                        keep = False
                        break
                if not keep:
                    break
        except OSError:
            pass
        finally:
            try:
                self.dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def serve_one(client, opts, n):
    upstream = socket.create_connection(("127.0.0.1", opts.target))
    recs = []
    if opts.record:
        stem = opts.record if n == 1 else "%s.%d" % (opts.record, n)
        recs = [open(stem + ".c2s.bin", "wb"), open(stem + ".s2c.bin", "wb")]
    pipes = [Pipe(client, upstream, "c2s", opts, recs[0] if recs else None),
             Pipe(upstream, client, "s2c", opts, recs[1] if recs else None)]
    threads = [threading.Thread(target=p.run, daemon=True) for p in pipes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in (client, upstream):
        s.close()
    for r in recs:
        r.close()


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("listen", type=int)
    ap.add_argument("target", type=int)
    ap.add_argument("--flip-byte", type=int)
    ap.add_argument("--truncate", type=int)
    ap.add_argument("--delay", type=int, default=0)
    ap.add_argument("--direction", choices=("c2s", "s2c"), default="c2s")
    ap.add_argument("--inject-unknown", action="store_true")
    ap.add_argument("--record")
    ap.add_argument("--once", action="store_true")
    opts = ap.parse_args(argv[1:])
    for name in ("flip_byte", "truncate", "delay"):
        val = getattr(opts, name)
        if val is not None and val < 0:
            ap.error("--%s must be >= 0" % name.replace("_", "-"))
    srv = socket.create_server(("127.0.0.1", opts.listen))
    sys.stdout.write("inject: listening on port %d\n" % srv.getsockname()[1])
    sys.stdout.flush()
    n = 0
    while True:
        c, _ = srv.accept()
        n += 1
        sys.stderr.write("inject: connection %d\n" % n)
        sys.stderr.flush()
        if opts.once:
            serve_one(c, opts, n)
            return 0
        threading.Thread(target=serve_one, args=(c, opts, n), daemon=True).start()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
