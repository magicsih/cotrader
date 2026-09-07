# Kubernetes 배포와 운영 인수

배포는 운영자의 승인된 환경에서 진행한다. 기본 설정은 실거래 비활성이며, 배포 승인은 실제 주문 시작 승인을 대신하지 않는다.

## 실행 구조

애플리케이션은 `Deployment`로 실행한다. 각 노드마다 실행하는 `DaemonSet`을 사용하지 않는다. 주문 실행기는 한 개만 활성화한다. 기존 MySQL 버전이나 다른 서비스 설정은 변경하지 않는다.

## 배포 전에 준비할 값

| 대상 | 내용 |
|---|---|
| 이미지 | ARM64·AMD64 이미지를 빌드·검증하고 commit 또는 digest로 고정 |
| DNS·TLS | 운영자가 선택한 호스트 `trading.example.com`의 실제 소유·라우팅·인증서 확인 |
| DB | 기존 MySQL에 Cotrader 전용 DB·사용자 |
| 런타임 DB 권한 | cotrader 스키마의 SELECT·INSERT·UPDATE·DELETE만 |
| 마이그레이션 | 분리된 DB 사용자로 cotrader 스키마 DDL; 변경 전 백업 |
| 외부 통신 | 고정 출구 IP를 토스 허용 목록에 등록; REST·WS 모두 확인 |
| 계좌 | 의도한 종합매매 계좌의 `accountSeq`를 확인하여 설정 |
| Telegram | 기존 webhook·다른 polling 소비자 없는 전용 봇, 본인 ID |
| 백업 | cotrader DB의 클러스터 밖 암호화 백업과 임시 DB 복원 검증 |
| 장애 감시 | 클러스터 밖에서 API readiness·engine heartbeat 감시 |

`deploy/k8s`는 Secret을 생성하지 않고 다음 이름을 참조한다.

- `cotrader-database`: `COTRADER_DATABASE_URL` — 런타임 전용 DB 연결.
- `cotrader-migration-database`: `COTRADER_DATABASE_URL` — 별도 DDL 연결.
- `cotrader-toss`: `TOSS_INVEST_OPEN_API_CLIENT_ID`, `TOSS_INVEST_OPEN_API_CLIENT_SECRET`, `COTRADER_ACCOUNT_SEQ`.
- `cotrader-upbit`: 선택 항목, `UPBIT_OPEN_API_ACCESS_KEY`, `UPBIT_OPEN_API_SECRET_KEY`. 실제 키가 필요한 계좌 조회를 사용할 때만 생성한다.
- `cotrader-identity`: `TELEGRAM_API_KEY`, `TELEGRAM_ME`, `COTRADER_SESSION_SECRET`, `COTRADER_GITHUB_CLIENT_ID`, `COTRADER_GITHUB_CLIENT_SECRET`, `COTRADER_GITHUB_USER_ID`.

원본 키는 사용자가 지정한 `~/.config/tossinvest/openapi.env`, `~/.config/tossinvest/telegram.env`다. 새 키로 대체하지 않는다. 파일의 실제 값은 대화·로그·Git·이미지에 넣지 않는다. 배포 전 소유자 전용 파일 권한을 확인한다. 세션 서명과 DB 비밀번호는 새 앱 전용 자격증명으로 관리하고 공용 root DB 비밀번호를 앱에 제공하지 않는다.

Toss·Upbit 키는 engine에만, Telegram 토큰과 세션 서명은 api에만 주입한다. research는 DB 연결만 사용한다. 업비트 공개 시세는 `COTRADER_UPBIT_ENABLED=true`, 계좌 조회는 `COTRADER_UPBIT_ACCOUNT_READS_ENABLED=true`를 추가로 설정한다. 기본 배포 예제는 두 값이 false다. 원화 모의 예산·손실 기준은 `COTRADER_CAPITAL_KRW`, `COTRADER_DAILY_LOSS_KRW`, `COTRADER_DRAWDOWN_KRW`로 달러 설정과 분리한다.

## 실행 순서

