#!/bin/bash
# Smoke-test the Daily Marrow Docker image. CI runs this same script.
#
#   ./test-docker.sh              build the image, then test it
#   SKIP_BUILD=1 ./test-docker.sh test an image that is already built
#
# Exits non-zero if any check fails.

set -u

IMAGE="${IMAGE:-daily-marrow:test}"
NAME="daily-marrow-test"
PORT="${PORT:-5551}"
BASE="http://localhost:$PORT"
EDITION_WAIT="${EDITION_WAIT:-120}"   # seconds to wait for the first edition
failures=0

pass() { echo "✅ $1"; }
fail() { echo "❌ $1"; failures=$((failures + 1)); }

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -z "${SKIP_BUILD:-}" ]; then
    echo "=== Building $IMAGE ==="
    docker build -t "$IMAGE" . || exit 1
fi

echo "=== Starting container ==="
cleanup
docker run -d --name "$NAME" -p "$PORT:5551" "$IMAGE" >/dev/null || exit 1

# Wait for the server instead of a fixed sleep
for _ in $(seq 1 30); do
    curl -fs -o /dev/null "$BASE/refresh-status" && break
    sleep 1
done

echo "=== Checking endpoints ==="
curl -fs "$BASE/" | grep -q "Daily Marrow" \
    && pass "Front page served" || fail "Front page not served"

curl -fs "$BASE/settings" | grep -q "How the edition is made" \
    && pass "Settings page served" || fail "Settings page missing"

code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/settings")
[ "$code" = "401" ] && pass "Settings API locked" || fail "Settings API returned $code, expected 401"

curl -fs "$BASE/refresh-status" | python3 -c "import json,sys; assert 'running' in json.load(sys.stdin)" \
    && pass "Refresh status is JSON" || fail "Refresh status invalid"

echo "=== Checking security headers ==="
headers=$(curl -fs -D - -o /dev/null "$BASE/" | tr -d '\r')
for h in "Content-Security-Policy: default-src 'self'" "X-Content-Type-Options: nosniff" \
         "Referrer-Policy: no-referrer" "X-Frame-Options: DENY"; do
    echo "$headers" | grep -qi "^$h" && pass "${h%%:*} set" || fail "${h%%:*} missing"
done

echo "=== Checking the container ==="
uid=$(docker exec "$NAME" sh -c 'cat /proc/1/status' | awk '/^Uid:/ {print $2}')
[ -n "$uid" ] && [ "$uid" != "0" ] && pass "Server runs as non-root (uid $uid)" || fail "Server runs as root"

echo "Waiting up to ${EDITION_WAIT}s for the first edition..."
ok=""
for _ in $(seq 1 "$EDITION_WAIT"); do
    if n=$(curl -fs "$BASE/edition.json" 2>/dev/null \
           | python3 -c "import json,sys; print(len(json.load(sys.stdin)['sections']))" 2>/dev/null); then
        ok=1; break
    fi
    sleep 1
done
[ -n "$ok" ] && pass "edition.json has $n sections" || fail "edition.json never became valid"

# Health checks run every 30s, so only fail if the container already says unhealthy
health=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
[ "$health" != "unhealthy" ] && pass "Health check status: $health" || fail "Container is unhealthy"

start=$(date +%s)
docker stop "$NAME" >/dev/null
took=$(( $(date +%s) - start ))
[ "$took" -lt 5 ] && pass "Stops cleanly in ${took}s" || fail "docker stop took ${took}s (SIGTERM ignored?)"

echo
if [ "$failures" -gt 0 ]; then
    echo "=== $failures check(s) failed ==="
    docker logs "$NAME" 2>&1 | tail -40
    exit 1
fi
echo "=== All checks passed ==="
