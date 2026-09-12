#!/usr/bin/env bash
# Runs the Go test suite with a throwaway MySQL so the integration tests have a
# real server. The container uses tmpfs and is removed on exit.
set -euo pipefail
test_container="cotrader-test-$$"
cleanup() { docker rm -f "$test_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run --detach --name "$test_container" --publish 127.0.0.1::3306 \
  --env MYSQL_ROOT_PASSWORD=isolated-test-root \
  --env MYSQL_DATABASE=cotrader_test --env MYSQL_USER=cotrader \
  --env MYSQL_PASSWORD=isolated-test-only --tmpfs /var/lib/mysql mysql:9.2.0 >/dev/null
test_db_ready=0
for attempt in {1..60}; do
  if docker exec "$test_container" mysql --protocol=TCP --host=127.0.0.1 --user=cotrader \
    --password=isolated-test-only --database=cotrader_test --execute='SELECT 1' >/dev/null 2>&1; then
    test_db_ready=1
    break
  fi
  sleep 1
done
if [[ "$test_db_ready" != 1 ]]; then
  echo '임시 MySQL의 TCP 연결과 테스트 사용자 준비에 실패했습니다.' >&2
  exit 1
fi
test_port="$(docker inspect --format '{{(index (index .NetworkSettings.Ports "3306/tcp") 0).HostPort}}' "$test_container")"
export COTRADER_TEST_DSN="cotrader:isolated-test-only@tcp(127.0.0.1:${test_port})/cotrader_test"
go test "$@" ./...
