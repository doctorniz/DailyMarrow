#!/bin/bash
# Test Daily Marrow Docker build locally
# Run: ./test-docker.sh

set -e

echo "=== Building Daily Marrow Docker image ==="
docker build -t daily-marrow:test .

echo "=== Starting container ==="
docker run -d --name daily-marrow-test -p 8091:8091 daily-marrow:test
sleep 5

echo "=== Testing endpoints ==="
echo "Frontend:"
curl -s http://localhost:8091/ | grep -q "Daily Marrow" && echo "✅ Frontend OK" || echo "❌ Frontend failed"

echo "Edition JSON:"
curl -s http://localhost:8091/edition.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'✅ edition.json has {len(d.get(\"sections\",[]))} sections')" 2>&1 || echo "❌ edition.json invalid"

echo "Refresh status:"
curl -s http://localhost:8091/refresh-status | grep -q "running" && echo "✅ Refresh status OK" || echo "❌ Refresh status failed"

echo "=== Cleanup ==="
docker stop daily-marrow-test 2>/dev/null || true
docker rm daily-marrow-test 2>/dev/null || true

echo "=== Done ==="
