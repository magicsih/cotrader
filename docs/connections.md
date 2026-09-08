# 텔레그램과 실제 계좌 조회

로컬에서 텔레그램 명령 송수신과 토스 실제 계좌 조회를 사용한다. 클러스터 배포와 주문 활성화는 별도다. GitHub 로그인은 [인증 설정](github-auth.md)을 참고한다.

## 실행

MySQL과 마이그레이션이 준비된 상태에서 각각 실행한다.

```bash
uv run cotrader api --connect
uv run cotrader engine --connect
uv run cotrader research
```

`--connect`는 실거래 실행을 강제로 비활성화한다. api는 `~/.config/tossinvest/telegram.env`, engine은 `~/.config/tossinvest/openapi.env`만 읽는다. 셸로 source하거나 저장소에 복사하지 않는다. 연구 프로세스는 두 파일 모두 읽지 않는다. 환경변수로 직접 주입할 때는 `COTRADER_TELEGRAM_ENABLED=true`, `COTRADER_ACCOUNT_READS_ENABLED=true`를 사용하고 `COTRADER_LIVE_ENABLED=false`를 유지한다.

서버의 `live_enabled=false`에서는 증권사 요청 전송 계층에서 모든 GET 이외의 요청을 거부한다. 예외는 인증 전용 토큰 발급 경로다. 따라서 주문 생성·정정·취소·조건주문 삭제가 차단된다. 이는 애플리케이션의 조회 전용 모드이며, 토스에서 별도의 읽기 전용 권한을 가진 키를 발급받았다는 의미가 아니다.

## 토스 조회

토큰 발급과 갱신은 MySQL 단일 실행 잠금을 가진 engine만 담당한다. 토큰을 메모리에서 재사용한다. 토스는 새 토큰 발급 시 이전 토큰을 무효화하므로 별도의 진단 프로세스가 토큰을 반복 발급하지 않는다.

종합매매 계좌가 하나면 해당 계좌를 선택한다. 여러 개면 추측하지 않고 `COTRADER_ACCOUNT_SEQ`를 요구한다. 약 60초마다 계좌 목록, USD·KRW 현금 매수 가능 금액, 보유 주식, 미체결 주문, 미국 수수료율을 조회한다. 계좌번호는 끝 4자리만 저장하고 토큰은 DB에 저장하지 않는다.

결과는 웹의 **포트폴리오 → 토스 실제 계좌** 및 Telegram `/toss` 또는 `/account`에서 확인한다. 계좌 전체 현황과 봇의 전략별 운용 손익을 구분한다. 실패 시 이전 성공 결과와 오류 상태를 함께 표시하며, 마지막 성공 조회 이후 120초가 지나면 갱신 필요로 표시한다. 확인 불가 값을 0으로 바꾸지 않는다.

[토스 공식 REST 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)의 `GET /api/v1/buying-power`는 미수거래를 제외한 현금 기반 매수 가능 금액을 반환한다. $5,000 운용 예산은 계좌 현금에 대한 내부 배정 한도다. 별도로 봇에 송금하지 않으며, 실매수 전 `USD cashBuyingPower`와 수수료 여유를 확인한다. 원화 총자산을 달러 현금으로 간주하거나 자동 환전하지 않는다. 기존 계좌 보유분은 봇의 소유분으로 자동 편입하지 않는다.

## Telegram

- 지정한 사용자 ID와 같은 ID의 **개인 대화방**만 허용한다.
- `getMe`, `getChat`, `getWebhookInfo`로 연결 대상을 확인하고 그 대화방에 명령 메뉴를 등록한다.
- 기존 웹훅이 있으면 자동 삭제하지 않는다. MySQL 잠금으로 이 앱의 중복 수신기를 차단한다.
- `paper_lab_enabled=true`이면 명령 메뉴와 첫 화면을 **모의 전용**으로 바꾼다. `/paper`는 8개 독립 가상 계좌의 판단·순자산·수익률·최대 하락폭·평가 진행 상황을 비교한다.
- `/paper_data`는 한 가상 계좌씩 최근 행동·대기 사유·신호일·평가 시각·다음 평가 시각과 지표 값·평균·목표·진입·청산 조건 통과 여부를 보여준다. 출처는 `paper_portfolios`와 `paper_events`이며 실제 계좌 잔고나 실거래 원장을 포함하지 않는다.
- `/pocket`은 연결된 실제 업비트 포켓의 자산별 가용·주문 중·합계 수량, `/toss`는 실제 토스 계좌의 USD·KRW 현금 매수 가능 금액과 보유 종목을 **조회 전용** 화면으로 보여준다.
- 모의 전용 실행에서는 `/orders`, `/profit`, `/strategies`, `/status`, `/pause`를 메뉴에 등록하지 않으며 과거 명령이나 버튼으로 호출해도 DB 명령을 만들지 않는다. 모의 운용을 끈 실거래 프로필에서만 기존 운영 명령을 제공한다.
- `/research`는 최근 토스 전략 발굴 진행·마지막 검증 결과·보류 사유를 보여준다. 발굴 완료를 전략 승인이나 실거래 시작으로 표시하지 않는다.
- `/guide`, `/web`도 제공한다.
- Bot API 10.3의 `sendRichMessage`와 `editMessageText.rich_message`를 사용한다. 제목·간격이 작은 표·접기·각주와 인라인 키보드를 사용하며, 6개씩 페이지를 이동한다. 새로고침·목록 이동은 같은 메시지를 수정한다. 인증이 Telegram인 경우에만 Mini App 버튼을 사용한다.
- 첫 연결 이전의 알림은 보내지 않는다. 재시작 이전 승인 버튼은 설정 재확인을 요구한다. 읽기 전용 탐색 버튼은 재시작 후에도 동작하며, 새로 수정한 상세 화면의 승인 버튼은 수정 시각을 기준으로 확인한다.
- 계좌 화면은 engine이 저장한 마지막 성공 조회만 읽고 매매 명령을 만들지 않는다. 새로고침은 거래소를 즉시 다시 호출하지 않으며, 조회 시각과 약 60초 자동 갱신 주기를 안내한다. 조회 실패·120초 초과 데이터는 마지막 조회 값으로 표시한다.
- 송신 영수증에는 메시지 ID만 저장한다. 자격증명과 메시지 본문을 로그에 남기지 않는다.

