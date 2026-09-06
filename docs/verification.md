# 검증 방법과 한계

```bash
uv sync --frozen
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run pytest -q
bash scripts/test-mysql.sh -q
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
python3 scripts/check-public.py
```

자동 테스트는 가상 시세, 모의 증권사 응답과 임시 MySQL을 사용한다. 실제 계좌·키·보유 종목·개인의 운용 결과를 테스트 fixture나 공개 로그에 포함하지 않는다.

- 주문 중복·부분 체결·수수료 지연·응답 미확인·손실 중단·취소 후 늦은 체결 대조.
- 조회 전용 상태의 모든 비조회 요청 차단, 계좌 마스킹, 마지막 조회 시각 및 오류 표시.
- GitHub state·PKCE·일회성 요청·유효시간·실제 사용자 ID·권한 확인, 공급자별 세션 분리, 로그아웃·Origin·비인증 API 차단.
- Telegram 서명·유효시간·본인 개인 대화방 제한과 계좌 조회의 주문 비생성.
- 최적화 기법별 재현성, 고정 손실 한도, 마지막 구간을 탐색에 사용하지 않음, 비교 모드도 최종 확인은 한 번만 실행.
- 조회 실패나 빈 값이 확정된 잔액 0으로 처리되지 않음.

CI의 실제 통과 여부는 해당 커밋의 GitHub Actions 결과로 확인한다. 테스트·이미지 빌드 통과는 실제 계좌 수익성, 운영 배포, 시장별 주문 수락·체결의 증거가 아니다. 실제 로그인·시세·주문 검증 기록은 운영자가 비공개로 보관한다.

백테스트는 1분봉의 가격 순서, 호가 대기열, 기업행사, 환율과 세금을 모두 재현하지 못한다. 모의 데이터의 높은 수익률은 투자 성과 근거가 아니다. 자세한 가정은 [최적화 문서](optimization.md)를 참고한다.
