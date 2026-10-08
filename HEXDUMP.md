# Annotated hexdump: one GET /index.html, one 200 response

Captured by `make_hexdump.py`: the real `bcurl -v localhost:9000/index.html` against a
live `bserve` on port 9000. Both byte streams are rebuilt from bcurl's own
`-v` hexdump, so every byte below crossed the wire, the date included.
`tests/test_golden.py` checks this file again without importing the codec.

- **Date:** `Thu, 08 Oct 2026 14:37:42 GMT`, the server's clock during this run, and the only
  value that changes between runs. The tests mask exactly these 29 bytes
  (response offset 0x19) and compare every other byte.
- **Last-modified and etag:** the generator sets `index.html`'s mtime to
  1790247000 (`Thu, 24 Sep 2026 10:50:00 GMT`), so etag is `"c-6ab50058"`: the size (12 = 0xc)
  and the mtime (0x6ab50058), both in hex.
- **Host:** `localhost:9000`, because bserve was bound to port 9000.

| Frame | Bytes | Length field | Check |
|---|---|---|---|
| preface | 4 | n/a | `BHT1` |
| request HEADERS | 8 + 48 | 0x000030 | 1 + 2 + 11 + 1 + (3+14) + (3+7) + (3+3) = 48 |
| response HEADERS | 8 + 121 | 0x000079 | 2 + 1 + (3+8) + (3+29) + (3+2) + (3+9) + (3+29) + (3+12) + (3+8) = 121 |
| response DATA | 8 + 12 | 0x00000c | equals content-length "12" |
| GOAWAY | 8 + 2 | 0x000002 | SPEC section 4: Length 2, stream 0 |

Every request entry uses a static index (SPEC section 7: 1 host, 2 user-agent,
3 accept); every response entry too (6 server, 7 date, 5 content-length,
4 content-type, 9 last-modified, 8 etag, 10 cache-control). A receiver that
cannot annotate its bytes cannot parse them: every length here is checked
against the bytes that follow (SPEC section 7, parsing rule).

```
CLIENT -> SERVER (offsets count from the first byte the client sends)
0000  42 48 54 31                                      # preface "BHT1" (SPEC section 2)
0004  00 00 30                                         # Length = 0x000030 = 48 payload bytes
0007  00                                               # Type 0x00 = HEADERS
0008  01                                               # Flags 0x01 = END_STREAM
0009  00 00 01                                         # Stream ID 1
000c  01                                               # Method 0x01 = GET
000d  00 0b                                            # Path length = 11
000f  2f 69 6e 64 65 78 2e 68 74 6d 6c                 # Path "/index.html"
001a  03                                               # Header count = 3
001b  01 00 0e                                         # index 1 (host), value length 14
001e  6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30        # "localhost:9000"
002c  02 00 07                                         # index 2 (user-agent), value length 7
002f  62 63 75 72 6c 2f 31                             # "bcurl/1"
0036  03 00 03                                         # index 3 (accept), value length 3
0039  2a 2f 2a                                         # "*/*"

SERVER -> CLIENT (offsets count from the first byte the server sends)
      -- frame 1: response HEADERS (SPEC section 6) --
0000  00 00 79                                         # Length = 0x000079 = 121 payload bytes
0003  00                                               # Type 0x00 = HEADERS
0004  00                                               # Flags 0x00 = none (DATA frames follow)
0005  00 00 01                                         # Stream ID 1
0008  00 c8                                            # Status 0x00c8 = 200
000a  07                                               # Header count = 7
000b  06 00 08                                         # index 6 (server), value length 8
000e  62 73 65 72 76 65 2f 31                          # "bserve/1"
0016  07 00 1d                                         # index 7 (date), value length 29
0019  54 68 75 2c 20 30 38 20 4f 63 74 20 32 30 32 36  # "Thu, 08 Oct 2026 14:37:42 GMT" (live: the server's clock during this run)
0029  20 31 34 3a 33 37 3a 34 32 20 47 4d 54           #   (continued)
0036  05 00 02                                         # index 5 (content-length), value length 2
0039  31 32                                            # "12" (ASCII decimal text, not a binary number)
003b  04 00 09                                         # index 4 (content-type), value length 9
003e  74 65 78 74 2f 68 74 6d 6c                       # "text/html"
0047  09 00 1d                                         # index 9 (last-modified), value length 29
004a  54 68 75 2c 20 32 34 20 53 65 70 20 32 30 32 36  # "Thu, 24 Sep 2026 10:50:00 GMT"
005a  20 31 30 3a 35 30 3a 30 30 20 47 4d 54           #   (continued)
0067  08 00 0c                                         # index 8 (etag), value length 12
006a  22 63 2d 36 61 62 35 30 30 35 38 22              # "c-6ab50058" (the quotes are part of the value)
0076  0a 00 08                                         # index 10 (cache-control), value length 8
0079  6e 6f 2d 63 61 63 68 65                          # "no-cache"
      -- frame 2: response DATA, the body --
0081  00 00 0c                                         # Length = 0x00000c = 12 payload bytes
0084  01                                               # Type 0x01 = DATA
0085  01                                               # Flags 0x01 = END_STREAM
0086  00 00 01                                         # Stream ID 1
0089  3c 68 31 3e 68 69 3c 2f 68 31 3e 0a              # "<h1>hi</h1>\n"

The connection stays open; a next request would use stream 2.
CLIENT -> SERVER, when done (then FIN; SPEC section 10, graceful shutdown)
003c  00 00 02                                         # Length = 0x000002 = 2 payload bytes
003f  02                                               # Type 0x02 = GOAWAY
0040  00                                               # Flags 0x00 = none
0041  00 00 00                                         # Stream ID 0
0044  00 00                                            # Error code 0 = normal
```
