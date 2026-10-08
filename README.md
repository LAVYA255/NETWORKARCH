# BHTTP/1: HTTP, but binary

Network Architecture course project. Lavya Tanotra, 24BCS10124. Solo submission.

There's one protocol and two programs, and the only thing shared between them is the spec (`SPEC.md`, which is also the submitted two-page document).

- `bserve ROOT PORT` is the server (track 1). It accepts a connection, reads binary request frames, maps each path to a file under ROOT and answers with a status, headers and the file. Missing files get 404, malformed requests get 400, and in both cases the connection stays open. PORT 0 picks a free port. On startup it prints `bserve: listening on port N`. If the host supports it, it listens on IPv6 and IPv4 at once; otherwise just IPv4.
- `bcurl [-v] [-I] host[:port][/path] [/path ...]` is the client (track 2). It builds the binary request, writes the body to stdout and exits non-zero on 4xx/5xx. `-v` hexdumps every frame in both directions to stderr (error GOAWAYs included), and `-I` sends HEAD. All the paths go over a single connection. Port defaults to 9000 and path to `/`. IPv6 literals need brackets: `[::1]:9000/`.
- `bhttp.py` is the frame codec that both programs use.
- `HEXDUMP.md` is the annotated hexdump deliverable. It's one real exchange: `bcurl -v localhost:9000/index.html` against a running bserve on port 9000, and every byte (the date too) comes from bcurl's own `-v` output. `HEXDUMP.txt` is the same dump without the notes, and `make_hexdump.py` regenerates both.
- `interop/` holds the interop evidence (more on that below).
- `tests/`: run with `python3 -m unittest discover -s tests -v`.

Needs Python 3.9+ and nothing outside the standard library. Linux only.

```
$ ./bserve ./www 9000 &
$ ./bcurl -v localhost:9000/index.html
```

**bcurl exit codes:**

| Code | Meaning |
|---|---|
| 0 | 1xx to 3xx |
| 4 | 4xx |
| 5 | 5xx |
| 1 | Connection or protocol error, timeout, or the server sent GOAWAY |
| 2 | Bad usage: arguments, port, a path too long for one frame, or a bad environment value |

**Tunables (environment variables).** These are validated, and a bad value exits with 2.

| Variable | Default | What it controls |
|---|---|---|
| `BHTTP_FRAME_TIMEOUT` | 10 s | How long one frame has to finish arriving once its first byte is in |
| `BSERVE_IDLE_TIMEOUT` | 30 s | If no new request comes in for this long (counting from connect or from the end of the last response), the server closes with GOAWAY(0) |
| `BSERVE_MAX_CONNECTIONS` | 64 | Connection cap. When full, the server drops new connections before reading the preface, without a GOAWAY |
| `BCURL_TIMEOUT` | 30 s | Connect timeout, and the longest bcurl will wait for a frame |

## Design decisions I'd point a reviewer at

- **GOAWAY actually reaches the peer.** Every GOAWAY is sent through `send_goaway_and_close`: send it, half-close (FIN), read and discard input for up to 1 s or 64 KiB, then close. My first version just closed the socket with unread input still buffered, and the kernel turned that into an RST that destroyed the GOAWAY. In a 30-run check it lost the GOAWAY all 30 times. Now the GOAWAY survives as long as the peer stops sending within 1 s / 64 KiB. Beyond that limit the close can still become an RST, and that's on purpose: a peer that never stops sending shouldn't be able to hold the connection open. `tests/test_bhttp.py` checks for a clean EOF after every GOAWAY, including with 20 KB of unread payload pending and when a real HTTP/1 request shows up in place of the preface.
- **No TOCTOU on paths.** `open_in_root` resolves the path, then opens it again one component at a time with `O_NOFOLLOW`, starting from a file descriptor for the root. If someone swaps in a symlink between the check and the open, the walk fails. Size, type and mtime all come from `fstat` on the open descriptor. A name that's too long for the file system is a 404 like any other missing file.
- **All ten static table names are actually used.** bcurl sends 1 to 3 and bserve sends 4 to 10. Index 8 used to be `connection`, which means nothing on a connection that's always persistent, so I replaced it with `etag`. bserve sends it alongside `last-modified` and `cache-control`, and it's what makes `if-none-match` / 304 work.

