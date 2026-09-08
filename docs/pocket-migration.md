# 업비트 포켓 이전 — 운영자 실행 절차

이 도구는 메인 포켓에서 지정한 BTC 수량을 남기고 다른 잔고를 코트레이더 키 소속 포켓으로 이전한다. 포켓과 API 키 소속은 매번 업비트에서 확인한다. 실제 수량·주문 ID·계좌 정보는 `private/`에만 저장한다.

**주문은 복제하지 않는다.** 기존 봇에서 전략 중단과 주문 대조를 완료한 뒤 포켓을 전환하고 같은 전략을 재개한다. 주문을 별도로 복제하면 기존 주문 ID와 장부가 달라지고 중복 주문·보유 불일치가 발생할 수 있다.

이 도구는 운영자가 직접 실행한다. `inspect`, `plan`, `verify`, `reactivate`는 업비트에 GET만 보낸다. `apply`만 포켓 자산 이전 POST를 보낸다. 주문 생성·취소·출금 기능은 없다. `reactivate`는 이전 완료를 다시 검증한 뒤 운용 종료된 전략 하나를 `PAUSED`로 복원하지만 주문을 만들거나 위험 기준점을 바꾸지 않는다. `verify --engine-env`는 검증 성공 후에만 로컬 키 파일을 새로 만든다.

## 준비

```bash
uv sync --frozen
uv run python -m cotrader.pockets inspect
```

기본 키 원본은 `~/.config/upbit/upbit-api-key.env`이며 다음 이름을 사용한다. 셸에서 source하지 않는다.

- 메인 조회: `UPBIT_OPEN_API_ACCESS_KEY`, `UPBIT_OPEN_API_SECRET_KEY`
- 메인 포켓 관리: `UPBIT_OPEN_API_POCKET_ADMIN_ACCESS_KEY`, `UPBIT_OPEN_API_POCKET_ADMIN_SECRET_KEY`
- 도착 포켓: `UPBIT_OPEN_API_COTRADER_ACCESS_KEY`, `UPBIT_OPEN_API_COTRADER_SECRET_KEY`

파일은 본인 소유 일반 파일·600 권한이어야 한다. 다른 위치라면 하위 명령 앞에 `--key-file`을 지정한다. 기존 메인 조회 키에 포켓관리 권한을 추가할 필요는 없다. 관리자 키는 이 도구에만 제공하며 엔진 키 파일에는 들어가지 않는다.

DB 설정 파일은 `COTRADER_DATABASE_URL` 하나를 포함하는 운영 MySQL 설정이다. CLI 인수로 비밀번호를 전달하지 않는다. `--expected-database`는 확인한 실제 DB 이름을 명시한다. 로컬 포트 전달을 쓸 때만 `--db-host 127.0.0.1 --db-port PORT`로 접속 주소를 바꾼다. 실제 DB 이름과 MySQL 서버 UUID를 계획에 기록하고 실행 때 다시 비교한다.

## 기존 주문 종료와 엔진 정지

1. 웹에서 **기존 업비트 실거래 전략을 중단**한다. 다른 시장을 중단할 필요가 없다면 전체 중단 대신 전략별 중단을 사용한다.
2. 기존 메인 키를 사용하는 엔진이 취소와 마지막 체결을 대조할 시간을 준다. 거래소 주문 0건뿐 아니라 장부의 `PREPARED`, `SENDING`, `UNKNOWN`, `PENDING`, `PARTIAL_FILLED`, `PENDING_CANCEL`도 모두 없어야 한다. DB 상태를 직접 바꿔 통과시키지 않는다.
3. 명령 처리가 끝나면 운영 엔진을 정지하고, 종료 중인 Pod도 없어졌는지 확인한다. 이후 키 전환 검증까지 시작·재개 명령을 보내지 않는다. 엔진을 공유하는 다른 시장의 주문 대조도 이 시간에는 멈추므로 해당 운용 상태를 먼저 확인한다.
4. 작업 도중 다른 프로그램이나 수동 조작으로 두 포켓을 거래·이전하지 않는다. 도구는 매 전송 전 잔고와 주문을 다시 읽지만 다른 거래 주체를 잠글 수는 없다.

`plan`, `apply`, `verify`는 기존 엔진과 같은 MySQL 실행 잠금을 획득한다. 엔진이 실행 중이거나 장부 대조가 끝나지 않았으면 실패한다. 잠금 우회 옵션은 없다. 작업 도중 잠금을 잃으면 다음 자산을 보내지 않는다.

## 이전 계획 검토

