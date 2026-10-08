# Independent-server build session (trimmed transcript)

This is an excerpt of the Claude Code session (Opus 5.5, effort High) in which
`interop/his_server.py` was built. Trimmed for submission: local file paths
elided as [...], the code diffs and end-of-session export troubleshooting cut,
and an unrelated mis-attachment reduced to its relevant exchange. Everything
kept is verbatim. Session recorded 2026-10-08, ~9:40-9:45 PM IST.

---

Claude Code v2.1.293 - Opus 5.5, Claude Pro - ~/bht1-server

> /model
  Set model to Opus 5.5 and saved as your default for new sessions

> /effort
  Set effort level to high

> I'm attaching SPEC.md, a wire protocol spec for a binary HTTP-like protocol
  (BHT1). Build me a minimal SERVER for it in Python 3, standard library only
  (socket, struct, sys, os - no external packages).

  Rules:
  - Work ONLY from the attached spec. Do not ask me for any existing server or
  client code.
  - Listen on localhost:9000, accept TCP connections, read the connection
  preface exactly as the spec says and validate it.
  - Parse 8-byte frame headers, read Length payload bytes. Handle one HEADERS
  frame = one request: extract method + path, serve files from a ./www
  directory.
  - Respond per spec: HEADERS frame with the status, then the body in DATA
  frames split at the spec's max frame size, END_STREAM on the last frame.
  - Multiple sequential requests on one connection, streams strictly increasing
  - honor that.
  - Unknown frame types: skip, not fatal. Malformed frames: respond per the
  spec's error rules (400 close / GOAWAY as specified). 404 for missing files.
  - Handle GOAWAY and close cleanly.
  - Keep it small and readable, ~150-200 lines. Print each frame it
  receives/sends so the wire flow is visible.

  Also give me the exact commands to: (1) set up www/ with index.html and a
  40KB blob.bin, (2) start your server, (3) run my existing client.py against
  it for / and /blob.bin, (4) verify the blob arrives byte-identical
  (sha256sum).
  You must NOT read, open, or compare against any existing server or client
  code, including bserve or bhttp.py. Spec only. If you have seen such code in
  this conversation, say so.
  [...]

---

No server or client code has come up in this conversation: no bserve, no
bhttp.py, no client.py. Next I'll read the file you attached.

---

The attached file is actually a demo script for the procurement-copilot video,
not SPEC.md, so it doesn't cover the protocol. I'll search locally for a BHT1
SPEC.md, limiting the search to .md spec files without opening any code.

  Interrupted - What should Claude do instead?

> [...]/SPEC.txt

---

I've read the spec, but haven't yet looked at the server/client code,
HEXDUMP.md, bserve, or bcurl - my understanding so far is based solely on
SPEC.txt.

---

Write(server.py) - Wrote 277 lines to server.py

---

The server compiles. Next I'll set up www/ and test it on port 9100 (so 9000
stays free for you) using my own small probe. I wrote it from the spec too,
not from your client.

---

All 38 spec checks pass.

---

I've built the server: ~/bht1-server/server.py. I wrote it from SPEC.txt alone
and haven't seen any server or client code in this conversation: no bserve,
bhttp.py or client.py. The spec mentions HEXDUMP.md, bserve and bcurl; I
didn't open any of them. The first file you attached, demo_script.txt, turned
out to be your procurement demo script rather than the spec, and I stopped
searching when you rejected the file search.

Testing: I ran it on port 9100 against a small test client I wrote from the
spec, not your client.py. All 38 checks passed. They covered:
- GET / and the 40,000-byte blob, which arrived as DATA frames of 16384, 16384
  and 7232 bytes with a matching sha256.
- A 16384-byte file arriving as one DATA frame.
- HEAD, empty files and 304 (by etag and by *) all ending on the HEADERS
  frame.
- 400 for a bad method followed by a 200 on the same connection.
- 400 for repeated or lower stream IDs, .., a path without a leading /,
  underrun and overrun, invalid UTF-8, duplicate headers, reserved header
  indexes and uppercase header names.
- 404 for missing files, a trailing / on a file, %2e%2e, and a symlink
  pointing outside www.
- Unknown frame types and client DATA skipped.
- GOAWAY(3) for a bad preface; GOAWAY(2) when a frame's Length is 16385 (16384
  is accepted); GOAWAY(1) for HEADERS on stream 0, a malformed GOAWAY, or a
  connection cut mid-frame.
- A clean close after the client sends GOAWAY.
- The idle and slow-frame timeouts fired at 30.0s and 10.0s.

Where it differs from your brief:
- It's 281 lines, not 150-200 (about 240 without blank lines and comments).
  The extra length comes from covering the spec's whole error list. I can cut
  it down if you'd accept dropping some of the rarer cases.
- It also imports time, which is needed for the date header and the timeouts.
  Everything else is socket, struct, sys and os.
- It listens on 127.0.0.1 (IPv4) only and handles one connection at a time.
  Your client should connect without trouble, unless it insists on IPv6.
