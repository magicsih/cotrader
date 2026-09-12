# 배포

## 실행 구조

`Deployment` 하나, `replicas: 1`, `strategy: Recreate`입니다. 주문 잠금은 한 프로세스만 쥘 수 있으므로 이전 파드가 완전히 사라진 뒤에 새 파드가 떠야 합니다.

봇은 아무것도 서빙하지 않습니다. 텔레그램과 거래소로 나가는 연결만 쓰므로 Service도 Ingress도 없고 NetworkPolicy에 ingress 규칙을 두지 않습니다. HTTP probe 대신 텔레그램 하트비트를 씁니다. 주문 실행기가 2분 넘게 조용하면 알림이 옵니다.

이미지는 distroless static 위의 정적 바이너리로 약 17MB입니다.

## 준비할 값

| 대상 | 내용 |
|---|---|
| 이미지 | ARM64·AMD64를 빌드하고 digest로 고정 |
| DB | 기존 MySQL에 Cotrader 전용 DB와 사용자 |
| DB 권한 | 스키마의 SELECT·INSERT·UPDATE·DELETE와 마이그레이션용 DDL |
| 외부 통신 | 고정 출구 IP를 토스와 업비트 허용 목록에 등록 |
| 계좌 | 종합매매 계좌가 여러 개면 `COTRADER_ACCOUNT_SEQ` |
| Telegram | 기존 웹훅과 다른 polling 소비자가 없는 전용 봇, 본인 ID |
| 백업 | cotrader DB의 클러스터 밖 암호화 백업과 복원 검증 |

`deploy/k8s`는 Secret을 만들지 않고 다음 이름을 참조합니다.

- `cotrader-database` — `COTRADER_DATABASE_URL`
- `cotrader-identity` — `TELEGRAM_API_KEY`, `TELEGRAM_ME`
- `cotrader-toss` — 선택. 토스 클라이언트 ID·시크릿과 `COTRADER_ACCOUNT_SEQ`
- `cotrader-upbit` — 선택. 업비트 키 세 쌍
- `cotrader-migration-database` — 스키마 변경이 있을 때만. `COTRADER_DATABASE_URL`에 DDL 권한 계정

## 스키마 변경

봇은 스키마를 **확인만** 합니다. 적용에는 DDL 권한이 필요한데, 항상 떠 있는 프로세스가 자기 테이블을 지울 수 있는 권한까지 가질 이유가 없습니다. 런타임 계정은 SELECT·INSERT·UPDATE·DELETE만 가집니다.

코드가 기대하는 스키마보다 DB가 낮으면 봇은 **기동을 거부하고** 무엇을 해야 하는지 말합니다. 낮은 스키마에 맞지 않는 행을 쓰느니 뜨지 않는 편이 낫습니다.

스키마 변경이 포함된 배포는 이 순서로 합니다.

```bash
kubectl -n cotrader scale deployment/cotrader-bot --replicas=0
kubectl -n cotrader delete job cotrader-migrate --ignore-not-found
kubectl -n cotrader apply -f deploy/migration-job.yaml
kubectl -n cotrader logs job/cotrader-migrate
kubectl -n cotrader scale deployment/cotrader-bot --replicas=1
```

봇을 먼저 내리는 이유는 마이그레이션이 같은 단일 실행 잠금을 가져가기 때문입니다. 두 쪽이 동시에 스키마를 건드릴 수 없습니다.

## 실행 순서

1. CI의 정적 검사·테스트·양쪽 아키텍처 빌드를 통과시킵니다. `kubectl kustomize deploy/k8s`로 출력물을 검토합니다.
2. `main`에 반영된 커밋이 CI를 통과하면 `sha-<commit>` 이미지가 GitHub Container Registry에 올라갑니다. CI 요약의 digest로 운영 이미지를 고정합니다.
3. DB·Secret·백업을 준비합니다. Secret 동기화는 값이 출력되지 않는 승인된 경로를 씁니다.
4. 주문 스위치를 모두 끈 상태로 배포합니다. 스키마 변경이 포함되어 있으면 위 절차를 먼저 실행합니다.
5. 텔레그램 연결과 잔고 조회를 확인한 뒤 [운영 문서](live-trading.md)의 절차로 시장을 하나씩 켭니다.

## GitHub Actions

`main` 푸시가 CI를 통과하면 `deploy` job이 `vzyx-cluster` Environment의 승인을 기다립니다. 운영자가 Approve해야 롤아웃이 시작됩니다. 승인은 배포 승인이며 주문 허용 승인이 아닙니다.

이 job은 `cotrader-bot` 하나의 이미지를 digest로 교체하고 `rollout status`를 확인합니다. ConfigMap, NetworkPolicy, 주문 스위치는 자동화하지 않습니다.

저장소가 공개이므로 ARC 자체 호스팅 러너를 쓰지 않고 GitHub-hosted 러너를 씁니다.

### 승인 전 확인

- `/orders`로 진행 중인 사다리가 없거나, 있어도 재시작을 견딜 상태인지 확인합니다. 롤아웃 중 프로세스가 교체되며 대기 주문은 거래소에 그대로 남습니다.
- `확인 필요` 주문이 있으면 먼저 대조합니다.

## 중단과 복구

- 배포 전에 `/cancel`로 대기 주문을 정리하면 가장 단순합니다. 보유 자산은 유지됩니다.
- 정전이나 DB 장애 중에는 앱이 취소를 완료할 수 없습니다. 거래소 앱에서 남은 주문을 확인할 수 있어야 합니다.
- `확인 필요` 주문을 DB에서 직접 다른 상태로 바꾸지 마세요. 거래소의 주문 목록과 종목·방향·수량·가격·시각을 대조해야 합니다.
- 롤백은 대기 주문을 정리한 상태에서 이전 이미지로 수행합니다. DB를 과거 백업으로 되돌리면 실제 거래소 주문과 달라집니다.