아래 변수는 확인한 운영값으로 설정한다. `POCKET_RESERVE_BTC`에는 메인에 남길 정확한 수량을 넣는다. 예제에 실제 계좌 수량을 고정하지 않는다.

```bash
uv run python -m cotrader.pockets plan \
  --database-env-file "$POCKET_DATABASE_ENV" \
  --expected-database "$POCKET_DATABASE_NAME" \
  --reserve-btc "$POCKET_RESERVE_BTC" \
  --output private/pocket-plan.json
```

계획에는 출발·도착 포켓, 실제 가용 잔고, BTC 보존량, 통화별 이전 수량, 전략·보유 장부 사본, 자산별 고유 식별자가 들어간다. 표준 출력의 `sha256`은 검토한 계획 확인값이다.

- 주문 또는 잠긴 잔고가 있으면 계획을 만들지 않는다.
- 첫 계획은 비어 있는 도착 포켓만 허용한다. 기존 잔고를 임의로 봇 장부에 편입하지 않는다.
- 봇이 보유 중인 코인은 보존량을 제외한 이전 수량과 장부 수량이 같아야 한다.
- 소액 KRW와 미세 잔고도 버리거나 반올림하지 않는다. 지원 여부는 실제 API 처리 결과로 판단하며, 거절된 자산을 임의로 제외하지 않는다.
- 기존 파일을 덮어쓰지 않는다. 실행 도중인 계획과 `.journal`은 삭제하거나 편집하지 않는다.

### 운용 종료된 전략 하나만 선택 재개

메인 포켓으로 원상 반환했던 USDT 전략 하나를 재개할 때는 전략 ID를 지정한다. 이 모드는 전략 종목의 보유 자산과 결제 자산 USDT만 자동 선택하며 `--reserve-btc`나 `--exclude-currency`를 받지 않는다.

```bash
uv run python -m cotrader.pockets plan \
  --key-file "$POCKET_OPERATOR_KEY_FILE" \
  --database-env-file "$POCKET_DATABASE_ENV" \
  --expected-database "$POCKET_DATABASE_NAME" \
  --reactivate-strategy-id "$POCKET_STRATEGY_ID" \
  --output private/reactivation-plan.json
```

계획 생성은 다음 조건을 모두 확인한다.

- 대상은 `ARCHIVED`인 업비트 USDT 실거래 전략 하나이며, 종료 이벤트의 원본 상태와 해시가 현재 장부와 일치한다.
- 코트레이더 포켓은 비어 있고 양쪽 포켓의 주문과 잠긴 잔고가 없다.
- 메인의 종목 수량은 종료 직전 전략 수량과 정확히 같고, USDT는 종료 직전 전략 현금 이상이다.
- 계획의 `items`는 해당 종목과 USDT 두 자산뿐이다. 메인 BTC·KRW 및 다른 자산은 이전하지 않는다.

## 운영자가 이전 실행

**다음 명령은 실제 자산을 이전한다.** 계획의 포켓·수량을 검토한 뒤 그 계획의 SHA256을 직접 전달한다. 실행 때 장부와 잔고가 달라졌다면 이전하지 않는다.

```bash
uv run python -m cotrader.pockets apply \
  --database-env-file "$POCKET_DATABASE_ENV" \
  --expected-database "$POCKET_DATABASE_NAME" \
  --plan private/pocket-plan.json \
  --confirm 검토한_계획의_SHA256
```

각 요청 전에 전송 시도를 `private/pocket-plan.json.journal`에 기록하고 디스크에 동기화한다. 자산별 `identifier`는 계획 생성 시 고정한다. 이전 내역의 출발·도착·통화·수량·식별자가 모두 일치하고 `done`인 경우만 완료로 인정한다. 이어서 양쪽 잔고 증감을 정확히 대조하고 다음 자산으로 진행한다.

프로그램 종료나 API 지연이 발생하면 **같은 계획과 같은 실행 기록을 유지한 채 같은 명령을 다시 실행**한다. 이미 완료된 자산은 다시 전송하지 않는다. `submitted`·`processing`이면 완료를 기다린 후 다시 확인한다. 전송 시도는 있는데 이전 내역이 없거나 `failed`이면 자동 재전송하지 않는다. 원인을 확인하기 전 새 계획·식별자를 만들지 않는다. 파일 쓰기 또는 JSON 손상 오류도 실행을 중단한다.

전송은 전체 통화에 대한 원자적 작업이 아니다. 일부만 완료될 수 있으며 자동 되돌리기를 하지 않는다. 계획 생성 후 24시간이 지나면 새 전송을 거절한다. 이전 내역은 계획 생성 시각부터 최대 7일 범위로 조회한다. 장기 장애, 미지원 통화, 확인되지 않는 응답은 거래소 내역을 확인해 별도로 복구해야 한다.

