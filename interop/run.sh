#!/usr/bin/env bash
# interop/run.sh: the BHTTP/1 interop matrix, reproducible end to end.
#
# Builds nothing. Runs every server present against every client present,
# through interop/inject.py (which records the bytes and, for some scenarios,
# damages them), and prints one verdict per (server, client, scenario):
#
#   PASS   the row met every expectation below
#   XFAIL  a row listed in DRIFT failed in exactly the documented way
#          (draft-spec drift in the reference pair). Reported, never a pass.
#   FAIL   anything else, including a DRIFT row failing in any other way
#
#   servers: ./bserve, interop/cleanroom_server.py, ./ref/bserve (if present)
#   clients: ./bcurl, interop/cleanroom_client.py, ./ref/bcurl (if present)
#
# A missing program is reported as SKIP, never faked. Raw transcripts go to
# interop/transcripts/: NAME.c2s.bin and NAME.s2c.bin (the bytes as forwarded),
# NAME.hex.txt (xxd, or od if xxd is absent) and NAME.log (exit code, stderr,
# verdict). Ends with the tally and sha256 of every artifact.
# Exit status: 0 only if nothing FAILed.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/transcripts"
PY="${PYTHON:-python3}"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

rm -rf "$OUT"
mkdir -p "$OUT"

# ---- content served by every server ----
WWW="$WORK/www"
mkdir -p "$WWW/sub"
printf '<h1>hi</h1>\n' > "$WWW/index.html"
printf 'sub index\n' > "$WWW/sub/index.html"
"$PY" -c 'import sys; open(sys.argv[1], "wb").write((bytes(range(256)) * 157)[:40000])' "$WWW/blob.bin"
EMPTY="$WORK/empty"; : > "$EMPTY"
cat "$WWW/index.html" "$WWW/sub/index.html" "$WWW/blob.bin" > "$WORK/three"
cat "$WWW/blob.bin" "$WWW/index.html" > "$WORK/blob_index"

free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }

wait_port() {
    "$PY" - "$1" <<'EOF'
import socket, sys, time
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.2).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.05)
sys.exit(1)
EOF
}

hexdump_file() {                     # file -> stdout
    if command -v xxd >/dev/null 2>&1; then xxd "$1"; else od -A x -t x1z -v "$1"; fi
}

# ---- who is present ----
declare -A SERVER CLIENT
SERVER[mine]="$ROOT/bserve"
SERVER[cleansrv]="$PY -I $HERE/cleanroom_server.py"
CLIENT[bcurl]="$ROOT/bcurl"
CLIENT[cleanroom]="$PY -I $HERE/cleanroom_client.py"
[ -x "$ROOT/ref/bserve" ] && SERVER[ref]="$ROOT/ref/bserve" || echo "SKIP  server ref: ./ref/bserve not present"
[ -x "$ROOT/ref/bcurl" ] && CLIENT[refcurl]="$ROOT/ref/bcurl" || echo "SKIP  client refcurl: ./ref/bcurl not present"
[ -f "$HERE/his_client.py" ] && CLIENT[his]="$PY -I $HERE/his_shim.py" || echo "SKIP  client his: interop/his_client.py not present"
[ -f "$HERE/his_server.py" ] && SERVER[hissrv]="$PY -I $HERE/his_server_shim.py" || echo "SKIP  server hissrv: interop/his_server.py not present"

# name | inject.py options | client flags | first path | more paths | want exit | want stdout
SCENARIOS=(
  "get_blob|||/blob.bin||0|$WWW/blob.bin"
  "head||-I|/blob.bin||0|$EMPTY"
  "missing_404|||/nope.txt||4|$EMPTY"
  "three_paths_one_conn|||/index.html|/sub/ /blob.bin|0|$WORK/three"
  "unknown_frames_both_ways|--inject-unknown||/blob.bin|/index.html|0|$WORK/blob_index"
  "corrupt_method_400|--flip-byte 12||/index.html||4|$EMPTY"
  "corrupt_preface|--flip-byte 0||/index.html||1|$EMPTY"
  "corrupt_response_length|--direction s2c --flip-byte 0||/index.html||1|$EMPTY"
  "truncated_response|--direction s2c --truncate 140||/index.html||1|*"  # 140 = HEADERS len + 12: inside DATA for bserve, inside the DATA header for cleanroom_server (10-byte longer server name)
  "slow_link|--delay 50||/blob.bin||0|$WWW/blob.bin"
)

# Known draft-spec drift. ./ref/ is the Sept 24 reference pair, written
# against the draft spec before the current SPEC.md. Key "server|scenario";
# value "exit code the drifting row produces|reason". A listed row is XFAIL
# only if it exits with that code over one connection; otherwise it is FAIL.
declare -A DRIFT=(
  ["ref|missing_404"]="4|draft 404 carries a body; SPEC 6 now: errors have none"
  ["ref|corrupt_method_400"]="4|draft 400 carries a body; SPEC 6 now: errors have none"
  ["ref|truncated_response"]="0|draft response is shorter than 140 bytes, so the cut never lands"
  ["ref|corrupt_preface"]="1|draft ref answers a bad preface GOAWAY(1); SPEC 2 now: GOAWAY(3)"
)

# Draft-client drift, key "client|scenario": the draft reference client
# answers an oversize response Length without GOAWAY(2) (it just exits).
declare -A CLIENT_DRIFT=(
  ["refcurl|corrupt_response_length"]="1|draft refcurl sends no GOAWAY(2) on oversize Length; SPEC 9 now requires it"
)