현재는 등록된 Cotrader 봇 하나를 모의·조회 전용으로 사용한다. 실거래를 다시 활성화할 때는 별도 자격증명과 소비자를 가진 실거래 제어 봇으로 분리한다. 퇴역한 `nats-handler` Telegram relay의 토큰은 재사용하지 않는다.

기본 웹은 loopback의 로컬 인증 모드다. 운영 웹은 HTTPS 주소에서 GitHub 또는 Telegram 인증을 설정한다. GitHub 모드의 `/web`은 일반 HTTPS 링크를 보내므로 브라우저에서 GitHub 로그인을 진행한다. Telegram 모드는 Mini App 버튼을 사용한다.

참고: [Telegram Bot API](https://core.telegram.org/bots/api).

## 업비트

`COTRADER_UPBIT_ENABLED=true`를 세 역할에 설정하면 원화·USDT 코인 시세 수집·연구·모의매매를 사용할 수 있다. 계좌 조회는 engine에 `COTRADER_UPBIT_ACCOUNT_READS_ENABLED=true`, `UPBIT_OPEN_API_ACCESS_KEY`, `UPBIT_OPEN_API_SECRET_KEY`를 추가로 주입한다. 로컬 `--connect`는 기존 토스·텔레그램 전용이므로 업비트 설정은 역할별 환경변수 또는 Kubernetes Secret을 사용한다. 키 파일을 저장소나 이미지에 복사하지 않는다.

코트레이더 포켓을 사용하는 운영 환경은 로컬 키 파일의 `UPBIT_OPEN_API_COTRADER_ACCESS_KEY`와 `UPBIT_OPEN_API_COTRADER_SECRET_KEY`를 engine의 `UPBIT_OPEN_API_ACCESS_KEY`와 `UPBIT_OPEN_API_SECRET_KEY`에 각각 주입한다. 원본 메인 포켓 키나 포켓 관리 키를 주입하지 않는다. 이 매핑이 실제 포켓 키와 일치하는지 값을 노출하지 않고 비교한 후 `COTRADER_UPBIT_ACCOUNT_LABEL=코트레이더 포켓`을 설정한다. 표시 이름은 키를 선택하지 않는다. engine이 실제 응답과 함께 저장한 이름을 Telegram이 표시하며, 이름이 없는 과거 응답은 ‘업비트 연결 계좌’로 표시한다.

공개 시세는 키 없이 조회한다. 비공개 계좌·주문 조회는 HS512 JWT로 서명한다. `COTRADER_UPBIT_LIVE_ENABLED=false`와 `COTRADER_UPBIT_USDT_LIVE_ENABLED=false`에서는 실제 주문·취소를 거절하며, 준비 점검의 `/v1/orders/test`는 실제 주문 없이 사용할 수 있다. 주문·취소 허용은 서버 설정과 API 키 자체의 권한이 모두 필요하다. 출금 경로는 제공하지 않는다. 계좌 값은 **시장 선택 → 업비트 코인 → 포트폴리오**와 Telegram `/pocket` 또는 `/crypto`에서 조회하며 로그인과 본인 대화방 제한이 적용된다.

`KRW-BTC`, `KRW-ETH`, `USDT-SOL` 같은 원화·USDT 거래쌍을 지원한다. 시세는 거래소의 1분봉을 페이지별로 수집하고, 거래가 없는 분을 채우지 않는다. 418·429 응답은 최소 60초 대기하며 즉시 반복 호출하지 않는다. 모의 잔액·예산은 실제 계좌 잔액과 별도이고, 운용 원장은 KRW·USD·USDT별로 분리한다. Telegram `/profit`과 웹 포트폴리오의 [운용 수익](profit.md)은 현재 참고 환율로 원화 합산을 표시하며 실제 환전은 하지 않는다. 코인의 하루 손실 기준은 UTC 날짜로 갱신한다.

공식 근거: [인증](https://docs.upbit.com/kr/reference/auth), [분봉](https://docs.upbit.com/kr/reference/list-candles-minutes), [원화 거래 단위](https://docs.upbit.com/kr/docs/krw-market-info).


실거래는 [실거래 준비와 운영 절차](live-trading.md)를 따릅니다. 로컬 `--connect`는 두 시장의 주문을 항상 잠급니다.
