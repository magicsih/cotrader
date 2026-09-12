#!/usr/bin/env bash
set -euo pipefail
case "${1:-start}" in
  start)
    if docker container inspect cotrader-dev-mysql >/dev/null 2>&1; then
      docker start cotrader-dev-mysql >/dev/null
    else
      docker run --detach --name cotrader-dev-mysql \
        --publish 127.0.0.1:13316:3306 \
        --env MYSQL_ROOT_PASSWORD=local-root-development-only \
        --env MYSQL_DATABASE=cotrader --env MYSQL_USER=cotrader \
        --env MYSQL_PASSWORD=local-development-only \
        --mount type=volume,src=cotrader-dev-mysql-data,dst=/var/lib/mysql \
        mysql:9.2.0 >/dev/null
    fi
    for attempt in {1..60}; do
      if docker exec cotrader-dev-mysql mysql --protocol=TCP --host=127.0.0.1 --user=cotrader \
        --password=local-development-only --database=cotrader --execute='SELECT 1' >/dev/null 2>&1; then
        echo '로컬 MySQL 준비됨: cotrader:local-development-only@tcp(127.0.0.1:13316)/cotrader'
        exit 0
      fi
      sleep 1
    done
    echo 'MySQL 준비 시간을 초과했습니다. 로컬 컨테이너 로그를 확인하세요.' >&2
    exit 1
    ;;
  stop) docker stop cotrader-dev-mysql ;;
  *) echo '사용법: bash scripts/local-db.sh start|stop' >&2; exit 2 ;;
esac