# Independent-client error rows: the substring his_client must log for the
# row to mean anything (verified against interop/his_client.py's messages).
declare -A HIS_ERR=(
  ["missing_404"]="error: 404 Not Found"
  ["corrupt_method_400"]="-> 400,"
  ["corrupt_preface"]="GOAWAY(3)"
  ["corrupt_response_length"]="frame Length"
  ["truncated_response"]="closed the connection mid-frame"
)

pass=0; xfail=0; fail=0; na=0; rows=0
for s in $(printf '%s\n' "${!SERVER[@]}" | sort); do
    sport="$(free_port)"
    ${SERVER[$s]} "$WWW" "$sport" >/dev/null 2>&1 &
    spid=$!
    if ! wait_port "$sport"; then
        echo "FAIL  server $s did not start"; fail=$((fail + 1)); kill $spid 2>/dev/null; continue
    fi
    for c in $(printf '%s\n' "${!CLIENT[@]}" | sort); do
        for row in "${SCENARIOS[@]}"; do
            IFS='|' read -r name popts cflags first more want_exit want_out <<<"$row"
            if [ "$c" = his ] && [ "$name" = head ]; then
                na=$((na + 1))
                printf 'N/A   %-8s <- %-9s %s (%s)\n' "$s" "$c" "$name" "independent client is GET-only; not run"
                continue
            fi
            tag="${s}__${c}__${name}"
            rows=$((rows + 1))
            pf="$WORK/pport_$tag"     # fresh per row: a shared file races its own truncation
            "$PY" "$HERE/inject.py" 0 "$sport" --record "$OUT/$tag" $popts \
                >"$pf" 2>"$WORK/perr" &
            ppid=$!
            for _ in $(seq 200); do [ -s "$pf" ] && break; sleep 0.05; done
            pport="$(awk '{print $NF}' "$pf")"
            timeout 30 ${CLIENT[$c]} $cflags "127.0.0.1:$pport$first" $more \
                >"$WORK/stdout" 2>"$WORK/stderr"
            code=$?
            # the client is done; the proxy accepts further connections until
            # stopped, so the one-connection check below is real. Let it
            # settle briefly, then stop it.
            sleep 0.3; kill $ppid 2>/dev/null; wait $ppid 2>/dev/null
            conns="$(grep -c 'connection' "$WORK/perr")"
            ok=1; why=""
            exp="$want_exit"
            if [ "$c" = his ] && [ "$want_exit" != "0" ]; then
                exp=1
                sig="${HIS_ERR["$name"]:-}"
                if [ -n "$sig" ] && ! grep -qF -e "$sig" "$WORK/stderr"; then
                    ok=0; why="stderr lacks '$sig'"
                fi
            fi
            [ "$code" = "$exp" ] || { ok=0; why="exit $code, want $want_exit"; }
            if [ "$want_out" != "*" ] && ! cmp -s "$WORK/stdout" "$want_out"; then
                ok=0; why="$why; stdout differs"
            fi
            [ "$conns" = "1" ] || { ok=0; why="$why; $conns connections"; }
            if [ "$name" = corrupt_preface ] && \
               [ "$(tail -c 10 "$OUT/$tag.s2c.bin" | xxd -p)" != "00000202000000000003" ]; then
                ok=0; why="$why; s2c lacks GOAWAY(3)"
            fi
            if [ "$name" = corrupt_response_length ] && \
               [ "$(tail -c 10 "$OUT/$tag.c2s.bin" | xxd -p)" != "00000202000000000002" ]; then
                ok=0; why="$why; c2s lacks GOAWAY(2)"
            fi
            if [ $ok = 1 ]; then
                verdict="PASS"; pass=$((pass + 1))
                printf 'PASS  %-8s <- %-9s %s\n' "$s" "$c" "$name"
            else
                drift="${DRIFT["$s|$name"]:-}"
                [ -z "$drift" ] && drift="${CLIENT_DRIFT["$c|$name"]:-}"
                if [ -n "$drift" ] && [ "$code" = "${drift%%|*}" ] && [ "$conns" = "1" ]; then
                    verdict="XFAIL"; xfail=$((xfail + 1))
                    printf 'XFAIL %-8s <- %-9s %s (%s; draft-spec drift: %s)\n' \
                        "$s" "$c" "$name" "${why# }" "${drift#*|}"
                else
                    verdict="FAIL"; fail=$((fail + 1))
                    printf 'FAIL  %-8s <- %-9s %s (%s)\n' "$s" "$c" "$name" "${why# }"
                fi
            fi
            { echo "server=$s client=$c scenario=$name proxy=[$popts]"
              echo "exit=$code want=$want_exit connections=$conns verdict=$verdict"
              echo "stdout_sha256=$(sha256sum < "$WORK/stdout" | cut -d' ' -f1)"
              echo "--- stderr"; cat "$WORK/stderr"
              echo "--- proxy stderr"; cat "$WORK/perr"; } > "$OUT/$tag.log"
            { echo "== client -> server"; hexdump_file "$OUT/$tag.c2s.bin"
              echo "== server -> client"; hexdump_file "$OUT/$tag.s2c.bin"; } > "$OUT/$tag.hex.txt"
        done
    done
    kill $spid 2>/dev/null; wait $spid 2>/dev/null
done

echo
echo "$pass passed, $xfail xfail (draft-spec drift), $na n/a (not run), $fail failed, of $rows rows"
echo
echo "sha256 of every artifact:"
( cd "$ROOT" && find . -type f \( -path ./interop/transcripts/'*' -o -name '*.py' -o -name '*.md' \
      -o -name '*.txt' -o -name '*.sh' -o -name bserve -o -name bcurl \) \
      -not -path '*/__pycache__/*' -not -path './www/*' | LC_ALL=C sort | xargs sha256sum )
[ $fail = 0 ]