## 완료 확인과 연결 키 준비

엔진을 계속 정지한 상태에서 실행한다.

```bash
uv run python -m cotrader.pockets verify \
  --database-env-file "$POCKET_DATABASE_ENV" \
  --expected-database "$POCKET_DATABASE_NAME" \
  --plan private/pocket-plan.json \
  --engine-env private/cotrader-pocket-engine.env
```

모든 이전의 `done`, 양쪽 전체 잔고, 메인 BTC 보존량, 미체결·잠긴 잔고 0, 기존 전략·장부 일치를 확인해야 `verified: true`를 출력한다. 자산별 잔고가 확인되지 않으면 0으로 처리하지 않는다.

출력한 env 파일은 600 권한이며, 기존 엔진이 읽는 `UPBIT_OPEN_API_ACCESS_KEY`, `UPBIT_OPEN_API_SECRET_KEY` 이름에 **코트레이더 포켓 키만** 담는다. 파일 내용은 터미널에 출력하지 않는다. 관리자·메인 키와 원본 파일·카탈로그는 수정하지 않는다. Kubernetes Secret 갱신은 등록된 자격증명 백업·복원 검사와 기존 consumer 확인 후 운영자가 수행한다.

## 기존 전략 재개

선택 재개 계획에서는 이전과 키 검증을 완료한 후 같은 계획 해시로 장부 상태를 복원한다.

```bash
uv run python -m cotrader.pockets reactivate \
  --key-file "$POCKET_OPERATOR_KEY_FILE" \
  --database-env-file "$POCKET_DATABASE_ENV" \
  --expected-database "$POCKET_DATABASE_NAME" \
  --plan private/reactivation-plan.json \
  --confirm 검토한_계획의_SHA256
```

`reactivate`는 종료 이벤트에 저장된 동일 전략 상태를 복원하고 `PAUSED`로 둔다. 계좌의 `daily_anchor`와 `high_water`는 변경하지 않는다. 계좌가 기존 평가금액 하락 중단 상태가 아니거나 다른 업비트 실거래 전략에 자금이 배정되어 있으면 중단한다. 이후 자동 복구는 최신 평가가 설정 한도의 안전 구간에 60초 이상 머물고 계좌·주문·수수료·호가·시험 주문 점검을 통과할 때만 이 전략을 `RUNNING`으로 바꾼다.

1. 엔진의 키 주입 위치를 코트레이더 키로 갱신하고, 저장한 실행 사본이 원본 코트레이더 키와 일치하는지 값을 출력하지 않고 대조한다. 기존 이미지·전략·장부·위험 기준을 유지한다. 이 도구를 엔진 이미지에 배포할 필요는 없다.
2. 엔진을 다시 시작한다. 기존 업비트 전략은 PAUSED 상태로 남는다. 실제 계좌 조회가 새 포켓을 표시하고, 자산 수량이 이전 검증 결과와 일치하는지 확인한다.
3. 웹에서 **기존 전략**의 설정·시작 화면을 연다. 새 전략을 만들거나 옛 주문 UUID를 복사하지 않는다. 시작 전 기존 계좌 수수료·시장 상태·보유 수량·현금·위험 기준을 다시 점검한다.
4. 운영자가 최종 시작을 확인한다. 기존 단계별 코인·현금과 체결 장부를 바탕으로 주문을 이어간다. 이전 직전의 주문을 그대로 복제하는 것이 아니므로 새 주문 UUID가 생기고, 가격 변화에 따라 메이커 주문은 보류될 수 있다.
5. 코트레이더 포켓의 실제 주문과 장부가 일치하고 메인에 새 주문이 없는지 확인한다. 불일치·위험 중단은 자동 해제하지 않는다.

## 검증 및 공식 근거

가짜 HTTP 응답과 격리 DB를 이용해 BTC 보존, 미세 잔고, 키 역할, 잠금·주문·장부 차단, 중복 실행, 통신 단절, 처리 중 이전, 재실행, 다른 포켓·수량 응답, 파일 권한을 검사한다. 실제 자산 이전 테스트를 대신하지 않는다.

- [메인포켓 자산 이전](https://docs.upbit.com/kr/reference/universal-transfer)
- [이전 내역 조회와 조회 기간](https://docs.upbit.com/kr/reference/list-universal-transfers)
- [포켓별 API Key 조회](https://docs.upbit.com/kr/reference/list-pocket-api-keys)
- [포켓 잔고 조회](https://docs.upbit.com/kr/reference/get-balance)
- [기존 보유 반복 그리드](usdt-grid.md)