## Tests

127 tests across three files, plus helpers in `tests/support.py`. Two get skipped when running as root on a machine without IPv6:
- the mode-000 file test, since root can read the file anyway. The 500 path it covers is also tested in-process, and the test passes as a normal user.
- the dual-stack test, since there's no IPv6.

What each file covers:

- **`tests/test_bhttp.py`** runs the real bserve and talks to it over raw sockets:
  - Normal cases and boundaries: 40,000 bytes split 16384/16384/7232, a 16384-byte file in exactly one frame, a 16384-byte HEADERS payload.
  - HEAD, 304 (on HEAD too), an empty file, `/` and directory index, a literal `%2e%2e` (404), `//` and `.` segments, a trailing slash on a file (`/index.html/` is 404, `/index.html/.` is 200), and an overlong path component (404, connection still alive).
  - Fifteen kinds of malformed request, including an empty literal name and invalid UTF-8 in a header value. Each one checks for **a 400 followed by a 200 on the same connection**. Also repeated and decreasing stream IDs.
  - Symlink escapes, unknown frame types (one of them exactly 16384 bytes), and every connection error, each checking for a GOAWAY and then a clean EOF.
  - Idle, slow-preface and slow-frame timeouts, and a full server.
  - In-process server tests: 500 on an OSError, GOAWAY(4) when a file shrinks mid-response, exiting quietly on a peer reset, `respond()` argument checks, and the TOCTOU symlink swap.
  - bcurl against scripted fake servers: exit codes; timeout (with GOAWAY(0)); every bad content-length I could think of (`١٢`, `²`, `+12`, ...), with 18 digits accepted and 19 refused; over-length output stopped before the extra bytes are written; HEAD or 304 HEADERS without END_STREAM refused immediately; oversize frames (GOAWAY(2)); a server GOAWAY that bcurl doesn't answer; stray frames between responses (GOAWAY(1)) and after the last one (never written to stdout); and `-v`. Several of these responses are built by hand rather than with the codec.
  - In-process client tests over a socketpair, and codec tests: underrun at every possible truncation point, overrun, index bounds, name rules.
- **`tests/test_golden.py`** never imports the codec. It checks:
  - bcurl's bytes against hand-built bytes.
  - bcurl against a hard-coded golden response.
  - bserve's response **byte for byte**, plus every header value parsed by hand. The date at offset 0x19 is checked to be an IMF-fixdate, and those 29 bytes are the only ones masked.
  - that `HEXDUMP.txt` and `HEXDUMP.md` contain the same real exchange: the request matches the hand-built request for its `localhost:PORT` host, and the response matches the golden one with the same date mask. It also checks that the examples in SPEC.md use the golden frame headers.
- **`tests/test_interop.py`** covers:
  - the clean-room client against bserve;
  - bcurl against the clean-room server;
  - both clients going through the fault-injecting proxy;
  - the clean-room client's own GOAWAY checks and timeout handling;
  - an AST check that pins the imports of both spec-only programs.

## Interoperability

The brief asks for a partner who builds the other side from the spec alone. **I didn't have a partner, and nothing in this folder is a substitute for one.** This is what I do have, from weakest evidence to strongest:

