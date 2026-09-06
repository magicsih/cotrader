# 토스증권 연동 계약

2026-09-06 확인. REST 명세 1.2.14, WebSocket 명세 1.2.2를 기준으로 구현했다. 검색 캐시에 REST 전용이라는 예전 설명이 있었으므로 아래 서버 원본을 정본으로 사용한다.

- [공식 안내](https://developers.tossinvest.com/llms.txt)
- [REST 원본](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [WebSocket 원본](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)
- [연동 개요](https://openapi.tossinvest.com/openapi-docs/overview.md)

| 계약 | 구현 결정 |
|---|---|
| 새 OAuth 토큰 발급은 이전 토큰을 무효화 | engine만 발급·갱신. 연구·웹 프로세스는 증권사 키를 보유하지 않음 |
| REST·WebSocket 모두 허용 IP 필요 | 운영 서버 출구 IP 등록을 배포 전 확인 |
| 미국 지정가는 정수 수량 | 가격선별 최소 1주 검증; 소수점 주문 제외 |
| 동일 종목 반대 방향 미체결 오류 | 종목별 주문 하나를 직렬 처리 |
| clientOrderId 유효 10분 | 내부 최초 제출 시각과 동일 키 저장; 9분 넘으면 자동 재전송 금지 |
| 주문 목록·상세 스키마에 clientOrderId 없음 | 오래된 미확인 주문을 목록에서 키로 찾을 수 있다고 가정하지 않음 |
| DAY는 미체결분 자동 취소 가능 | 다음 세션까지 주문 유지 가정 금지; 상태 대조 후 새 신호 판단 |
| 미국 주문 수정은 수량 변경 불가 | 초기 구현은 정정 대신 기존 주문 취소 확인 후 새 판단 |
| 미체결 조회 OPEN은 전량 반환 | 커서로 잘못 반복하지 않음; CLOSED는 커서 사용 |
| 수수료·세금은 지연 확정 가능 | 추정 비용과 확정 비용 구별·차액 반영 |
| 캔들 1m·1d, 최대 200개, nextBefore | 중복·진행 정지 감지, 실제 수집 범위 기록 |
| WebSocket 2연결·100구독 | engine의 1연결 사용; 최대 5종목으로 제한 |
| WebSocket 초기 시세 미전송 | REST 호가로 상태 복구 |
| WebSocket 단절 구간 주문 이벤트 재전송 없음 | REST 주문 상세 재대조 |
| 동적 API 그룹별 호출 한도 | 응답 한도의 80%로 속도를 낮추고 GET 429는 Retry-After 준수 |

실제 인증·계좌·주문 검증 결과는 운영자의 비공개 기록으로 보관한다. 이 공개 저장소는 특정 계좌의 상태나 수익성을 보증하지 않는다. [조회 연결](connections.md)을 참고한다.