1. 빌드·Python 테스트·MySQL 통합 테스트·웹뷰 빌드를 통과시킨다. `kubectl kustomize deploy/k8s`로 출력물을 검토한다.
2. `main`에 반영된 커밋은 CI 테스트·양쪽 아키텍처 빌드를 통과한 후 GitHub Container Registry에 `sha-<commit>` 이미지로 게시된다. PR에서는 게시하지 않는다. 처음 생성된 GitHub 패키지는 private이므로 공개 소스만 포함함을 검증한 후 패키지를 public으로 설정하고 비인증 이미지 조회를 확인한다. CI 요약에 기록된 digest로 운영 이미지를 고정한다. 승인된 원격 저장소와 이미지 registry 경로를 확정하고 이미지를 게시한다. `REPLACE_WITH_VERIFIED_COMMIT` 두 곳을 실제 검증된 버전으로 바꾼다. 버전 문자열 그대로는 배포할 수 없다.
3. DB·Secret·DNS·백업 준비를 완료한다. Secret 동기화는 값이 출력되지 않는 승인된 운영 경로를 사용한다.
4. `deploy/migration-job.yaml`의 일회성 마이그레이션을 실행·확인한다. 진행 중인 engine과 동시에 스키마를 변경하지 않는다. 시장 분리 버전 `a781c092b5d3`은 기존 데이터를 Toss로 유지하면서 계좌 키·봉 고유 키를 변경하므로 API·engine·research를 모두 중지한 뒤 수행한다. 백업의 복원 검증을 먼저 마치고 새 이미지로 세 역할을 재개한다. 이 버전은 서로 다른 통화의 원장을 합치는 자동 다운그레이드를 제공하지 않는다.
5. `COTRADER_LIVE_ENABLED=false`를 확인하고 세 역할을 배포한다. API와 DB 연결, GitHub 본인 인증, 시세·캘린더·수집을 확인한다.
6. 여러 실제 거래일 동안 모의 전략을 운영한다. 휴장·세션 전환, 재시작, 네트워크 끊김, 부분 체결 모의, 전체 중단, 비용·원장 대조를 확인한다.
7. 실제 데이터 커버리지와 기업행사 영향을 검토하고 사용자가 실거래 종목·예산·위험 기준을 다시 승인할 때에만 별도 실거래 전환 작업을 한다.

이미지 빌드 예시:

```bash
docker build -t cotrader:local .
```

두 아키텍처의 로컬 이미지 검증:

```bash
docker buildx build --platform linux/arm64,linux/amd64 --tag cotrader:local --load .
```