1. **An earlier reference pair** (`bhttp-reference.tar.gz`, 24 September). I wrote it separately, against the draft spec, and it's extracted here as `./ref/` (`ref/bserve`, `ref/bcurl`). The matrix picks it up automatically. It follows the old draft, so index 8 is `connection` and not `etag`, and its responses differ in the ways listed below. Since I wrote it too, it only shows that my own programs agree with each other, not that someone else could build from the spec.
2. **`interop/cleanroom_client.py`.** A minimal client written from SPEC.md with no shared code. It imports only `os`, `socket`, `struct` and `sys`, and it runs under `python3 -I` so it can't import `bhttp.py`. It has its own static table and frame code. But I wrote it after having seen `bhttp.py`, so it's spec-only code, not a true clean room.
3. **`interop/cleanroom_server.py`.** Same idea on the server side, so bcurl has a peer other than bserve. It imports only `os`, `socket`, `struct`, `sys` and `time` (for the date header) and runs under `python3 -I`. Same caveat as the client.
4. **`interop/his_client.py`.** A client built from SPEC.md alone in a separate LLM session (a different model from the one used on this project) that had no access to anything in this repository: not the server, not bcurl, not the tests. I ran that session, tested the client against the reference server, and it's included here byte for byte (sha256 is in the manifest). `interop/his_shim.py` is just harness glue that adapts the client's command line to the matrix; it can't change how the client behaves on the wire. This is the strongest evidence I have, because whoever wrote it knew nothing about my code and the spec was enough.
5. **`interop/run.sh`.** A matrix you can rerun: every server here against every client here. Each run goes through `interop/inject.py`, which records every byte and, in some scenarios, damages them:

   | Scenario | What it shows |
   |---|---|
   | GET 40,000 bytes, HEAD, 404 | Basic exchanges |
   | Three paths on one connection | The one-connection rule (the proxy counts connections) |
   | Unknown frames injected both ways | Unknown frames get skipped across implementations |
   | Flipped method byte | Server answers 400 |
   | Flipped preface | Server sends GOAWAY(3) |
   | Flipped response Length | Client sends GOAWAY(2) |
   | Truncated response | Client stops |
   | Slow link | Delays don't break the exchange |

   Transcripts (the raw `.bin` files, a hexdump and a log per run) go into `interop/transcripts/`. The script finishes with the tally and a sha256 of every artifact.

**First complete run (2026-10-08).** This was before the clean-room server existed, so the servers were mine and ref, and the clients were bcurl, cleanroom and refcurl. Result: 51 PASS, 6 FAIL.

Every row with my server passed, including all the ones with the reference client. All six failures were the reference server, against both bcurl and the clean-room client:

- **missing_404 and corrupt_method_400 (stdout differs).** The draft-era server puts a short body on its 404 and 400. The clients wrote that body to stdout and exited 4, which is correct. The matrix expects empty stdout because SPEC.md section 6 now says errors have no body.
- **truncated_response (exit 0, want 1).** The draft-era response to `/index.html` is shorter than 140 bytes, so the proxy's cut lands after the response has already ended. The clients really did get a complete response.

So those rows show the reference pair drifting from the current spec, not bcurl or the clean-room client getting it wrong. `run.sh` now lists exactly these (server, scenario) pairs as known draft-spec drift. If a listed row fails in exactly the documented way, it prints XFAIL with the reason. Any other failure is still FAIL, including a listed row failing in some other way.

**Latest run of this tree (2026-10-09):** 138 passed, 18 xfail (documented drift), 4 n/a (not run), 0 failed, out of 156 rows. Servers: bserve, cleanroom_server, the draft reference server and the independent his_server. Clients: bcurl, cleanroom_client, refcurl and the independent his_client. bcurl passed all ten scenarios against his_server, so both sides of the wire now have an independently written peer. his_server passed 38 of its 39 rows, and its one XFAIL is the draft reference client failing to send GOAWAY(2) on an oversize Length, which isn't his_server's fault. The four n/a rows are `head` with the independent client, which only does GET; run.sh prints N/A for those and doesn't count them. All 18 xfail rows are draft-reference drift: the draft server's error bodies, its GOAWAY(1) instead of GOAWAY(3) on a bad preface (checked on the wire for every client, not just his_client), its response being too short for the truncation cut, and the draft client not sending GOAWAY(2) on an oversize response Length (also checked on the wire for every client). For the `his` error rows, the client's own log also has to contain the scenario's signature line, and the one-connection check counts real connections at the proxy, so a PASS can't happen by accident. Wire transcripts, per-row logs and the sha256 manifest are in `interop/transcripts/` and `interop/matrix_output.txt`.

There are two things the independent client gets wrong. It accepts a content-length longer than 18 ASCII digits, which SPEC.md section 6 forbids (receivers MUST reject any other value), and it exits 1 on a 404 instead of telling statuses apart. I've left both as they are rather than patching them, because the whole point of his_client is that someone else wrote it from the spec, and these are exactly the two spots where a fresh reader slipped. I tightened section 6 after finding the first one.

