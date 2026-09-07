"""Telegram rich messages: explicit units, bounded tables and read-only navigation."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from cotrader.domain import StrategySpec, grid_quantity, levels
from cotrader.markets import UPBIT_CAUTION_LABELS, currency, is_upbit
from cotrader.services import approval_digest

PAGE_SIZE = 6
COMMANDS = (
    ("menu", "계좌·주문·전략 메뉴"),
    ("pocket", "코트레이더 포켓 잔고"),
    ("toss", "토스증권 잔고와 보유 종목"),
    ("orders", "봇의 미체결·확인 필요 주문"),
    ("profit", "마켓별·전체 합산 운용 수익"),
    ("strategies", "전략 상세·시작·중단"),
    ("research", "토스증권 전략 발굴 결과"),
    ("status", "실행 상태와 운용 손익"),
    ("pause", "전체 전략 중단 요청 — 보유 유지"),
    ("web", "Cotrader 웹 화면"),
    ("guide", "사용 가이드"),
    ("help", "명령어와 사용 방법"),
)
STATES = {
    "RUNNING": "운영 중",
    "WAITING": "대기",
    "DRAFT": "시작 전",
    "PAUSED": "중단",
    "ARCHIVED": "보관",
    "CONNECTED": "연결됨",
    "DISCONNECTED": "연결 대기",
    "ERROR": "확인 필요",
    "PREPARED": "전송 준비",
    "SENDING": "접수 확인 중",
    "PENDING": "미체결",
    "UNKNOWN": "주문 대조 필요",
    "PENDING_CANCEL": "취소 확인 중",
    "PARTIAL_FILLED": "일부 체결",
    "CANCEL_UNKNOWN": "취소 대조 필요",
    "COLLECTING": "시세 수집 중",
    "SUCCEEDED": "완료",
    "FAILED": "실패",
    "CANCELED": "취소",
}


def number(value, places=8):
    if value is None:
        return "확인 불가"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "확인 불가"
        result = f"{amount:,.{places}f}"
        return result.rstrip("0").rstrip(".") if "." in result else result
    except (InvalidOperation, ValueError):
        return "확인 불가"


def when(value):
    if not value:
        return "확인 불가"
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (
        at.replace(tzinfo=at.tzinfo or UTC).astimezone(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M:%S KST")
    )


def paragraph(text):
    return {"type": "paragraph", "text": text}


def heading(text):
    return {"type": "heading", "size": 3, "text": text}


def footer(text):
    return {"type": "footer", "text": text}


def table(headers, rows):
    return {
        "type": "table",
        "is_compact": True,
        "is_striped": True,
        "cells": [[{"text": h, "is_header": True, "align": "left", "valign": "middle"} for h in headers]]
        + [
            [
                {"text": str(v), "align": "left" if i == 0 else "right", "valign": "middle"}
                for i, v in enumerate(row)
            ]
            for row in rows
        ],
    }


def button(text, data):
    return {"text": text, "callback_data": data}


def nav(screen, page=0, total=0):
    pages = []
    if page:
        pages.append(button("‹ 이전", f"nav:{screen}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        pages.append(button("다음 ›", f"nav:{screen}:{page + 1}"))
    return ([pages] if pages else []) + [
        [button("↻ 새로고침", f"nav:{screen}:{page}"), button("⌂ 메뉴", "nav:menu:0")]
    ]


def page_of(rows, page):
    page = min(page, max(0, (len(rows) - 1) // PAGE_SIZE))
    return rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], page


def menu():
    return [
        heading("Cotrader"),
        paragraph("계좌 현황부터 전략 운영까지, 아래 버튼으로 확인하세요."),
        footer("잔고는 거래소 조회 값입니다. 전략의 배정 예산·운용 손익과 구분해 표시합니다."),
    ], [
        [button("◈ 코트레이더 포켓", "nav:pocket:0"), button("토스증권", "nav:toss:0")],
        [button("미체결 주문", "nav:orders:0"), button("전략 관리", "nav:strategies:0")],
        [button("운용 수익 · 전체 합산", "nav:profit:0")],
        [button("토스 전략 발굴", "nav:research:0"), button("운영 상태", "nav:status:0")],
        [button("웹 화면", "nav:web:0"), button("사용 방법", "nav:help:0")],
    ]


def account_header(data, title):
    blocks = [heading(title)]
    snapshot = data.get("snapshot")
    if data.get("status") != "CONNECTED" or data.get("stale"):
        blocks.append(
            paragraph(
                f"⚠ 조회 확인 필요 · {data.get('error') or STATES.get(data.get('status'), '연결 대기')}\n아래 값은 마지막 조회 값입니다."
            )
        )
    if not snapshot:
        blocks.append(paragraph("아직 확인된 계좌 데이터가 없습니다."))
    return blocks


def pocket(data, page=0):
    snapshot = data.get("snapshot") or {}
    blocks = account_header(data, snapshot.get("account_label") or "업비트 연결 계좌")
    balances = snapshot.get("balances")
    if snapshot and balances is None:
        blocks.append(paragraph("코인별 잔고를 확인할 수 없습니다. 다음 계좌 조회를 기다려 주세요."))
    if balances is not None:
        priority = {"BTC": 0, "SOL": 1, "USDT": 2, "KRW": 3}
        rows = sorted(balances, key=lambda r: (priority.get(r["currency"], 4), r["currency"]))
        visible, page = page_of(rows, page)
        blocks.append(
            table(
                ["자산", "가용", "주문 중", "합계"],
                [
                    [
                        r["currency"],
                        number(r["balance"]),
                        number(r["locked"]),
                        number(Decimal(r["balance"]) + Decimal(r["locked"])),
                    ]
                    for r in visible
                ],
            )
        )
        blocks.append(
            footer(
                f"{len(rows)}종 · {page + 1}쪽 · 수량 기준\n조회 {when(snapshot.get('checked_at'))} · 자동 갱신 약 60초"
            )
        )
    blocks.append(
        paragraph("이 계좌에 연결된 포켓의 잔고입니다. 전략별 예산과 손익은 ‘전략 관리’에서 확인하세요.")
    )
    return blocks, nav("pocket", page, len(balances or [])) + [
        [button("미체결 주문", "nav:orders:0"), button("전략 관리", "nav:strategies:0")]
    ]


def toss(data, page=0):
    snapshot = data.get("snapshot") or {}
    blocks = account_header(data, "토스증권 · 실제 계좌")
    holdings = snapshot.get("holdings", {}).get("items", [])
    if snapshot:
        cash = snapshot.get("cash_buying_power", {})
        blocks += [
            paragraph(f"계좌 {snapshot.get('account_mask', '확인 불가')}"),
            table(["현금 매수 가능", "금액"], [[unit, number(cash.get(unit), 2)] for unit in ("USD", "KRW")]),
        ]
        visible, page = page_of(holdings, page)
        if visible:
            blocks += [
                heading(f"보유 종목 · {len(holdings)}개"),
                table(
                    ["종목", "수량", "평가액"],
                    [
                        [
                            r["symbol"],
                            number(r.get("quantity")),
                            f"{number(r.get('marketValue', {}).get('amount'), 2)} {r.get('currency', '')}",
                        ]
                        for r in visible
                    ],
                ),
            ]
        else:
            blocks.append(paragraph("보유 종목 없음"))
        rate = snapshot.get("us_commission_rate")
        blocks.append(
            footer(
                f"증권사 미체결 {len(snapshot.get('open_orders', []))}건 · 미국 수수료 {number(Decimal(rate) * 100, 4) if rate is not None else '확인 불가'}%\n조회 {when(snapshot.get('checked_at'))} · 자동 갱신 약 60초"
            )
        )
    return blocks, nav("toss", page, len(holdings)) + [[button("토스 전략 발굴", "nav:research:0")]]


def status(engine, upbit, portfolios):
    blocks = [heading("운영 상태")]
    for label, row in (("토스증권", engine), ("업비트", upbit)):
        value = row.data if row else {}
        blocks.append(
            paragraph(
                f"{label} · {STATES.get(value.get('status'), value.get('status', '연결 대기'))}\n{value.get('reason') or '상태 확인'}\n응답 {when(row.updated_at) if row else '확인 불가'}"
            )
        )
    for row in portfolios:
        if row:
            p = row.data
            label = {"toss": "토스", "upbit": "업비트 KRW", "upbit_usdt": "업비트 USDT"}[p["venue"]]
            blocks += [
                heading(f"{label} · {'실거래' if p['mode'] == 'live' else '모의'}"),
                table(
                    ["항목", p["currency"]],
                    [
                        ["평가액", number(p.get("equity"), 2)],
                        [
                            "운용 손익",
                            number(Decimal(p["equity"]) - Decimal(p["capital"]), 2)
                            if p.get("equity") is not None
                            else "확인 불가",
                        ],
                        ["누적 비용", number(p.get("costs"), 2)],
                    ],
                ),
                paragraph(
                    f"미체결 {p.get('pending_orders', 0)}건 · {'평가금액 하락 기준으로 중단' if p.get('halted') else '운용 가능'}"
                ),
            ]
    return blocks, [[button("마켓별·합산 운용 수익", "nav:profit:0")]] + nav("status")


def profit(report):
    unit = report["base_currency"]
    total = report["total"]
    blocks = [
        heading(f"운용 수익 · {'실거래' if report['mode'] == 'live' else '모의'}"),
        paragraph("봇 운용 시작 이후 누적 손익 · 보유 편입 당시 평가 기준가부터 집계"),
        heading(f"전체 합산 · {unit}"),
        table(
            ["항목", unit],
            [
                [label, number(total[key], 2)]
                for label, key in (
                    ("비용 후 총손익", "total_net"),
                    ("비용 후 실현손익", "realized_net"),
                    ("평가손익", "unrealized"),
                    ("누적 비용", "costs"),
                )
            ],
        ),
    ]
    if report["issues"]:
        blocks.append(paragraph("⚠ 합산 확인 필요\n" + "\n".join(report["issues"])))
    blocks.append(
        table(
            ["마켓", "원래 통화 순손익", f"{unit} 환산"],
            [
                [
                    market["label"],
                    f"{number(market['metrics']['total_net'], 4)} {market['currency']}",
                    number(market["converted"]["total_net"] if market["converted"] else None, 2),
                ]
                for market in report["markets"]
            ],
        )
    )
    for market in report["markets"]:
        metrics = market["metrics"]
        blocks.append(
            {
                "type": "details",
                "summary": f"{market['label']} · {market['status_label']}",
                "blocks": [
                    table(
                        ["항목", market["currency"]],
                        [
                            [label, number(metrics[key], 4)]
                            for label, key in (
                                ("실현손익 · 비용 전", "realized_gross"),
                                ("누적 비용", "costs"),
                                ("비용 후 실현손익", "realized_net"),
                                ("평가손익", "unrealized"),
                                ("비용 후 총손익", "total_net"),
                            )
                        ],
                    ),
                    footer(f"집계 {when(market['checked_at'])} · 갱신이 필요하면 마지막 기록을 표시합니다."),
                ],
            }
        )
    blocks.append(
        {
            "type": "details",
            "summary": "환산 기준과 적용 환율",
            "blocks": [
                paragraph(
                    "누적 손익을 현재 참고 환율로 환산합니다. 보유 기간의 환차손익이나 실제 환전 비용을 계산한 값은 아닙니다. USD와 USDT를 1:1로 취급하지 않습니다."
                ),
                *[
                    paragraph(
                        f"{rate['source'] or rate['currency'] + '/KRW'}\n1 {rate['currency']} = {number(rate['rate'], 4)} KRW\n조회 {when(rate['checked_at'])} · 유효 종료 {when(rate['valid_until'])}"
                    )
                    for rate in report["fx"]
                ],
            ],
        }
    )
    blocks.append(
        footer(
            "봇 원장의 손익입니다. 계좌 전체 손익·입출금·배당·리워드는 포함하지 않습니다.\n순손익 = 실현손익 − 기록 비용 + 평가손익. 모의 운용은 합산하지 않습니다."
        )
    )
    return blocks, nav("profit")


def strategies(rows, page=0):
    visible, page = page_of(rows, page)
    blocks = [
        heading(f"전략 관리 · {len(rows)}개"),
        paragraph("전략을 선택하면 현재 설정과 시작·중단 버튼이 나타납니다."),
    ]
    if not rows:
        blocks.append(paragraph("저장된 전략이 없습니다. 웹 화면에서 전략을 준비하세요."))
    controls = []
    for row in visible:
        blocks.append(
            paragraph(
                f"{row.symbol} · {'실거래' if row.mode == 'live' else '모의'} · {STATES.get(row.status, row.status)}\n{row.name}\n예산 {number(row.config['budget'], 2)} {currency(row.venue)}"
            )
        )
        controls.append([button(f"{row.symbol} · 설정 보기", f"detail:{row.id}")])
    return blocks, controls + nav("strategies", page, len(rows))


def strategy(row, settings, pending=False, notice=None):
    c = row.config
    spec = StrategySpec.model_validate(c)
    cur, unit = currency(row.venue), "개" if is_upbit(row.venue) else "주"
    blocks = [
        heading(f"{row.symbol} · {'실거래' if row.mode == 'live' else '모의'}"),
        paragraph(f"{row.name}\n{STATES.get(row.status, row.status)} · {row.reason}"),
    ]
    if notice:
        blocks.append(paragraph(notice))
    blocks += [
        table(
            ["설정", "값"],
            [
                [
                    "전략",
                    {
                        "grid": "그리드",
                        "trend": "추세 추종",
                        "rebound": "과매도 반등",
                        "rotation": "ETF 월간 교체",
                    }[c["kind"]],
                ],
                ["예산", f"{number(c['budget'], 2)} {cur}"],
                ["신호", "252거래일 · 상위 2개" if spec.kind == "rotation" else f"{c['timeframe']}분"],
                ["수수료 가정", f"{number(Decimal(c['commission_rate']) * 100, 4)}%"],
                ["체결 비용 / 호가 차이", f"{c['slippage_bps']} / {c['max_spread_bps']} bp"],
            ],
        )
    ]
    if c["kind"] == "grid":
        prices = levels(spec)
        blocks.append(
            paragraph(
                f"가격 범위 {number(c['lower'])} ~ {number(c['upper'])} {cur} · {c['grids']}단계\n하락 시 신규 매수 보류: {'사용' if c['signal_gate'] else '사용 안 함'}"
            )
        )
        blocks.append(
            {
                "type": "details",
                "summary": "단계별 주문 수량",
                "blocks": [
                    table(
                        [f"매수 {cur}", f"매도 {cur}", unit],
                        [
                            [number(price), number(prices[i + 1]), number(grid_quantity(spec, i), 10)]
                            for i, price in enumerate(prices[:-1])
                        ],
                    )
                ],
            }
        )
        if spec.inventory_quantity:
            blocks.append(
                paragraph(
                    f"보유 {number(spec.inventory_quantity, 10)}{unit} 편입 · 추가 {cur} 0\n매도부터 시작하고 각 단계의 매도대금으로 재매수합니다.\n손익 기준가 {number(spec.inventory_reference_price)} {cur}"
                )
            )
    if spec.kind == "rotation":
        blocks.append(
            paragraph(
                "SPY · QQQ · IWM · IEF · TLT · GLD · SHY\n"
                "최근 252거래일 수익률 상위 2개에 각 50% 배정합니다. 음수 후보 몫은 현금으로 유지합니다.\n"
                "월 첫 거래일 종가로 선정하고, 다음 정규장에 매도부터 집행합니다. 목표 비중과 5%p 이상 벌어지면 조절합니다.\n"
                "최초 진입은 직전 완성 정규장 기준입니다. 배당은 순위에 반영하며 실제 배당금은 전략에 자동 편입하지 않습니다."
            )
        )
        positions = [
            [symbol, number(position["quantity"], 0)]
            for symbol, position in row.state.get("positions", {}).items()
            if Decimal(position["quantity"]) > 0
        ]
        if positions:
            blocks.append(table(["보유 ETF", "주"], positions))
    else:
        blocks.append(
            paragraph(
                f"EMA {c['fast']}/{c['slow']} · RSI {c['rsi_period']} · 진입 {c['rsi_entry']} / 매도 {c['rsi_exit']}"
            )
        )
    risk = settings.risk_for(row.venue)
    blocks.append(
        paragraph(
            f"평가금액 하락 기준 · 하루 {number(risk['daily_loss'])} / 고점 대비 {number(risk['drawdown'])} {cur}\n"
            f"{'미국 정규장' if spec.kind == 'rotation' else '전체 거래 세션'} · 중단 시 보유 자산 유지"
        )
    )
    if is_upbit(row.venue):
        allowed = " · ".join(
            UPBIT_CAUTION_LABELS.get(name, name) for name in c.get("allowed_market_cautions", [])
        )
        blocks.append(paragraph(f"주의 상태 거래 허용: {allowed or '없음'}"))
    blocks.append(footer(f"설정 버전 {row.version} · 시작 버튼을 누르면 이 설정의 승인을 요청합니다."))
    controls = []
    if pending:
        blocks.append(paragraph("설정 변경 처리 중입니다. 완료 후 새로고침해 다시 확인하세요."))
    elif row.status in {"DRAFT", "PAUSED"}:
        controls.append(
            [
                button(
                    "위 설정 확인·시작 요청",
                    f"run:{row.id.replace('-', '')}:{row.version:x}:{approval_digest(row, settings)}",
                )
            ]
        )
    if row.status == "RUNNING":
        controls.append([button("이 전략 중단 요청", f"pause:{row.id}")])
    return blocks, controls + [
        [button("↻ 새로고침", f"detail:{row.id}"), button("전략 목록", "nav:strategies:0")]
    ]


def orders(rows, page=0):
    visible, page = page_of(rows, page)
    blocks = [
        heading(f"봇 주문 · 확인할 {len(rows)}건"),
        paragraph("봇이 기록한 실거래 주문입니다. 거래소의 수동 주문은 포함하지 않습니다."),
    ]
    for row in visible:
        blocks += [
            paragraph(
                f"{row.symbol} · {'매수' if row.side == 'BUY' else '매도'} · {STATES.get(row.status, row.status)}"
            ),
            table(
                ["주문 수량", "체결 수량", currency(row.venue)],
                [[number(row.quantity, 10), number(row.filled_quantity, 10), number(row.price)]],
            ),
            footer(f"갱신 {when(row.updated_at)} · 확인번호 {row.id[:8]}"),
        ]
    if not rows:
        blocks.append(paragraph("미체결·대조가 필요한 봇 주문이 없습니다."))
    return blocks, nav("orders", page, len(rows))


def research(row, public_url):
    venue = row.request.get("venue", "toss") if row else "toss"
    cur = currency(venue)
    label = "토스증권" if venue == "toss" else "업비트 " + cur
    blocks = [heading(label + " · 전략 발굴")]
    if not row:
        blocks.append(paragraph("아직 발굴 결과가 없습니다. 웹 화면에서 실제 시세로 비교를 시작하세요."))
    else:
        blocks.append(
            paragraph(
                f"{STATES.get(row.status, row.status)} · {row.progress.get('stage', '')}\n가상 예산 {number(row.request['budget'], 2)} {cur} · {', '.join(row.request['symbols'])}"
            )
        )
        if row.status == "SUCCEEDED":
            result = row.result
            selected = result.get("selected", {})
            spec, final = selected.get("spec", {}), selected.get("final", {})
            blocks.append(
                paragraph(
                    "모의 운용 검토 가능"
                    if result.get("recommendation_ready")
                    else "추천 보류 · 아직 시작 조건을 통과하지 못했습니다."
                )
            )
            blocks += [
                table(
                    ["비교 결과", "값"],
                    [
                        ["후보 수", result.get("compared_count", 0)],
                        ["선정 후보", f"{spec.get('symbol', '')} · {spec.get('kind', '')}"],
                        ["마지막 구간 손익", f"{number(final.get('profit'), 2)} {cur}"],
                        ["최대 하락폭", f"{number(final.get('max_drawdown'), 2)} {cur}"],
                        ["매수·매도 완료", str(final.get("completed_cycles", 0))],
                    ],
                )
            ]
            if result.get("recommendation_reasons"):
                blocks.append(
                    {
                        "type": "details",
                        "summary": "보류 사유",
                        "is_open": True,
                        "blocks": [paragraph("\n".join(result["recommendation_reasons"]))],
                    }
                )
        elif row.result.get("message"):
            blocks.append(paragraph(row.result["message"]))
        blocks.append(
            footer(
                f"갱신 {when(row.updated_at)} · 확인번호 {row.id[:8]}\n실제 자금·주문은 사용하지 않는 과거 데이터 비교입니다."
            )
        )
    controls = (
        [[button("↻ 새로고침", f"research:{row.id}"), button("⌂ 메뉴", "nav:menu:0")]]
        if row
        else nav("research")
    )
    return blocks, controls + [[{"text": "데이터·전체 결과 보기", "url": public_url + "/?venue=" + venue}]]


def notification(event, intent=None):
    title = {
        "fill": "체결 알림",
        "risk": "평가금액 하락 기준 알림",
        "pause": "중단 처리",
        "health": "연결 확인 필요",
        "research": "전략 발굴 결과",
    }.get(event.kind, "Cotrader 알림")
    blocks = [heading(title)]
    if intent is not None:
        cur = currency(intent.venue)
        blocks += [
            paragraph(
                f"{intent.symbol} · {'매수' if intent.side == 'BUY' else '매도'} · {'실거래' if intent.mode == 'live' else '모의'}"
            ),
            table(
                ["이번 처리", "값"],
                [
                    ["체결 수량", number(event.data.get("quantity"), 10)],
                    ["체결 금액", f"{number(event.data.get('amount'), 8)} {cur}"],
                    ["비용", f"{number(event.data.get('costs'), 8)} {cur}"],
                ],
            ),
        ]
    else:
        blocks.append(paragraph(event.message))
    return blocks + [footer(f"{when(event.created_at)} · 확인번호 {event.id[:8]}")]