브라우저용 정적 파일은 빌드 머신의 아키텍처에서 한 번 만들고, Python 실행 환경은 각 대상 아키텍처로 구성한다. [Docker 공식 다중 플랫폼 빌드 문서](https://docs.docker.com/build/building/multi-platform/)의 `BUILDPLATFORM` 방식을 사용한다.

DB 백업은 `mysqldump --single-transaction` 기반으로 cotrader 스키마를 매일 클러스터 밖에 보관한다. 최소 일별 7개·주별 4개를 유지하고 별도 임시 DB에 복원하여 원장 건수·누적 체결·마지막 주문 상태를 검사한다. 실제 저장 대상·암호화 키가 정해지기 전 백업 완료로 표시하지 않는다.

## GitHub Actions 배포

`main` 푸시가 CI를 통과하고 이미지가 게시되면 `deploy` job이 `vzyx-cluster` Environment의 승인을 기다린다. 운영자가 Approve해야 롤아웃이 시작된다. 승인은 배포 승인이며 실거래 시작 승인이 아니다.

이 job은 `cotrader-api`, `cotrader-research`, `cotrader-engine` 세 Deployment의 이미지를 CI가 게시한 digest로 교체하고 각각 `rollout status`를 확인한다. 순서는 api, research, engine이며 앞 단계가 준비되지 않으면 주문 실행기를 재시작하지 않는다.

ConfigMap, Ingress, NetworkPolicy, 실거래 플래그, Alembic 마이그레이션은 자동화하지 않는다. 운영 오버레이는 저장소 밖에 남기고 기존 승인된 수동 절차로만 변경한다.

### 승인 전 확인

- `/pause`로 봇 주문이 모두 종료 상태인지 확인한다. engine 재시작은 롤아웃 중에 일어난다.
- 스키마 변경이 포함된 커밋은 승인하지 않는다. 위 실행 순서 4번의 수동 마이그레이션을 먼저 끝낸 다음 승인한다.
- 운영 설정 변경이 필요한 커밋은 오버레이를 먼저 적용한 다음 승인한다.

### 배포 자격

`deploy/k8s/ci-deployer.yaml`의 `github-deployer` ServiceAccount는 cotrader 네임스페이스에서 Deployment의 `get`, `list`, `watch`, `patch`와 Pod 조회만 가진다. Secret 읽기 권한은 없고 다른 네임스페이스에도 접근하지 않는다. 저장소가 공개이므로 워크플로는 클러스터 주소와 해석된 IP를 `add-mask`로 가려 로그에 남기지 않는다.

### 최초 준비

1. 오버레이를 적용해 ServiceAccount, Role, RoleBinding, 토큰 Secret을 만든다. 토큰 값은 클러스터가 채우며 저장소에는 들어가지 않는다.
2. 승인자를 등록한 `vzyx-cluster` Environment를 만들고 배포 브랜치를 보호 브랜치로 제한한다.
3. 배포 전용 kubeconfig를 만들어 Environment secret `KUBECONFIG_B64`에 넣는다. 값은 출력하지 않는다.

```bash
umask 077
work=$(mktemp -d)
server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
kubectl -n cotrader get secret github-deployer-token -o jsonpath='{.data.ca\.crt}' | base64 -d > "$work/ca.crt"
kubectl config set-cluster vzyx --server="$server" --certificate-authority="$work/ca.crt" \
  --embed-certs=true --kubeconfig="$work/config"
kubectl config set-credentials github-deployer --kubeconfig="$work/config" \
  --token="$(kubectl -n cotrader get secret github-deployer-token -o jsonpath='{.data.token}' | base64 -d)"
kubectl config set-context default --cluster=vzyx --user=github-deployer --namespace=cotrader \
  --kubeconfig="$work/config"
kubectl config use-context default --kubeconfig="$work/config"

KUBECONFIG="$work/config" kubectl -n cotrader get deployment
KUBECONFIG="$work/config" kubectl -n cotrader get secret 2>&1 | tail -1

base64 < "$work/config" | gh secret set KUBECONFIG_B64 --env vzyx-cluster
rm -rf "$work"
```

Deployment 조회는 성공하고 Secret 조회는 거부되어야 한다. 토큰을 회전할 때는 `github-deployer-token` Secret을 지우고 다시 적용한 다음 같은 절차로 `KUBECONFIG_B64`를 갱신한다.

## 중단·복구

- 계획된 배포 전에 `/pause`를 실행하고 모든 봇 주문이 체결·취소 등 종료 상태인지 확인한다. 보유 주식은 유지한다.
- 주문 응답이 불명확하면 `UNKNOWN`을 다른 상태로 직접 DB 수정하지 않는다. 토스의 열린 주문·종료 주문과 종목·방향·수량·가격·시각을 대조한다.
- 웹뷰의 **주문 대조**에서 확인한 토스 주문 번호를 입력한다. `resolve` 명령은 `intent_id`와 확인한 `broker_id`를 받는다. 서버는 증권사 상세를 읽고 종목·방향·수량·가격이 맞고 다른 내부 주문과 연결되지 않았는지 확인해 원장을 반영한다. 주문 시각은 사용자가 함께 대조한다. 조회에서 보이지 않는다고 미제출로 가정하지 않는다.
- 정전이나 DB 장애 중에는 앱이 취소를 완료할 수 없다. 토스 앱에서 남은 주문을 확인할 수 있어야 한다.
- 복구 후 자동 재전송은 9분 이내의 같은 주문 의도에만 허용된다. 이미 중단한 전략이나 오래된 미확인 주문에는 재전송하지 않는다.
- 롤백은 먼저 주문을 중단한 상태에서 이전 이미지로 수행한다. 원장 DB를 과거 백업으로 되돌리면 실제 증권사 주문과 달라질 수 있으므로 자동 DB 다운그레이드나 무조건 복원을 하지 않는다.

통계는 시장별 USD·KRW 기준의 봇 운용 기록이다. 계좌 전체 입출금·배당·기업행사의 자동 대사는 포함하지 않는다. 수동 거래·입출금·분할 후에는 봇 원장과 계좌를 확인한 다음 재개한다. `/guide`와 정적 파일만 공개이며 두 시장의 계좌·주문·연구 API는 본인 인증이 필요하다.
