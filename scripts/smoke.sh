#!/usr/bin/env bash
# Smoke test of the image, as CI runs it (`make smoke` runs the same):
#     scripts/smoke.sh IMAGE
# The container runs as in production: read-only root, no capabilities, uid 65532, /data and
# /media on tmpfs. Checked: the image's healthcheck, the MCP tools and their annotations, the
# admin UI, a feed round trip, the public routes' answers, ffmpeg and ffprobe, a log without
# the public base, and that `docker stop` (SIGTERM) ends the process by itself within 8 s.
# The container is always removed; its log is printed when a check fails.
set -euo pipefail

image=${1:?usage: scripts/smoke.sh IMAGE}
name=podcast-feed-smoke
public_base=https://podcast-api.example.com/smoke
stop_limit_s=8
tmp=$(mktemp -d)

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- smoke test failed; the container's log:" >&2
    docker logs "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$tmp"
  exit "$status"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# expect STATUS METHOD PATH [curl options...]: the answer's status must be STATUS; the body is
# left in $tmp/body.
expect() {
  local want=$1 method=$2 path=$3 got
  shift 3
  got=$(curl -sS -o "$tmp/body" -w '%{http_code}' -X "$method" "$@" "http://$web$path")
  [ "$got" = "$want" ] || fail "$method $path answered $got, expected $want"
  echo "ok: $method $path -> $got"
}

docker rm -f "$name" >/dev/null 2>&1 || true
# Host ports are picked by Docker (127.0.0.1 only), so the test never collides with a local
# service on 8000 or 8080. SMOKE_PLATFORM (set by `make smoke`) runs the platform it built.
docker run -d --name "$name" ${SMOKE_PLATFORM:+--platform "$SMOKE_PLATFORM"} \
  --read-only --tmpfs /tmp --tmpfs /data:mode=1777 --tmpfs /media:mode=1777 --cap-drop ALL \
  -e PODCAST_PUBLIC_BASE="$public_base" --health-interval 1s \
  -p 127.0.0.1::8000 -p 127.0.0.1::8080 "$image" >/dev/null
mcp=$(docker port "$name" 8000/tcp | head -n 1)
web=$(docker port "$name" 8080/tcp | head -n 1)

# The image's own HEALTHCHECK (both ports), run every second here instead of every 30 s.
health=starting
for _ in $(seq 1 30); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$name")
  [ "$health" = healthy ] && break
  sleep 1
done
[ "$health" = healthy ] || fail "not healthy after 30 s ($health)"
echo "ok: healthy"

python3 scripts/smoke_mcp.py "http://$mcp/mcp"
curl -fsS "http://$web/ui/" >/dev/null
echo "ok: GET /ui/"

# uid 65532 (real, effective, saved, filesystem) and no capabilities, for the app itself.
proc_status=$(docker exec "$name" cat /proc/1/status)
uids=$(awk '/^Uid:/ { print $2, $3, $4, $5 }' <<<"$proc_status")
[ "$uids" = "65532 65532 65532 65532" ] || fail "PID 1 runs as uid $uids"
caps=$(awk '/^Cap(Prm|Eff):/ { print $2 }' <<<"$proc_status" | sort -u)
[ "$caps" = 0000000000000000 ] || fail "PID 1 has capabilities $caps"
echo "ok: PID 1 runs as uid 65532 with no capabilities"
for tool in ffmpeg ffprobe; do
  docker exec "$name" "$tool" -version | sed -n 1p
done
# What the worker does with a file, in miniature: encode AAC into media/.tmp, then probe it.
docker exec "$name" ffmpeg -nostdin -v error -f lavfi -i sine=duration=1 -c:a aac -b:a 128k \
  -f ipod -y /media/.tmp/smoke.m4a
codec=$(docker exec "$name" ffprobe -v error -show_entries stream=codec_name -of csv=p=0 \
  /media/.tmp/smoke.m4a)
[ "$codec" = aac ] || fail "ffmpeg wrote '$codec', expected aac"
docker exec "$name" rm /media/.tmp/smoke.m4a
echo "ok: ffmpeg encodes AAC into /media/.tmp"

# A feed round trip: SQLite on /data is writable, and the feed's links use the public base.
expect 303 POST /ui/feeds -H 'Sec-Fetch-Site: same-origin' --data 'title=Smoke'
expect 200 GET /feeds/smoke.xml
grep -q "<atom:link href=\"$public_base/feeds/smoke.xml\"" "$tmp/body" ||
  fail "the feed does not link to itself under the public base"
expect 404 GET /feeds/nope.xml
grep -qx 'no such feed' "$tmp/body" || fail "GET /feeds/nope.xml: unexpected body"
expect 403 POST /feeds/smoke.xml
expect 405 POST /feeds/smoke.xml -H 'Sec-Fetch-Site: same-origin'
expect 303 GET /

# SIGTERM: the process must stop by itself (exit status 0), well before Docker's 10 s
# timeout would kill it (137).
TIMEFORMAT=%R
elapsed=$({ time docker stop "$name" >/dev/null; } 2>&1)
echo "docker stop took ${elapsed} s"
awk -v s="$elapsed" -v limit="$stop_limit_s" 'BEGIN { exit !(s < limit) }' ||
  fail "docker stop took ${elapsed} s (limit ${stop_limit_s} s)"
code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
[ "$code" = 0 ] || fail "the container exited with status $code"
echo "ok: stopped by SIGTERM, exit status 0"

docker logs "$name" >"$tmp/log" 2>&1
if grep -q 'podcast-api.example.com' "$tmp/log"; then
  fail "the log shows the public base"
fi
echo "ok: the log never shows the public base"
echo "--- the container's log:"
cat "$tmp/log"
echo "smoke test passed"