**What's still open:** both independent programs were still produced by me driving an LLM, not by a classmate who had nothing to do with the project. They show the spec can be implemented from its text alone. They don't show that an uninvolved person finds it easy to read.

## Authorship and process

I'm the sole author of this submission. I used AI assistance (Anthropic's Claude) for code, tests, spec wording and several rounds of adversarial review of the whole project. The design decisions were mine and I checked every result.

Two pieces of the interop evidence are independent of this project's code: `interop/his_client.py` (sha256 43c76a48...93aad) and `interop/his_server.py` (sha256 0fd516a8...a7cb). Each was written from SPEC.md alone in a separate LLM session that never saw this repository. The client session used a different model family from the one I used for the project, so it's the more independent reader. The server session used Claude Opus 5.5, the same family, which means it shares some assumptions with the spec's wording, so it's the weaker of the two. The server session explicitly confirmed it hadn't seen bserve, bhttp.py or any client code. Its 38 passing checks were tests it wrote for itself, so I don't count them as interop evidence. I ran both sessions myself and tested both programs against this tree before adding them to the matrix.

On feedback: nothing from bserve, bcurl, the tests or the matrix output was passed into either session. The server session only ever saw SPEC.txt and its own code (the transcript shows the whole exchange). The client session's only contact with this project was me running its output against the reference server and this tree after it was finished. The server session's transcript is in `interop/sessions/his_server-session.md`, cut down to the cold-build evidence: the prompt, the spec-only constraint, the model saying it hadn't seen any existing code, and its 38-check test summary. I removed local paths, code diffs and the troubleshooting at the end, and everything I kept is verbatim. The fuller chat export it was cut from is next to it as `his_server-session-fullexport.md` (sha256 461d5091...5fb4, versus 8838980f...c0f2 for the trimmed one). That's the session as exported from the chat UI, not the native Claude Code log; the session's own attempts to produce a verbatim export inside the chat failed, as the end of the export shows.

Which spec version they saw: both sessions got the spec as it stood on the afternoon of 2026-10-08 (his_server's session read it as SPEC.txt; that revision of SPEC.md has sha256 ba5fba35...a8d9). The submitted SPEC.md differs from it only by the edits I made that evening: the explicit receiver MUST on content-length, "errors carry content-length 0", the sentence about pipelined requests, the stream-state wording in section 3, and the reworded idle definition. Neither session saw those. That fits with his_client accepting a 19-digit content-length: at the time, the receiver rule was only in section 9's list of client errors, not next to the content-length definition in section 6.

For the viva, "how does a client cancel a request?": it sends GOAWAY and reconnects, so cancelling costs you the connection. There's deliberately no RST_STREAM. With only one stream open at a time, a fresh connection does the same job. Apart from the two independent programs, everything here comes from one author working with one assistant, the spec-only programs included.

## Checking the spec PDF

The submitted spec PDF is `BHTTP-1-spec.pdf`, rendered from SPEC.md on A4 and confirmed to be exactly 2 pages (`pdfinfo BHTTP-1-spec.pdf | grep Pages` prints `Pages: 2`). It was built with pandoc (SPEC.md to HTML) and then `wkhtmltopdf --disable-smart-shrinking --page-size A4` at 11pt with 20mm margins.

`make_hexdump.py` starts bserve on port 9000 (if 9000 is busy it falls back to a free port and tells you). It gives `index.html` a fixed mtime so last-modified and etag don't change between runs, runs the real `bcurl -v`, and rebuilds both byte streams from bcurl's own hexdump of everything it sent and received.

Before writing anything, it checks that:
- the bytes sent equal the codec-built preface, request and GOAWAY(0);
- the response equals the codec-built response, with only the 29-byte live date masked.

Every annotation is computed from the bytes, with assertions that:
- Length fields equal payload sizes;
- value lengths equal value sizes;
- indexes map through the SPEC table;
- END_STREAM is where section 6 says it should be.

SPEC.md is laid out to fit on two pages at 11pt (A4 or Letter, 20mm margins), so after any edit, rebuild it with pandoc and wkhtmltopdf and check the page count.
