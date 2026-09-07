import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { ResearchLibrary } from "./ResearchLibrary";
import { Start } from "./Start";
import { Guide } from "./Guide";
import { ProfitPanel, ProfitReport } from "./ProfitPanel";
import {
  MarketCautionFields,
  MarketCautionSummary,
  MarketCautionsForm,
} from "./MarketCautions";
import {
  MarketContext,
  isCrypto,
  Venue,
  marketInfo,
  useMarket,
  formatMoney,
} from "./Market";

import {
  approvalMatches,
  changedApproval,
  prepareStartApproval,
  verifyStartApproval,
  waitForCommand,
} from "./approval";

type Data = Record<string, any>;
type Tab = "start" | "overview" | "strategies" | "research" | "orders";
declare global {
  interface Window {
    Telegram?: { WebApp?: { initData: string; ready(): void; expand(): void } };
  }
}

const when = (value: string) =>
  new Date(
    value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
  ).toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
const labels: Data = {
  CANCEL_REQUESTED: "중단 요청",
  ARCHIVED: "종료",
  DRAFT: "승인 대기",
  RUNNING: "실행 중",
  PAUSED: "중단",
  QUEUED: "대기",
  SUCCEEDED: "완료",
  FAILED: "실패",
  REJECTED: "거절",
  UNKNOWN: "대조 필요",
  PENDING: "미체결",
  FILLED: "체결",
  PARTIAL_FILLED: "부분 체결",
  PENDING_CANCEL: "취소 확인 중",
  CANCELED: "취소",
  PREPARED: "주문 준비",
  SENDING: "응답 확인 중",
};
const kindName: Data = {
  grid: "그리드",
  trend: "추세 추종",
  rebound: "과매도 반등",
};

class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function api(path: string, body?: unknown): Promise<any> {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d: Data) => d.msg).join("\n")
      : data.detail;
    throw new ApiError(
      detail || `요청을 처리하지 못했습니다 (${response.status})`,
      response.status,
    );
  }
  return data;
}

function Icon({ name }: { name: string }) {
  const paths: Data = {
    overview: "M3 12h5v9H3z M10 3h5v18h-5z M17 8h5v13h-5z",
    strategies: "M3 5h18M3 12h18M3 19h18M8 2v6M16 9v6M10 16v6",
    research:
      "M9 3h6M10 3v7l-6 9a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-9V3M7 15h10",
    orders: "M8 3h12v18H4V7zM8 3v4H4M8 12h8M8 16h8",
    arrow: "M5 12h14M13 6l6 6-6 6",
    plus: "M12 4v16M4 12h16",
    stop: "M5 5h14v14H5z",
  };
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.overview} />
    </svg>
  );
}

function Chart({
  data,
  empty = "거래와 평가 기록이 쌓이면 이곳에 표시됩니다.",
}: {
  data: { at: string; equity: string }[];
  empty?: string;
}) {
  const { money } = useMarket();
  if (data.length < 2)
    return (
      <div className="chart-empty">
        <div className="empty-lines" />
        <span>{empty}</span>
      </div>
    );
  const values = data.map((d) => Number(d.equity));
  const low = Math.min(...values),
    high = Math.max(...values),
    padding = Math.max((high - low) * 0.2, 5);
  const points = values
    .map(
      (v, i) =>
        `${((i / (values.length - 1)) * 900).toFixed(1)},${(190 - ((v - low + padding) / (high - low + 2 * padding)) * 170).toFixed(1)}`,
    )
    .join(" ");
  return (
    <div className="chart">
      <div className="chart-scale">
        <span>{money(high + padding)}</span>
        <span>{money(low - padding)}</span>
      </div>
      <svg
        viewBox="0 0 900 210"
        role="img"
        aria-label="평가금액 추이"
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
            <stop stopColor="#277357" stopOpacity=".15" />
            <stop offset="1" stopColor="#277357" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path
          d={`M0,210 L${points.replaceAll(" ", " L")} L900,210 Z`}
          fill="url(#fade)"
        />
        <polyline
          points={points}
          fill="none"
          stroke="#277357"
          strokeWidth="2.3"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="chart-dates">
        <span>{when(data[0].at)}</span>
        <span>{when(data.at(-1)!.at)}</span>
      </div>
    </div>
  );
}

function TradingApp() {
  const query = new URLSearchParams(window.location.search);
  const [venue, setVenue] = useState<Venue>(
    query.get("venue") === "upbit_usdt"
      ? "upbit_usdt"
      : query.get("venue") === "upbit"
        ? "upbit"
        : "toss",
  );
  return (
    <MarketContext.Provider value={venue}>
      <App
        venue={venue}
        onVenue={(next) => {
          const params = new URLSearchParams(window.location.search);
          params.set("venue", next);
          params.delete("symbol");
          window.history.replaceState(null, "", `/?${params}`);
          setVenue(next);
        }}
        key={venue}
      />
    </MarketContext.Provider>
  );
}

function App({
  venue,
  onVenue,
}: {
  venue: Venue;
  onVenue: (venue: Venue) => void;
}) {
  const { money, currency, unit } = useMarket();
  const params = new URLSearchParams(window.location.search);
  const requestedTab = params.get("view") as Tab;
  const [tab, setTab] = useState<Tab>(
    ["start", "overview", "strategies", "research", "orders"].includes(
      requestedTab,
    )
      ? requestedTab
      : "start",
  );
  const [mode, setMode] = useState(
    params.get("mode") === "live" ? "live" : "paper",
  );
  const [status, setStatus] = useState<Data>({});
  const [account, setAccount] = useState<Data>({});
  const [portfolio, setPortfolio] = useState<Data | null>(null);
  const [profit, setProfit] = useState<ProfitReport | null>(null);
  const [strategies, setStrategies] = useState<Data[]>([]);
  const [orders, setOrders] = useState<Data[]>([]);
  const [events, setEvents] = useState<Data[]>([]);
  const [snapshots, setSnapshots] = useState<Data[]>([]);
  const [datasets, setDatasets] = useState<Data[]>([]);
  const [jobs, setJobs] = useState<Data[]>([]);
  const [discoveries, setDiscoveries] = useState<Data | null>(null);
  const [saved, setSaved] = useState<Data[]>([]);
  const [commands, setCommands] = useState<Data[]>([]);
  const [auth, setAuth] = useState(false);
  const [authMode, setAuthMode] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [inventoryAsset, setInventoryAsset] = useState<Data | null>(null);
  const [cautionStrategy, setCautionStrategy] = useState<Data | null>(null);
  const [selected, setSelected] = useState<Data | null>(null);
  const [confirm, setConfirm] = useState<Data | null>(null);
  const [result, setResult] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      const info = await api("/auth/mode");
      setAuthMode(info.mode);
      if (info.mode === "github") {
        await api("/auth/session");
      }
      if (info.mode === "telegram") {
        const webapp = window.Telegram?.WebApp;
        if (!webapp?.initData)
          throw new Error("본인 텔레그램 봇의 /web 버튼으로 열어주세요.");
        await api("/auth/telegram", { init_data: webapp.initData });
        webapp.ready();
        webapp.expand();
      }
      setAuth(true);
    })().catch((e) => setError(e.message));
  }, []);

  const refreshSequence = useRef(0);
  const refresh = useCallback(async () => {
    if (!auth) return;
    const sequence = ++refreshSequence.current;
    const values = await Promise.all([
      api(`/status?venue=${venue}&mode=${mode}`),
      api(`/portfolio?mode=${mode}&venue=${venue}`),
      api("/strategies"),
      api(`/orders?mode=${mode}&venue=${venue}`),
      api("/events"),
      api(`/snapshots?mode=${mode}&venue=${venue}`),
      api("/datasets"),
      api("/backtests"),
      api("/commands"),
      api(`/account?venue=${venue}`),
      api("/recommendations"),
      api(`/discoveries?venue=${venue}`),
      api(`/profit?mode=${mode}&base_currency=KRW`),
    ]);
    if (sequence !== refreshSequence.current) return;
    setDiscoveries(values[11]);
    setProfit(values[12]);
    setStatus(values[0]);
    setPortfolio(values[1]?.data ?? null);
    setStrategies(values[2].filter((row: Data) => row.venue === venue));
    setOrders(values[3]);
    setEvents(values[4]);
    setSnapshots(values[5]);
    setDatasets(values[6].filter((row: Data) => row.venue === venue));
    setJobs(
      values[7].filter(
        (row: Data) => (row.request.spec.venue || "toss") === venue,
      ),
    );
    setCommands(values[8]);
    setAccount(values[9]);
    setSaved(
      values[10].filter(
        (row: Data) => (row.evidence.spec.venue || "toss") === venue,
      ),
    );
  }, [auth, mode, venue]);

  const showError = useCallback((e: Error) => {
    if (e instanceof ApiError && e.status === 401) setAuth(false);
    setError(e.message);
  }, []);

  useEffect(() => {
    refresh().catch(showError);
    const timer = setInterval(() => refresh().catch(showError), 5000);
    return () => {
      clearInterval(timer);
      refreshSequence.current++;
    };
  }, [refresh, showError]);
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      showError(e as Error);
    } finally {
      setBusy(false);
    }
  };
  const sendCommand = (action: string, payload: Data) =>
    act(async () => {
      if (action === "start") {
        try {
          await verifyStartApproval(api, {
            id: payload.strategy_id,
            ...payload,
          });
        } catch (error) {
          setConfirm(null);
          throw error;
        }
      }
      const command = await api("/commands", {
        id: crypto.randomUUID(),
        action,
        payload,
      });
      setConfirm(null);
      if (action === "set_usdt_capital") {
        setNotice("운용 한도를 반영 중입니다…");
        try {
          await waitForCommand(api, command.id);
        } finally {
          setNotice("");
          await refresh();
        }
        setNotice(
          "운용 한도 반영 완료. BTC 설정 확인·시작에서 최신 내용을 확인하세요.",
        );
        return;
      }
      setNotice(
        "요청을 접수했습니다. 아래 처리 기록에서 결과를 확인할 수 있습니다.",
      );
    });
  const staleConfirmation =
    confirm?.action === "start" &&
    !approvalMatches(
      confirm.strategy,
      strategies.find((s) => s.id === confirm.strategy.id),
    );
  const funded = strategies.filter((s) => s.mode === mode && s.state.funded);
  const engineTime = status.engine?.updated_at
    ? new Date(`${status.engine.updated_at}Z`).getTime()
    : 0;
  const connected = Date.now() - engineTime < 120000;
  const titles: Data = {
    start: "쉽게 시작",
    overview: "포트폴리오",
    strategies: "내 전략",
    research: "전략 연구실",
    orders: "주문과 기록",
  };
  const subtitles: Data = {
    start: "시장과 가상 예산을 고르면, 나머지는 함께 찾습니다.",
    overview: "수익뿐 아니라, 보유 자산의 변화까지 확인하세요.",
    strategies: "가격과 예산을 정하고, 확인한 전략만 실행하세요.",
    research: "실행하기 전에 과거 데이터로 비교하세요.",
    orders: "신호부터 체결까지 모든 판단을 확인하세요.",
  };

  if (!auth)
    return (
      <main className="login-screen">
        <section className="panel login-panel">
          <span className="eyebrow">COTRADER</span>
          <h1>내 트레이딩 데스크</h1>
          <a className="outline" href="/guide">
            처음이라면 사용 가이드 열기 →
          </a>
          <p>
            계좌와 거래 기록은 허용된 본인 계정으로 로그인한 뒤 확인할 수
            있습니다.
          </p>
          {authMode === "github" ? (
            <>
              <a className="primary" href="/api/auth/github">
                GitHub으로 로그인
              </a>
              <p className="fine-print">
                저장소나 이메일 접근 권한을 요청하지 않습니다.
              </p>
              {new URLSearchParams(window.location.search).has(
                "login_error",
              ) && (
                <p role="alert">
                  로그인을 완료하지 못했습니다. 허용된 계정인지 확인한 뒤 다시
                  시도하세요.
                </p>
              )}
            </>
          ) : (
            <p>{error || "연결을 확인하고 있습니다."}</p>
          )}
        </section>
      </main>
    );

  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={() => setTab("start")}>
          <span className="brand-mark">
            c<span>↗</span>
          </span>
          <div>
            cotrader<small>내 투자 기록</small>
          </div>
        </a>
        <nav>
          {(
            ["start", "overview", "strategies", "research", "orders"] as Tab[]
          ).map((key) => (
            <button
              key={key}
              className={tab === key ? "active" : ""}
              onClick={() => setTab(key)}
            >
              <Icon name={key} />
              <span className="nav-full">{titles[key]}</span>
              <span className="nav-compact">
                {
                  {
                    start: "시작",
                    overview: "자산",
                    strategies: "전략",
                    research: "연구",
                    orders: "기록",
                  }[key]
                }
              </span>
              {key === "strategies" && (
                <span className="count">
                  {
                    strategies.filter(
                      (s) => s.status !== "ARCHIVED" && s.mode === mode,
                    ).length
                  }
                </span>
              )}
            </button>
          ))}
          <a className="guide-nav" href="/guide">
            사용 가이드 ↗
          </a>
        </nav>
        <div className="sidebar-note">
          <span className="eyebrow">운영 원칙</span>
          <p>
            예산 안에서 매매하고,
            <br />
            모든 판단을 기록합니다.
          </p>
          <small>
            미국 주식 · KRW·USDT 코인
            <br />
            토스증권 · 업비트
          </small>
        </div>
        <div className="sidebar-bottom">
          <span className={`dot ${connected ? "green" : ""}`} />
          {connected ? "실행기 연결됨" : "실행기 연결 대기"}
          <small>개인 계좌 전용 · {currency}</small>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <label className="market-picker">
            시장
            <select
              aria-label="시장 선택"
              value={venue}
              onChange={(e) => onVenue(e.target.value as Venue)}
            >
              <option value="toss">토스 미국 주식 · USD</option>
              <option value="upbit">업비트 코인 · KRW</option>
              <option value="upbit_usdt">업비트 코인 · USDT</option>
            </select>
          </label>
          <span className="breadcrumb">
            내 트레이딩 데스크 <span>/</span> {titles[tab]}
          </span>
          <div className="mode-switch">
            <button
              className={mode === "paper" ? "selected" : ""}
              onClick={() => setMode("paper")}
            >
              모의매매
            </button>
            <button
              className={mode === "live" ? "selected live" : ""}
              onClick={() => setMode("live")}
            >
              실거래
            </button>
          </div>
          {authMode !== "local" && (
            <button
              className="text-button"
              onClick={() =>
                act(async () => {
                  await api("/auth/logout", {});
                  setAuth(false);
                })
              }
            >
              로그아웃
            </button>
          )}
          <div className="avatar">ME</div>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {tab === "start"
                  ? "PAPER RESEARCH"
                  : mode === "paper"
                    ? "PAPER TRADING"
                    : "LIVE TRADING"}
              </div>
              <h1>{titles[tab]}</h1>
              <p>{subtitles[tab]}</p>
            </div>
            {tab !== "start" && (
              <button
                className="primary"
                onClick={() => setShowForm(true)}
                disabled={!auth}
              >
                <Icon name="plus" />
                직접 설정
              </button>
            )}
          </div>
          {tab !== "start" && (
            <div className="getting-started">
              <div>
                <strong>처음 시작하시나요?</strong>
                <p>
                  실제 종목 데이터 수집부터 모의매매까지, 단계별로 따라오세요.
                </p>
              </div>
              <a className="primary" href="/guide">
                사용 가이드 열기 →
              </a>
            </div>
          )}
          {error && (
            <div className="banner danger" role="alert">
              {error}
              <button onClick={() => setError("")} aria-label="오류 닫기">
                ×
              </button>
            </div>
          )}
          {notice && (
            <div className="banner" role="status">
              {notice}
              <button onClick={() => setNotice("")} aria-label="안내 닫기">
                ×
              </button>
            </div>
          )}
          {tab === "start" && discoveries && (
            <Start
              data={discoveries}
              api={api}
              act={act}
              busy={busy}
              onDraft={() => {
                setMode("paper");
                setTab("strategies");
              }}
              onAdvanced={() => setTab("research")}
            />
          )}
          {isCrypto(venue) && (
            <div className="banner">
              업비트 {currency} 마켓 ·{" "}
              {status.live_enabled
                ? "실거래 허용. 전략별 점검과 시작 확인 후 자동 주문합니다."
                : "실거래 잠금. 연구·모의매매·계좌 조회를 사용할 수 있습니다."}
            </div>
          )}
          {(status.market_source === "offline" || !connected) && (
            <div className="connection-banner">
              <span className="connection-icon">⌁</span>
              <div>
                <strong>
                  {status.market_source === "offline"
                    ? "시세 연결 전 · 주문은 실행되지 않습니다"
                    : "실행기 상태를 확인해주세요"}
                </strong>
                <p>
                  전략 설정과 가져온 데이터의 백테스트를 사용할 수 있습니다.
                  모의 포트폴리오 값은 실제 계좌 잔고와 별도입니다.
                </p>
              </div>
              <span className="badge neutral">
                {mode === "paper"
                  ? "모의 환경"
                  : status.live_enabled
                    ? "실거래 허용"
                    : "실거래 잠금"}
              </span>
            </div>
          )}
          {tab === "overview" && (
            <>
              <ProfitPanel report={profit} />
              <AccountPanel
                account={account}
                telegram={status.telegram}
                onInventory={setInventoryAsset}
              />
              <div className="metrics">
                <Metric
                  label="운용 평가금액"
                  value={money(portfolio?.equity)}
                  sub={`배정 가능한 총예산 ${money(status.capital)}`}
                  emphasis
                />
                <Metric
                  label="총 손익"
                  value={
                    portfolio?.equity == null
                      ? "—"
                      : money(
                          Number(portfolio.equity) - Number(portfolio.capital),
                        )
                  }
                  sub="평가손익과 비용 포함"
                />
                <Metric
                  label="보유 자산 평가"
                  value={money(portfolio?.exposure)}
                  sub={`${funded.length}개 전략에 예산 배정`}
                />
                <Metric
                  label="남은 현금"
                  value={money(portfolio?.cash)}
                  sub="전략별 현금 + 미배정 예산"
                />
              </div>
              <section className="panel chart-panel">
                <div className="section-heading">
                  <div>
                    <h2>평가금액 추이</h2>
                    <p>미실현 손익을 포함한 운용 기록</p>
                  </div>
                  <span className="badge neutral">
                    최근 {snapshots.length}개 기록
                  </span>
                </div>
                <Chart
                  data={snapshots
                    .filter((s) => s.data.complete)
                    .map((s) => ({
                      at: `${s.created_at}Z`,
                      equity: s.data.equity,
                    }))}
                />
              </section>
              <div className="two-columns">
                <section className="panel">
                  <div className="section-heading">
                    <h2>운영 중인 전략</h2>
                    <button
                      className="text-button"
                      onClick={() => setTab("strategies")}
                    >
                      모두 보기 <Icon name="arrow" />
                    </button>
                  </div>
                  {funded.length ? (
                    funded.map((s) => (
                      <div className="strategy-mini" key={s.id}>
                        <div className="ticker-icon">
                          {s.symbol.slice(0, 2)}
                        </div>
                        <div>
                          <strong>{s.symbol}</strong>
                          <small>
                            {s.name} · {kindName[s.config.kind]}
                          </small>
                        </div>
                        <span
                          className={`badge ${s.status === "RUNNING" ? "success" : "neutral"}`}
                        >
                          {labels[s.status]}
                        </span>
                      </div>
                    ))
                  ) : (
                    <Empty
                      title="첫 전략을 준비해보세요"
                      text="종목과 예산을 고르면 가격선별 주문 규모를 미리 확인할 수 있습니다."
                      button="전략 만들기"
                      action={() => setShowForm(true)}
                    />
                  )}
                </section>
                <section className="panel risk-panel">
                  <div className="section-heading">
                    <h2>자동 중단 기준</h2>
                    <span
                      className={`badge ${portfolio?.halted ? "warn" : "success"}`}
                    >
                      {portfolio?.halted ? "중단됨" : "보유 유지"}
                    </span>
                  </div>
                  <RiskRow
                    title="하루 손실"
                    amount={status.daily_loss}
                    current={
                      portfolio?.equity != null
                        ? Math.max(
                            0,
                            Number(portfolio.daily_anchor) -
                              Number(portfolio.equity),
                          )
                        : 0
                    }
                  />
                  <RiskRow
                    title="운용 고점 대비 하락"
                    amount={status.drawdown}
                    current={
                      portfolio?.equity != null
                        ? Math.max(
                            0,
                            Number(portfolio.high_water) -
                              Number(portfolio.equity),
                          )
                        : 0
                    }
                  />
                  <p className="fine-print">
                    기준 도달 시 대기 주문을 취소하고 알립니다. 보유분은
                    유지하므로 이후 평가손실은 더 커질 수 있습니다.
                  </p>
                  <button
                    className="outline full"
                    onClick={() =>
                      setConfirm({
                        action: "pause_all",
                        title: "전체 전략을 중단할까요?",
                        detail:
                          "봇의 대기 주문을 취소하고 보유 자산은 유지합니다. 취소 확인 전까지 체결될 수 있습니다.",
                        payload: {},
                      })
                    }
                  >
                    <Icon name="stop" />
                    전체 주문 중단
                  </button>
                  {portfolio?.halted && (
                    <button
                      className="text-button"
                      onClick={() =>
                        setConfirm({
                          action: "reset_risk",
                          title: "손실 기준을 재설정할까요?",
                          detail:
                            "현재 평가금액을 새 기준으로 삼아 추가 손실 허용 범위를 다시 부여합니다. 전략은 별도로 재개해야 합니다.",
                          payload: { mode, venue, confirm: "RESET_ANCHORS" },
                        })
                      }
                    >
                      현재 평가금액으로 기준 재설정
                    </button>
                  )}
                </section>
              </div>
            </>
          )}
          {tab === "strategies" && (
            <>
              <div className="strategy-types">
                <div>
                  <span>01</span>
                  <strong>그리드</strong>
                  <p>정한 구간 안에서 분할 매수·매도</p>
                </div>
                <div>
                  <span>02</span>
                  <strong>추세 추종</strong>
                  <p>이동평균 교차로 방향 확인</p>
                </div>
                <div>
                  <span>03</span>
                  <strong>과매도 반등</strong>
                  <p>RSI 회복과 추세를 함께 확인</p>
                </div>
              </div>
              <div className="strategy-grid">
                {strategies
                  .filter((s) => s.mode === mode && s.status !== "ARCHIVED")
                  .map((s) => (
                    <section className="panel strategy-card" key={s.id}>
                      <div className="section-heading">
                        <div>
                          <span className="eyebrow">
                            {kindName[s.config.kind]}
                          </span>
                          <h2>
                            {s.symbol} <span className="muted">/ {s.name}</span>
                          </h2>
                        </div>
                        <span className="badge neutral">
                          {labels[s.status]}
                        </span>
                      </div>
                      <div className="strategy-summary">
                        <div>
                          <small>
                            {Number(s.config.inventory_quantity) > 0
                              ? "배정 평가금액"
                              : "배정 예산"}
                          </small>
                          <strong>{money(s.config.budget)}</strong>
                        </div>
                        <div>
                          <small>보유 수량</small>
                          <strong>
                            {Number(s.state.quantity)}
                            {unit}
                          </strong>
                        </div>
                      </div>
                      {s.config.kind === "grid" && (
                        <div className="grid-visual">
                          <span>{money(s.config.lower)}</span>
                          <div>
                            {Array.from(
                              { length: s.config.grids + 1 },
                              (_, i) => (
                                <i key={i} />
                              ),
                            )}
                          </div>
                          <span>{money(s.config.upper)}</span>
                        </div>
                      )}
                      <p className="strategy-reason">{s.reason}</p>
                      {isCrypto(s.venue) && (
                        <>
                          <MarketCautionSummary
                            allowed={s.config.allowed_market_cautions}
                          />
                          <button
                            className="outline"
                            disabled={
                              busy ||
                              s.pending_settings ||
                              !["DRAFT", "PAUSED"].includes(s.status)
                            }
                            onClick={() =>
                              act(async () => {
                                const current = (await api("/strategies")).find(
                                  (row: Data) => row.id === s.id,
                                );
                                if (!current || current.pending_settings)
                                  throw new Error(
                                    "주의 설정을 반영 중입니다. 잠시 후 다시 확인하세요.",
                                  );
                                setCautionStrategy(current);
                              })
                            }
                          >
                            {s.status === "RUNNING"
                              ? "중단 후 주의 설정 변경 가능"
                              : s.pending_settings
                                ? "주의 설정 반영 중…"
                                : "주의 설정"}
                          </button>
                        </>
                      )}
                      {s.venue === "upbit" &&
                        s.mode === "live" &&
                        s.state.funded &&
                        Number(s.state.quantity) > 0 && (
                          <button
                            className="outline"
                            disabled={s.status === "RUNNING"}
                            onClick={() =>
                              setInventoryAsset({
                                currency: s.symbol.split("-")[1],
                                balance: s.state.quantity,
                                source: s,
                              })
                            }
                          >
                            {s.status === "RUNNING"
                              ? "중단 후 USDT로 보유 이관 가능"
                              : "USDT로 보유 이관·초안 준비"}
                          </button>
                        )}
                      {Number(s.state.released_value) > 0 && (
                        <p>
                          이관 시 평가액 {money(s.state.released_value)} · 실제
                          현금·매도 수익이 아닙니다.
                        </p>
                      )}

                      {Number(s.config.inventory_quantity) > 0 && (
                        <p className="banner">
                          보유 {s.config.inventory_quantity}
                          {unit}로 매도부터 시작 · 추가 {currency} 0. 각 단계의
                          매도대금으로 재매수한 뒤 같은 매도가에서 반복합니다.
                        </p>
                      )}
                      {s.status !== "RUNNING" &&
                        Number(s.state.quantity) === 0 && (
                          <button
                            className="text-button"
                            onClick={() =>
                              setConfirm({
                                action: "archive",
                                title: "전략 운영을 종료할까요?",
                                detail:
                                  "체결과 손익 기록은 보존하고 배정 예산을 해제합니다. 보유분과 미확인 주문이 있으면 종료할 수 없습니다.",
                                payload: { strategy_id: s.id },
                              })
                            }
                          >
                            운영 종료·예산 해제
                          </button>
                        )}
                      <div className="card-actions">
                        {s.mode === "paper" && (
                          <button
                            className="outline"
                            disabled={busy}
                            onClick={() =>
                              act(async () => {
                                await api("/strategies", {
                                  name: `${s.name.slice(0, 90)} · 실거래`,
                                  mode: "live",
                                  spec: s.config,
                                });
                                setMode("live");
                                setNotice(
                                  "실거래 초안을 복사했습니다. 준비 점검 후 설정 확인·시작을 눌러주세요.",
                                );
                              })
                            }
                          >
                            실거래 초안 복사
                          </button>
                        )}
                        {s.mode === "live" && s.status !== "RUNNING" && (
                          <button
                            className="outline"
                            disabled={busy}
                            onClick={() =>
                              sendCommand("check_live", { strategy_id: s.id })
                            }
                          >
                            실거래 준비 점검
                          </button>
                        )}
                        <button
                          className="outline"
                          disabled={Number(s.config.inventory_quantity) > 0}
                          title={
                            Number(s.config.inventory_quantity) > 0
                              ? "계좌 편입 초안은 과거 백테스트 대상이 아닙니다"
                              : undefined
                          }
                          onClick={() => {
                            setSelected(s);
                            setTab("research");
                          }}
                        >
                          백테스트
                        </button>
                        <button
                          className={
                            s.status === "RUNNING" ? "outline" : "primary"
                          }
                          disabled={
                            busy ||
                            s.pending_settings ||
                            (s.status !== "RUNNING" &&
                              s.mode === "live" &&
                              !status.live_enabled)
                          }
                          onClick={() =>
                            act(async () => {
                              const prepared =
                                s.status === "RUNNING"
                                  ? { strategy: s, grid: [] }
                                  : await prepareStartApproval(api, s.id);
                              const current = prepared.strategy;
                              setConfirm({
                                action:
                                  current.status === "RUNNING"
                                    ? "pause"
                                    : "start",
                                title:
                                  current.status === "RUNNING"
                                    ? "전략을 중단할까요?"
                                    : "이 설정으로 실행할까요?",
                                strategy:
                                  current.status === "RUNNING" ? null : current,
                                grid: prepared.grid,
                                detail:
                                  current.status === "RUNNING"
                                    ? "대기 주문을 취소하고 보유분은 유지합니다."
                                    : `${current.mode === "paper" ? "모의매매" : "실거래"} · 전체 세션 · 승인한 예산과 설정으로 자동 주문합니다.`,
                                payload: {
                                  strategy_id: current.id,
                                  version: current.version,
                                  approval: current.approval,
                                },
                              });
                            })
                          }
                        >
                          {s.status === "RUNNING"
                            ? "중단"
                            : s.mode === "live" && !status.live_enabled
                              ? "실거래 잠금"
                              : "설정 확인·시작"}
                        </button>
                      </div>
                    </section>
                  ))}
              </div>
              {venue === "upbit_usdt" && mode === "live" && (
                <section className="panel">
                  <p>
                    총운용 한도 {money(status.capital)} · 보유분 편입을 포함한
                    전략 예산의 상한입니다.
                  </p>
                  <button
                    className="outline"
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        const capital = await api("/upbit-usdt-capital");
                        setConfirm({
                          action: "set_usdt_capital",
                          title: "USDT 운용 한도를 변경할까요?",
                          detail:
                            "코트레이더 포켓의 등록 전략을 위한 한도입니다. 메인 포켓 자산은 포함하지 않습니다. 한도 변경만으로 전략을 시작하거나 주문하지 않습니다.",
                          capital,
                          payload: {
                            capital: capital.required,
                            approval: capital.approval,
                          },
                        });
                      })
                    }
                  >
                    운용 한도 설정
                  </button>
                </section>
              )}
              {!strategies.some(
                (s) => s.mode === mode && s.status !== "ARCHIVED",
              ) && (
                <section className="panel">
                  <Empty
                    title="아직 저장된 전략이 없습니다"
                    text="그리드, 추세 추종, 과매도 반등 중에서 선택할 수 있습니다."
                    button="새 전략"
                    action={() => setShowForm(true)}
                  />
                </section>
              )}
            </>
          )}
          {tab === "research" && (
            <>
              <Research
                commands={commands}
                busy={busy}
                datasets={datasets}
                strategies={strategies}
                selected={selected}
                jobs={jobs}
                act={act}
                onResult={async (id) =>
                  setResult(await api(`/backtests/${id}`))
                }
                onAdopt={(spec) => {
                  setSelected({ config: spec, suggested: true });
                  setShowForm(true);
                }}
              />
              <ResearchLibrary
                jobs={jobs}
                datasets={datasets}
                saved={saved}
                busy={busy}
                request={api}
                act={act}
                money={money}
                when={when}
              />
            </>
          )}
          {tab === "orders" && (
            <>
              <section className="panel">
                <div className="section-heading">
                  <h2>최근 주문</h2>
                  <span className="badge neutral">
                    {mode === "paper" ? "모의 주문" : "실제 주문"}{" "}
                    {orders.length}건
                  </span>
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>시각</th>
                        <th>종목</th>
                        <th>방향</th>
                        <th>주문가</th>
                        <th>체결 / 주문</th>
                        <th>상태</th>
                        <th>비용</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((o) => (
                        <tr key={o.id}>
                          <td>{when(o.created_at)}</td>
                          <td>
                            <strong>{o.symbol}</strong>
                          </td>
                          <td className={o.side === "BUY" ? "buy" : "sell"}>
                            {o.side === "BUY" ? "매수" : "매도"}
                          </td>
                          <td>{money(o.price)}</td>
                          <td>
                            {Number(o.filled_quantity)} / {Number(o.quantity)}
                          </td>
                          <td>
                            <span className="badge neutral">
                              {labels[o.status]}
                            </span>
                            {o.mode === "live" && o.status === "UNKNOWN" && (
                              <button
                                className="text-button"
                                onClick={() =>
                                  setConfirm({
                                    action: "resolve",
                                    title: "증권사 주문과 대조",
                                    detail: `${o.symbol} · ${o.side === "BUY" ? "매수" : "매도"} ${Number(o.quantity)}주 · ${money(o.price)} · ${when(o.submitted_at || o.created_at)}. 토스 앱에서 시각과 주문 내용을 확인한 후 주문 번호를 입력하세요. 새 주문을 제출하지 않습니다.`,
                                    payload: { intent_id: o.id, broker_id: "" },
                                  })
                                }
                              >
                                주문 대조
                              </button>
                            )}
                          </td>
                          <td>
                            {money(o.costs)}
                            {!o.costs_final && <small>잠정</small>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!orders.length && (
                    <Empty
                      title="주문 기록이 없습니다"
                      text="승인한 전략이 신호를 만들면 주문과 체결 결과가 여기에 기록됩니다."
                    />
                  )}
                </div>
              </section>
              <div className="two-columns">
                <section className="panel">
                  <div className="section-heading">
                    <h2>처리 기록</h2>
                  </div>
                  {commands.slice(0, 15).map((c) => (
                    <div className="event-row" key={c.id}>
                      <span className="event-dot" />
                      <div>
                        <strong>
                          {c.action} · {labels[c.status]}
                        </strong>
                        <p>
                          {c.result.message || `확인번호 ${c.id.slice(0, 8)}`}
                        </p>
                        <small>{when(c.created_at)}</small>
                      </div>
                    </div>
                  ))}
                  {!commands.length && (
                    <p className="muted">아직 접수된 요청이 없습니다.</p>
                  )}
                </section>
                <section className="panel">
                  <div className="section-heading">
                    <h2>신호와 알림</h2>
                  </div>
                  {events.slice(0, 20).map((e) => (
                    <div className="event-row" key={e.id}>
                      <span className="event-dot" />
                      <div>
                        <strong>{e.message}</strong>
                        <small>{when(e.created_at)}</small>
                      </div>
                    </div>
                  ))}
                  {!events.length && (
                    <p className="muted">기록이 쌓이면 표시됩니다.</p>
                  )}
                </section>
              </div>
            </>
          )}
          {Number(portfolio?.released_value) > 0 && (
            <p className="banner">
              총 평가에는 다른 시장으로 이관한 보유분의 당시 평가액{" "}
              {money(portfolio?.released_value)}이 포함됩니다. 사용 가능한
              현금이 아니며 이관 이후 변동은 새 시장에 기록됩니다.
            </p>
          )}
          <footer>
            봇 운용 금액은 {currency} 기준입니다. 시장별 예산·손익은 별도로
            계산합니다.{" "}
            <span>수수료·세금의 확정 여부는 주문 기록에서 확인하세요.</span>
          </footer>
        </div>
      </main>
      {cautionStrategy && (
        <MarketCautionsForm
          symbol={cautionStrategy.symbol}
          allowed={cautionStrategy.config.allowed_market_cautions}
          busy={busy}
          onClose={() => setCautionStrategy(null)}
          onSave={(allowed) =>
            act(async () => {
              const command = await api("/commands", {
                id: crypto.randomUUID(),
                action: "set_market_cautions",
                payload: {
                  strategy_id: cautionStrategy.id,
                  version: cautionStrategy.version,
                  approval: cautionStrategy.approval,
                  allowed_market_cautions: allowed,
                },
              });
              setCautionStrategy(null);
              setNotice("주의 설정을 반영 중입니다…");
              try {
                await waitForCommand(api, command.id);
                setNotice(
                  "주의 설정 반영이 완료되었습니다. 설정 확인·시작에서 최신 내용을 확인하세요.",
                );
              } catch (error) {
                setNotice("");
                throw error;
              } finally {
                await refresh();
              }
            })
          }
        />
      )}
      {inventoryAsset && (
        <InventoryForm
          asset={inventoryAsset}
          busy={busy}
          onClose={() => setInventoryAsset(null)}
          onPrepare={(payload) =>
            act(async () => {
              await api("/commands", {
                id: crypto.randomUUID(),
                action: "prepare_inventory",
                payload,
              });
              setInventoryAsset(null);
              if (payload.source_strategy_id) {
                window.history.replaceState(
                  null,
                  "",
                  "/?venue=upbit_usdt&mode=live&view=strategies",
                );
                onVenue("upbit_usdt");
              }
              setMode("live");
              setTab("strategies");
              setNotice(
                "계좌와 단계별 주문 가능 여부를 확인 중입니다. 완료되면 실거래 초안이 나타납니다. 실패 사유는 주문과 기록에서 확인하세요.",
              );
            })
          }
        />
      )}
      {showForm && (
        <StrategyForm
          mode={mode}
          initial={selected?.suggested ? selected.config : undefined}
          busy={busy}
          onClose={() => {
            setShowForm(false);
            if (selected?.suggested) setSelected(null);
          }}
          onSave={(body) =>
            act(async () => {
              await api("/strategies/preview", body);
              await api("/strategies", body);
              setShowForm(false);
              setSelected(null);
              setTab("strategies");
              setNotice(
                "전략을 저장했습니다. 설정 확인·시작 버튼으로 승인하면 실행됩니다.",
              );
            })
          }
        />
      )}
      {confirm && (
        <div className="modal-backdrop" onClick={() => setConfirm(null)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={confirm.title}
            onClick={(e) => e.stopPropagation()}
          >
            <h2>{confirm.title}</h2>
            <p>{confirm.detail}</p>
            {confirm.action === "set_usdt_capital" && (
              <div className="confirm-details">
                <p>
                  현재 {confirm.capital.capital} USDT → 등록 전략 전체 필요 한도{" "}
                  {confirm.capital.required} USDT
                </p>
                {confirm.capital.strategies.map((s: Data) => (
                  <p key={s.id}>
                    {s.symbol} · 예산 {s.budget} USDT
                  </p>
                ))}
                <label>
                  변경할 총운용 한도 · USDT
                  <input
                    aria-label="변경할 총운용 한도"
                    type="number"
                    min={confirm.capital.minimum}
                    max={confirm.capital.maximum}
                    step="any"
                    value={confirm.payload.capital}
                    onChange={(e) =>
                      setConfirm({
                        ...confirm,
                        payload: {
                          ...confirm.payload,
                          capital: e.target.value,
                        },
                      })
                    }
                  />
                </label>
                <p>
                  일일 손실 {confirm.capital.risk.daily_loss} USDT · 고점 대비
                  하락 {confirm.capital.risk.drawdown} USDT 기준을 유지합니다.
                  이미 발생한 손익과 위험 중단 상태도 유지합니다.
                </p>
                <p>
                  저장 후 변경된 한도로 전략 설정을 다시 확인해야 시작할 수
                  있습니다.
                </p>
              </div>
            )}
            {confirm.action === "resolve" && (
              <label>
                거래소 주문 번호
                <input
                  value={confirm.payload.broker_id}
                  maxLength={128}
                  onChange={(e) =>
                    setConfirm({
                      ...confirm,
                      payload: {
                        ...confirm.payload,
                        broker_id: e.target.value.trim(),
                      },
                    })
                  }
                />
              </label>
            )}
            {confirm.strategy && (
              <div className="confirm-details">
                <strong>
                  {confirm.strategy.symbol} ·{" "}
                  {kindName[confirm.strategy.config.kind]}
                </strong>
                <p>
                  예산 {money(confirm.strategy.config.budget)} · 설정 버전{" "}
                  {confirm.strategy.version}
                </p>
                {Number(confirm.strategy.config.inventory_quantity) > 0 && (
                  <div className="banner inventory-confirm">
                    <strong>
                      보유 {confirm.strategy.config.inventory_quantity}
                      {unit} 전량을 이 전략에 편입합니다.
                    </strong>
                    <p>
                      추가 {currency}는 필요하지 않습니다. 아래 매도가에서 먼저
                      팔고, 그 단계의 매도대금으로 아래 매수가에서 재매수합니다.
                      수량은 단계별 최대 보유 수량이며 이익은 {currency}로
                      남습니다.
                    </p>
                    <p>
                      평가 기준가{" "}
                      {money(confirm.strategy.config.inventory_reference_price)}{" "}
                      · 계좌 평균 매수가{" "}
                      {Number(
                        confirm.strategy.config.inventory_average_price,
                      ).toLocaleString("ko-KR")}{" "}
                      {confirm.strategy.config.inventory_average_currency ||
                        currency}
                      . 운용 손익은 평가 기준가부터 계산하며 기존 보유 손익과
                      별도입니다.
                    </p>
                    <p>
                      {confirm.strategy.config.execution_policy === "maker_only"
                        ? "메이커 전용 지정가를 단계별로 미리 올려 둡니다. 즉시 체결되는 가격이면 주문을 보류하며 가격을 쫓지 않습니다."
                        : "가격에 도달할 때 종목당 한 주문씩 처리합니다."}{" "}
                      가격 범위 아래에서도 매수·대기를 이어가며, 하루 손실·고점
                      대비 손실 한도에 도달하면 주문을 중단하고 보유분을
                      유지합니다.
                    </p>
                  </div>
                )}
                {confirm.strategy.config.kind === "grid" && (
                  <p>
                    {money(confirm.strategy.config.lower)} ~{" "}
                    {money(confirm.strategy.config.upper)} ·{" "}
                    {confirm.strategy.config.grids}단계
                  </p>
                )}
                {confirm.grid?.length > 0 && (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>매수가</th>
                          <th>매도가</th>
                          <th>수량</th>
                        </tr>
                      </thead>
                      <tbody>
                        {confirm.grid.map((level: Data) => (
                          <tr key={level.buy}>
                            <td>{money(level.buy)}</td>
                            <td>{money(level.sell)}</td>
                            <td>
                              {String(level.quantity).replace(
                                /(\.\d*?[1-9])0+$|\.0+$/,
                                "$1",
                              )}
                              {unit}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {!Number(confirm.strategy.config.inventory_quantity) && (
                  <p>
                    {confirm.strategy.config.timeframe}분 봉 · EMA{" "}
                    {confirm.strategy.config.fast}/
                    {confirm.strategy.config.slow} · RSI{" "}
                    {confirm.strategy.config.rsi_period}
                  </p>
                )}
                {confirm.strategy.config.kind === "grid" &&
                  !Number(confirm.strategy.config.inventory_quantity) && (
                    <p>
                      {confirm.strategy.config.spacing === "geometric"
                        ? "동일 비율"
                        : "동일 금액"}{" "}
                      간격 · 하락 시 매수 보류{" "}
                      {confirm.strategy.config.signal_gate
                        ? "사용"
                        : "사용 안 함"}
                    </p>
                  )}
                {confirm.strategy.config.kind === "rebound" && (
                  <p>
                    RSI 진입 {confirm.strategy.config.rsi_entry} · 매도{" "}
                    {confirm.strategy.config.rsi_exit}
                  </p>
                )}
                <p>
                  하루 손실 {money(status.daily_loss)} / 고점 대비{" "}
                  {money(status.drawdown)}에 중단
                </p>
                <p>
                  호가 차이 최대 {confirm.strategy.config.max_spread_bps}bp ·
                  시세 최대 {confirm.strategy.config.quote_max_age}초
                </p>
                <p>
                  수수료 가정{" "}
                  {Number(confirm.strategy.config.commission_rate) * 100}% ·
                  체결 비용 {confirm.strategy.config.slippage_bps}bp
                </p>
                <p>중단 시 미체결 취소 · 보유 유지</p>
                {isCrypto(confirm.strategy.venue) && (
                  <MarketCautionSummary
                    allowed={confirm.strategy.config.allowed_market_cautions}
                  />
                )}
                {confirm.strategy.mode === "live" && (
                  <p className="banner danger">
                    실제 계좌에서 자동으로 매수·매도합니다. 시작 직전 계좌·기존
                    주문·비용을 다시 점검하며 조건을 충족하지 않으면 시작을
                    거절합니다.
                  </p>
                )}
              </div>
            )}
            {staleConfirmation && (
              <p className="banner danger">{changedApproval}</p>
            )}
            <div className="card-actions">
              <button className="outline" onClick={() => setConfirm(null)}>
                돌아가기
              </button>
              <button
                className="primary"
                disabled={
                  busy ||
                  staleConfirmation ||
                  (confirm.action === "resolve" &&
                    !confirm.payload.broker_id) ||
                  (confirm.action === "set_usdt_capital" &&
                    (!confirm.payload.capital ||
                      Number(confirm.payload.capital) <= 0 ||
                      Number(confirm.payload.capital) <
                        Number(confirm.capital.minimum) ||
                      Number(confirm.payload.capital) >
                        Number(confirm.capital.maximum)))
                }
                onClick={() => sendCommand(confirm.action, confirm.payload)}
              >
                {busy ? "접수 중…" : "내용 확인·요청"}
              </button>
            </div>
          </div>
        </div>
      )}
      {result && (
        <div className="modal-backdrop" onClick={() => setResult(null)}>
          <div
            className="modal large"
            role="dialog"
            aria-modal="true"
            aria-label="백테스트 결과"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="section-heading">
              <h2>백테스트 결과</h2>
              <button
                className="close"
                onClick={() => setResult(null)}
                aria-label="결과 닫기"
              >
                ×
              </button>
            </div>
            {result.result.synthetic && (
              <div className="banner">
                가상 예제 데이터 결과입니다. 실제 시장 성과가 아닙니다.
              </div>
            )}
            <div className="metrics compact">
              <Metric label="총 손익" value={money(result.result.profit)} />
              <Metric
                label="최대 하락"
                value={money(result.result.max_drawdown)}
              />
              <Metric
                label="체결 수"
                value={result.result.trade_count ?? "—"}
              />
            </div>
            <Chart data={result.result.curve || []} />
            <p>
              단순 보유 시 평가금액 {money(result.result.buy_hold_equity)} ·
              종료 보유 {Number(result.result.held_quantity || 0)}
              {unit}
            </p>
            {result.result.halted && (
              <div className="banner danger">{result.result.halted}</div>
            )}
            <div className="limitations">
              {(result.result.limitations || []).map((l: string) => (
                <p key={l}>· {l}</p>
              ))}
            </div>
            <small className="mono">
              데이터 확인값 {result.result.data_hash?.slice(0, 24)}
            </small>
          </div>
        </div>
      )}
    </div>
  );
}

const optimizerNames: Data = {
  exhaustive: "전수 탐색",
  random: "무작위",
  tpe: "TPE",
  nsga2: "NSGA-II",
};

function CryptoAccountPanel({
  account,
  onInventory,
}: {
  account: Data;
  onInventory: (asset: Data) => void;
}) {
  const { money, currency } = useMarket();
  const snapshot = account.snapshot;
  const ready = account.status === "CONNECTED" && !account.stale;
  return (
    <section className="panel account-panel">
      <div className="section-heading">
        <h2>업비트 실제 계좌</h2>
        <span className={`badge ${ready ? "success" : "neutral"}`}>
          {ready
            ? account.read_only
              ? "조회 전용 연결"
              : "계좌 연결"
            : "조회 확인 필요"}
        </span>
      </div>
      <p>
        아래 잔액은 실제 계좌이며 봇의 배정 예산과 별도입니다. 실거래는 전략을
        확인하고 시작한 경우에만 실행됩니다.
      </p>
      {account.error && (
        <p className="banner danger">
          조회 실패: {account.error} · 마지막 조회 값을 현재 잔액으로 사용하지
          않습니다.
        </p>
      )}
      {snapshot ? (
        <>
          <div className="metrics compact">
            <Metric
              label={`사용 가능한 ${currency}`}
              value={money(snapshot.cash_available)}
              sub={ready ? "계좌 사용 가능 잔액" : "마지막 조회 값 · 갱신 필요"}
            />
            <Metric
              label={`주문 등에 묶인 ${currency}`}
              value={money(snapshot.cash_locked)}
              sub="기존 미체결 주문 등에 배정된 금액"
            />
            <Metric
              label="보유 코인"
              value={`${snapshot.assets.length}종목`}
              sub="계좌 평가 합계와 봇 성과는 별도입니다"
            />
          </div>
          <details open>
            <summary>보유 코인 수량 보기</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>코인</th>
                    <th>사용 가능</th>
                    <th>주문 등에 묶인 수량</th>
                    <th>평균 매수가</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.assets.map((r: Data) => (
                    <tr key={r.currency}>
                      <td>{r.currency}</td>
                      <td>{r.balance}</td>
                      <td>{r.locked}</td>
                      <td>
                        {Number(r.avg_buy_price).toLocaleString("ko-KR")}{" "}
                        {r.unit_currency}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <div className="card-actions">
            {snapshot.assets
              .filter(
                (r: Data) =>
                  Number(r.balance) > 0 &&
                  !["KRW", "USDT"].includes(r.currency),
              )
              .map((r: Data) => (
                <button
                  key={r.currency}
                  className="outline"
                  disabled={!ready || Number(r.locked) !== 0}
                  onClick={() => onInventory(r)}
                >
                  {r.currency} 전량 반복 그리드 준비
                </button>
              ))}
          </div>
          <p className="fine-print">
            마지막 성공 조회{" "}
            {new Date(snapshot.checked_at).toLocaleString("ko-KR")} · 약
            60초마다 갱신
          </p>
        </>
      ) : (
        <p>
          확인된 계좌 정보가 없습니다. 공개 시세를 이용한 연구와 계좌 조회
          상태는 별도로 표시됩니다.
        </p>
      )}
    </section>
  );
}

function AccountPanel({
  account,
  telegram,
  onInventory,
}: {
  account: Data;
  telegram: Data | undefined;
  onInventory: (asset: Data) => void;
}) {
  const { venue } = useMarket();
  if (isCrypto(venue))
    return <CryptoAccountPanel account={account} onInventory={onInventory} />;
  const snapshot = account.snapshot;
  const ready = account.status === "CONNECTED" && !account.stale;
  const telegramReady =
    telegram?.data?.status === "CONNECTED" &&
    Date.now() - new Date(`${telegram.updated_at}Z`).getTime() < 120000;
  return (
    <section className="panel account-panel">
      <div className="section-heading">
        <h2>토스 실제 계좌</h2>
        <span className={`badge ${ready ? "success" : "neutral"}`}>
          {ready
            ? account.read_only
              ? "조회 전용 연결"
              : "계좌 연결"
            : "조회 확인 필요"}
        </span>
      </div>
      <p className="fine-print">
        계좌 전체 현황입니다. 아래 봇의 모의 자금·전략 예산과 별도로 표시합니다.
        Telegram {telegramReady ? "연결됨" : "연결 대기"} · /account로 조회할 수
        있습니다.
      </p>
      {account.error && (
        <p className="banner danger">
          조회 실패: {account.error} · 이전 잔액을 현재 잔액으로 사용하지
          않습니다.
        </p>
      )}
      {snapshot ? (
        <>
          <div className="metrics compact">
            <Metric
              label="USD 현금 매수 가능"
              value={formatMoney(snapshot.cash_buying_power.USD, "toss")}
              sub={
                account.stale || !ready
                  ? "마지막 조회 값 · 갱신 필요"
                  : snapshot.account_mask
              }
            />
            <Metric
              label="KRW 현금 매수 가능"
              value={
                Number(snapshot.cash_buying_power.KRW).toLocaleString("ko-KR") +
                "원"
              }
              sub="자동 환전은 실행하지 않습니다"
            />
            <Metric
              label="계좌 미체결 주문"
              value={String(snapshot.open_orders.length) + "건"}
              sub={`미국 수수료율 ${(Number(snapshot.us_commission_rate) * 100).toFixed(4)}%`}
            />
          </div>
          <details>
            <summary>
              보유 종목 {snapshot.holdings.items.length}개 · 계좌 미체결 주문
              보기
            </summary>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>종목</th>
                    <th>수량</th>
                    <th>통화</th>
                    <th>평가금액</th>
                    <th>평가손익</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.holdings.items.map((row: Data) => (
                    <tr key={row.symbol}>
                      <td>
                        {row.symbol} · {row.name}
                      </td>
                      <td>{row.quantity}</td>
                      <td>{row.currency}</td>
                      <td>{row.marketValue?.amount ?? "확인 불가"}</td>
                      <td>{row.profitLoss?.amount ?? "확인 불가"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {snapshot.open_orders.map((row: Data, i: number) => (
              <p key={i}>
                {row.symbol} · {row.side} · {row.quantity}주 ·{" "}
                {row.price ?? "가격 확인 불가"} · {row.status}
              </p>
            ))}
          </details>
          <p className="fine-print">
            마지막 성공 조회{" "}
            {new Date(snapshot.checked_at).toLocaleString("ko-KR")} · 약
            60초마다 갱신
          </p>
        </>
      ) : (
        <p>
          아직 확인된 계좌 데이터가 없습니다. 조회가 성공하면 잔액과 보유 종목이
          표시됩니다.
        </p>
      )}
    </section>
  );
}

function Metric({
  label,
  value,
  sub,
  emphasis,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  emphasis?: boolean;
}) {
  return (
    <div className={`metric ${emphasis ? "emphasis" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {sub && <small>{sub}</small>}
    </div>
  );
}
function RiskRow({
  title,
  amount,
  current,
}: {
  title: string;
  amount: string;
  current: number;
}) {
  const { money } = useMarket();
  return (
    <div className="risk-row">
      <div>
        <span>{title}</span>
        <strong>
          {money(current)} <small>/ {money(amount)}</small>
        </strong>
      </div>
      <div className="progress">
        <i
          style={{
            width: `${Math.min(100, (current / Number(amount || 1)) * 100)}%`,
          }}
        />
      </div>
    </div>
  );
}
function Empty({
  title,
  text,
  button,
  action,
}: {
  title: string;
  text: string;
  button?: string;
  action?: () => void;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon name="strategies" />
      </div>
      <h3>{title}</h3>
      <p>{text}</p>
      {button && (
        <button className="text-button" onClick={action}>
          {button}
          <Icon name="arrow" />
        </button>
      )}
    </div>
  );
}

function InventoryForm({
  asset,
  busy,
  onClose,
  onPrepare,
}: {
  asset: Data;
  busy: boolean;
  onClose: () => void;
  onPrepare: (payload: Data) => void;
}) {
  const { venue } = useMarket();
  const targetVenue: Venue = asset.source ? "upbit_usdt" : venue;
  const currency = marketInfo(targetVenue).currency;
  const maker = targetVenue === "upbit_usdt";
  const [basis, setBasis] = useState(maker ? "near_market" : "average");
  const [grids, setGrids] = useState(5);
  const [step, setStep] = useState(maker ? "0.75" : "2");
  const [allowedCautions, setAllowedCautions] = useState<string[]>([]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form
        className="modal form-modal inventory-form"
        role="dialog"
        aria-modal="true"
        aria-label="보유 전량 반복 그리드 준비"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          onPrepare({
            symbol: `${currency}-${asset.currency}`,
            execution_policy: maker ? "maker_only" : "trigger_limit",
            ...(asset.source
              ? {
                  source_strategy_id: asset.source.id,
                  source_version: asset.source.version,
                  source_approval: asset.source.approval,
                }
              : {}),
            allocation: "all",
            first_sell_basis: basis,
            grids,
            step_percent: step,
            allowed_market_cautions: allowedCautions,
          });
        }}
      >
        <div className="section-heading">
          <h2>보유 {asset.currency}로 반복 그리드</h2>
          <button
            type="button"
            className="close"
            onClick={onClose}
            aria-label="닫기"
          >
            ×
          </button>
        </div>
        <p>
          <strong>
            현재 조회 수량 {asset.balance} {asset.currency} 전량
          </strong>
          을 사용합니다. 준비 시 최신 잔고를 다시 조회하며, 실제 수량과 가격표는
          시작 전에 확인합니다.
        </p>
        <p>
          먼저 매도 → 한 단계 아래에서 같은 수량 재매수 → 원래 매도가에서
          재매도합니다. 각 단계의 매도대금 안에서 반복하고 이익은 {currency}로
          남깁니다.
        </p>
        {asset.source && (
          <p className="banner">
            기존 원화 전략을 종료하고 같은 SOL의 장부를 USDT 초안에 넘깁니다.
            실제 매도·환전은 없으며 기존 원화 현금과 기록은 유지합니다. 새
            전략의 시작은 별도 확인합니다.
          </p>
        )}
        <label>
          첫 매도 기준
          <select value={basis} onChange={(e) => setBasis(e.target.value)}>
            <option value="near_market">현재 매도 호가보다 한 틱 위</option>
            {!maker && (
              <option value="average">평균 매수가와 수수료 이상</option>
            )}
            <option value="market">현재가 한 단계 위</option>
          </select>
        </label>
        <p className="fine-print">
          {basis === "average"
            ? "현재가 한 단계 위와 평균 매수가에 비용을 더한 가격 중 높은 쪽에서 시작합니다."
            : "현재가 기준으로 시작하면 첫 매도에서 기존 보유 손실이 확정될 수 있습니다."}
        </p>
        <details>
          <summary>단계 수와 간격 조정</summary>
          <div className="form-grid">
            <label>
              단계 수
              <input
                type="number"
                min={2}
                max={30}
                required
                value={grids}
                onChange={(e) => setGrids(Number(e.target.value))}
              />
            </label>
            <label>
              단계 간격 · %
              <input
                type="number"
                min="0.5"
                max="10"
                step="0.05"
                required
                value={step}
                onChange={(e) => setStep(e.target.value)}
              />
            </label>
          </div>
        </details>
        <p className="fine-print">
          5단계·{maker ? "0.75" : "2"}%는 조정할 수 있는 시작 예시이며 최적화
          결과가 아닙니다. 주문별 최소 금액·수수료·호가 단위를 확인합니다. 추가
          현금은 필요하지 않습니다.
        </p>
        <p className="fine-print">
          초안은 주문을 시작하지 않습니다.{" "}
          {maker
            ? "시작 후에는 메이커 전용 지정가를 단계별로 미리 올립니다. 리워드는 실제 지급 전 수익에 포함하지 않습니다."
            : "시작 후에는 가격이 도달할 때 한 주문씩 처리합니다."}{" "}
          계좌 수량이 달라지거나 손실 한도에 도달하면 중단합니다.
        </p>
        <MarketCautionFields
          allowed={allowedCautions}
          onChange={setAllowedCautions}
          disabled={busy}
        />
        <div className="card-actions">
          <button type="button" className="outline" onClick={onClose}>
            돌아가기
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "요청 중…" : "최신 잔고로 초안 준비"}
          </button>
        </div>
      </form>
    </div>
  );
}

function StrategyForm({
  mode,
  initial,
  busy,
  onSave,
  onClose,
}: {
  mode: string;
  initial?: Data;
  busy: boolean;
  onSave: (body: Data) => void;
  onClose: () => void;
}) {
  const { venue, currency } = useMarket();
  const [form, setForm] = useState<Data>({
    name: "",
    venue,
    symbol: "",
    kind: "grid",
    budget: marketInfo(venue).budget,
    lower: "",
    upper: "",
    grids: 5,
    spacing: "geometric",
    signal_gate: false,
    timeframe: 15,
    fast: 20,
    slow: 60,
    rsi_period: 14,
    rsi_entry: 30,
    rsi_exit: 55,
    max_spread_bps: "30",
    quote_max_age: 10,
    commission_rate: venue === "upbit_usdt" ? "0.0025" : "0.001",
    slippage_bps: "10",
    allowed_market_cautions: [],
    ...initial,
  });
  const change = (key: string, value: unknown) =>
    setForm({ ...form, [key]: value });
  const input = (key: string, label: string, props: Data = {}) => (
    <label>
      {label}
      <input
        value={form[key]}
        onChange={(e) =>
          change(
            key,
            props.type === "number" &&
              ![
                "budget",
                "lower",
                "upper",
                "commission_rate",
                "max_spread_bps",
                "slippage_bps",
              ].includes(key)
              ? Number(e.target.value)
              : e.target.value,
          )
        }
        {...props}
      />
    </label>
  );
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form
        className="modal form-modal"
        role="dialog"
        aria-modal="true"
        aria-label="새 전략"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          const { name, ...spec } = form;
          onSave({
            name: name || `${spec.symbol} ${kindName[spec.kind]}`,
            mode,
            spec: {
              ...spec,
              symbol: spec.symbol.toUpperCase(),
              lower: spec.lower || null,
              upper: spec.upper || null,
            },
          });
        }}
      >
        <div className="section-heading">
          <div>
            <span className="eyebrow">NEW STRATEGY</span>
            <h2>새 전략 준비</h2>
          </div>
          <button
            type="button"
            className="close"
            onClick={onClose}
            aria-label="닫기"
          >
            ×
          </button>
        </div>
        <p className="muted">
          저장 후 주문 규모와 설정을 확인하고 실행할 수 있습니다.
        </p>
        <div className="form-grid">
          {input("symbol", isCrypto(venue) ? "코인 거래쌍" : "미국 종목 코드", {
            placeholder: marketInfo(venue).example,
            required: true,
            autoCapitalize: "characters",
          })}
          {input("name", "전략 이름", { placeholder: "선택 사항" })}
          <label>
            전략
            <select
              value={form.kind}
              onChange={(e) => change("kind", e.target.value)}
            >
              <option value="grid">그리드</option>
              <option value="trend">추세 추종</option>
              <option value="rebound">과매도 반등</option>
            </select>
          </label>
          {input("budget", `배정 예산 · ${currency}`, {
            type: "number",
            min: 1,
            max: marketInfo(venue).maxBudget,
            step: ".01",
            required: true,
          })}
        </div>
        {form.kind === "grid" && (
          <>
            <div className="form-section-title">그리드 가격선</div>
            <div className="form-grid">
              {input("lower", `하단 가격 · ${currency}`, {
                type: "number",
                step: ".0001",
                min: ".0001",
                required: true,
              })}
              {input("upper", `상단 가격 · ${currency}`, {
                type: "number",
                step: ".0001",
                min: ".0001",
                required: true,
              })}
              {input("grids", "매수 단계 수", {
                type: "number",
                min: 2,
                max: 30,
              })}
              <label>
                간격
                <select
                  value={form.spacing}
                  onChange={(e) => change("spacing", e.target.value)}
                >
                  <option value="geometric">동일 비율</option>
                  <option value="arithmetic">동일 금액</option>
                </select>
              </label>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={form.signal_gate}
                onChange={(e) => change("signal_gate", e.target.checked)}
              />
              하락 추세에서는 신규 매수 보류
            </label>
            <p className="fine-print">
              가격선마다 같은 예산을 배정합니다. 주식은 최소 1주, 코인은 최소
              5,000원이며 소수점 수량을 사용합니다. 하단 이탈 시 주문을 취소하고
              보유분은 유지합니다.
            </p>
          </>
        )}
        {isCrypto(venue) && (
          <MarketCautionFields
            allowed={form.allowed_market_cautions}
            onChange={(value) => change("allowed_market_cautions", value)}
            disabled={busy}
          />
        )}
        <details>
          <summary>신호와 체결 설정</summary>
          <div className="form-grid">
            {input("timeframe", "신호 봉 · 분", {
              type: "number",
              min: 1,
              max: 240,
            })}
            {input("fast", "단기 이동평균 기간", { type: "number", min: 2 })}
            {input("slow", "장기 이동평균 기간", { type: "number", min: 3 })}
            {input("rsi_period", "RSI 기간", { type: "number", min: 2 })}
            {input("rsi_entry", "RSI 진입 회복선", {
              type: "number",
              min: 5,
              max: 45,
            })}
            {input("rsi_exit", "RSI 매도선", {
              type: "number",
              min: 50,
              max: 90,
            })}
            {input("max_spread_bps", "호가 차이 상한 · bp", {
              type: "number",
              min: 1,
              max: 100,
            })}
            {input("commission_rate", "수수료율 · 0.001 = 0.1%", {
              type: "number",
              step: ".00001",
              min: 0,
              max: ".02",
            })}
            {input("slippage_bps", "백테스트 편도 체결 비용 · bp", {
              type: "number",
              min: 0,
              max: 100,
            })}
          </div>
          <p className="fine-print">
            1bp는 0.01%입니다. 전략 수치는 수익을 보장하는 값이 아닌 비교·검증용
            설정입니다.
          </p>
        </details>
        <div className="card-actions">
          <button className="outline" type="button" onClick={onClose}>
            돌아가기
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "검증 중…" : "검증하고 저장"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Research({
  busy,
  datasets,
  strategies,
  selected,
  jobs,
  act,
  onResult,
  onAdopt,
  commands,
}: {
  commands: Data[];
  busy: boolean;
  datasets: Data[];
  strategies: Data[];
  selected: Data | null;
  jobs: Data[];
  act: (fn: () => Promise<void>) => Promise<void>;
  onResult: (id: string) => Promise<void>;
  onAdopt: (spec: Data) => void;
}) {
  const { money, venue, currency } = useMarket();
  const [strategyId, setStrategyId] = useState(selected?.id || "");
  const [symbol, setSymbol] = useState(
    (new URLSearchParams(window.location.search).get("venue") === venue
      ? new URLSearchParams(window.location.search).get("symbol")
      : "") || marketInfo(venue).example,
  );
  const [budget, setBudget] = useState(marketInfo(venue).budget);
  const [method, setMethod] = useState("exhaustive");
  const [searchSpace, setSearchSpace] = useState("compact");
  const [trials, setTrials] = useState("100");
  const [seed, setSeed] = useState("42");
  const [ddWeight, setDdWeight] = useState("0");
  const [commission, setCommission] = useState("0.001");
  const [slippage, setSlippage] = useState("10");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [fileSymbol, setFileSymbol] = useState(
    (new URLSearchParams(window.location.search).get("venue") === venue
      ? new URLSearchParams(window.location.search).get("symbol")
      : "") || marketInfo(venue).example,
  );
  const [filename, setFilename] = useState("");
  const strategy = strategies.find((s) => s.id === strategyId);
  const run = (action: "backtest" | "suggest") =>
    act(async () => {
      const ticker =
        action === "backtest" ? strategy?.symbol : symbol.toUpperCase();
      const data = datasets.find(
        (d) => d.symbol === ticker && d.interval === "1m",
      );
      if (!data)
        throw new Error(
          "해당 종목의 1분봉 데이터를 먼저 수집하거나 가져오세요.",
        );
      if (action === "backtest" && !strategy)
        throw new Error("검증할 전략을 선택하세요.");
      const start = from ? `${from}T00:00:00Z` : `${data.first}Z`;
      const end = to
        ? `${to}T23:59:59Z`
        : new Date(new Date(`${data.last}Z`).getTime() + 60000).toISOString();
      await api("/backtests", {
        spec:
          action === "backtest"
            ? strategy!.config
            : {
                venue,
                symbol: ticker,
                kind: "trend",
                budget,
                commission_rate: commission,
                slippage_bps: slippage,
              },
        start,
        end,
        action,
        optimization: {
          method,
          space: searchSpace,
          trials: Number(trials),
          seed: Number(seed),
          drawdown_weight: Number(ddWeight),
        },
      });
    });
  const importFile = (file: File) =>
    act(async () => {
      if (!fileSymbol.trim())
        throw new Error("데이터의 종목 심볼을 먼저 입력하세요.");
      const text = await file.text();
      let candles: Data[];
      if (file.name.endsWith(".json")) {
        const parsed = JSON.parse(text);
        candles = Array.isArray(parsed) ? parsed : parsed.candles;
      } else {
        const lines = text.trim().split(/\r?\n/);
        const keys = lines
          .shift()!
          .replace(/^\uFEFF/, "")
          .split(",")
          .map((k) => k.trim());
        candles = lines.map((line) => {
          const cells = line.split(",");
          const row = Object.fromEntries(
            keys.map((k, i) => [k, cells[i]?.trim()]),
          );
          return {
            timestamp: row.timestamp,
            openPrice: row.open,
            highPrice: row.high,
            lowPrice: row.low,
            closePrice: row.close,
            volume: row.volume,
          };
        });
      }
      await api("/datasets/import", {
        venue,
        symbol: fileSymbol.trim().toUpperCase(),
        source: "user",
        candles,
      });
      setFilename(`${file.name} · 가져오기 완료`);
    });
  return (
    <>
      <section className="panel">
        <div className="section-heading">
          <h2>시장 데이터</h2>
          <span className="badge neutral">수정하지 않은 가격</span>
        </div>
        <div className="data-tools">
          <label>
            종목
            <input
              value={fileSymbol}
              onChange={(e) => setFileSymbol(e.target.value)}
              placeholder={marketInfo(venue).example}
            />
          </label>
          <label>
            수집 시작일
            <input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </label>
          <button
            className="outline"
            onClick={() =>
              act(async () => {
                if (!fileSymbol || !from)
                  throw new Error("종목과 시작일을 입력하세요.");
                await api("/commands", {
                  id: crypto.randomUUID(),
                  action: "ingest",
                  payload: {
                    venue,
                    symbol: fileSymbol.toUpperCase(),
                    from: `${from}T00:00:00Z`,
                    interval: "1m",
                  },
                });
              })
            }
          >
            {isCrypto(venue) ? "업비트" : "토스"}에서 수집 요청
          </button>
          <label className="outline file-button">
            CSV·JSON 가져오기
            <input
              type="file"
              accept=".csv,.json"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) importFile(file);
              }}
            />
          </label>
        </div>
        <div className="ingest-status" aria-live="polite">
          {commands
            .filter(
              (c) =>
                c.action === "ingest" && (c.payload.venue || "toss") === venue,
            )
            .slice(0, 3)
            .map((c) => (
              <p key={c.id}>
                <strong>
                  {c.payload.symbol} · {labels[c.status] || c.status}
                </strong>{" "}
                · {c.result.count || 0}봉 수집{" "}
                {c.result.message && `· ${c.result.message}`}
              </p>
            ))}
        </div>
        <p className="fine-print">
          CSV 헤더: timestamp,open,high,low,close,volume · 시각은 시간대가
          포함된 ISO 8601 형식 · 한 번에 최대 20,000봉
          {filename && ` · ${filename}`}
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>종목</th>
                <th>출처</th>
                <th>봉 수</th>
                <th>시작</th>
                <th>종료</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d, i) => (
                <tr key={i}>
                  <td>
                    <strong>{d.symbol}</strong> · {d.interval}
                  </td>
                  <td>
                    {d.source === "synthetic"
                      ? "가상 예제"
                      : d.source === "toss"
                        ? "토스증권"
                        : "사용자 파일"}
                  </td>
                  <td>{d.count.toLocaleString()}</td>
                  <td>{when(d.first)}</td>
                  <td>{when(d.last)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!datasets.length && (
            <Empty
              title="아직 저장된 시세가 없습니다"
              text="위에서 시세 수집을 요청하거나, 사용 권한이 있는 시세 파일을 가져오세요."
            />
          )}
        </div>
      </section>
      <div className="two-columns">
        <section className="panel">
          <div className="section-heading">
            <h2>전략 검증</h2>
            <span className="badge neutral">1분봉 기반</span>
          </div>
          <label>
            검증할 전략
            <select
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
            >
              <option value="">전략 선택</option>
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.symbol} · {s.name}
                </option>
              ))}
            </select>
          </label>
          <div className="form-grid">
            <label>
              시작일 · UTC
              <input
                type="date"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              />
            </label>
            <label>
              종료일 · UTC
              <input
                type="date"
                value={to}
                onChange={(e) => setTo(e.target.value)}
              />
            </label>
          </div>
          <p className="fine-print">
            기간을 비우면 저장된 전체 범위를 사용합니다. 비용·평가손익·최대
            하락폭을 함께 비교합니다.
          </p>
          <button
            className="primary full"
            disabled={busy}
            onClick={() => run("backtest")}
          >
            백테스트 실행
            <Icon name="arrow" />
          </button>
        </section>
        <section className="panel">
          <div className="section-heading">
            <h2>그리드 설정 찾기</h2>
            <span className="badge neutral">
              그리드 · {searchSpace === "compact" ? "168" : "756"}개 조합
            </span>
          </div>
          <div className="form-grid">
            <label>
              {isCrypto(venue) ? "코인 거래쌍" : "미국 종목 코드"}
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="저장된 데이터의 종목"
              />
            </label>
            <label>
              배정 예산 · {currency}
              <input
                type="number"
                min="1"
                max={marketInfo(venue).maxBudget}
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
              />
            </label>
          </div>
          <div className="form-grid">
            <label>
              탐색 기법
              <select
                value={method}
                onChange={(e) => setMethod(e.target.value)}
              >
                <option value="exhaustive">전수 탐색 · 작은 범위에 적합</option>
                <option value="random">무작위 탐색</option>
                <option value="tpe">베이지안 탐색 · TPE</option>
                <option value="nsga2">유전 알고리즘 · NSGA-II</option>
                <option value="compare">
                  3가지 기법 비교 · 무작위 / TPE / NSGA-II
                </option>
              </select>
            </label>
            <label>
              탐색 범위
              <select
                value={searchSpace}
                onChange={(e) => setSearchSpace(e.target.value)}
              >
                <option value="compact">기본 · 단계 수 3 / 5 / 8 / 12</option>
                <option value="wide">확장 · 단계 수 3~20</option>
              </select>
            </label>
            <label>
              기법별 시도 횟수
              <input
                type="number"
                min="20"
                max="1000"
                value={trials}
                disabled={method === "exhaustive"}
                onChange={(e) => setTrials(e.target.value)}
              />
            </label>
            <label>
              난수 시드 · 재현에 사용
              <input
                type="number"
                min="0"
                max="4294967295"
                value={seed}
                disabled={method === "exhaustive"}
                onChange={(e) => setSeed(e.target.value)}
              />
            </label>
            <label>
              선정 기준
              <select
                value={ddWeight}
                onChange={(e) => setDdWeight(e.target.value)}
              >
                <option value="0">비용 후 수익 최대</option>
                <option value="1">수익 − 최대 하락폭</option>
                <option value="2">수익 − 최대 하락폭 × 2</option>
              </select>
            </label>
          </div>
          <p className="fine-print">
            NSGA-II는 좋은 후보를 교차·변이시키고, TPE는 앞선 결과를 참고해 다음
            설정을 고릅니다. 시도 횟수에는 중복·유효하지 않은 조합도 포함합니다.
            비교 모드는 기법별로 같은 횟수를 사용하며, 모든 후보를 합쳐 최종
            검증할 1개를 선정합니다.
          </p>
          <details>
            <summary>비용 가정</summary>
            <div className="form-grid">
              <label>
                수수료율 · 0.001 = 0.1%
                <input
                  type="number"
                  min="0"
                  max="0.02"
                  step="0.00001"
                  value={commission}
                  onChange={(e) => setCommission(e.target.value)}
                />
              </label>
              <label>
                편도 호가 비용 · bp
                <input
                  type="number"
                  min="0"
                  max="15"
                  value={slippage}
                  onChange={(e) => setSlippage(e.target.value)}
                />
              </label>
            </div>
            <p className="fine-print">
              연구에는 입력한 비용 가정을 고정해서 사용합니다. 계좌 조회의 실제
              수수료율과 비교해 입력하세요. 1bp는 0.01%입니다.
            </p>
          </details>
          <p className="fine-print">
            가격 범위 3가지, 선택한 단계 수, 간격 2가지, 매수 신호 조건을
            탐색합니다. 앞 60%에서 선정 기준 상위 5개를 찾고 다음 20%에서 1개를
            선정한 뒤 마지막 20%로 확인합니다. 예산과 손실 한도는 유지하며,
            수익은 비용과 종료 보유분 평가손익을 포함합니다.
          </p>
          <button
            className="primary full"
            disabled={busy}
            onClick={() => run("suggest")}
          >
            그리드 설정 탐색
          </button>
        </section>
      </div>
      <section className="panel">
        <div className="section-heading">
          <h2>검증 기록</h2>
        </div>
        {jobs.map((job) => (
          <div className="job" key={job.id}>
            <div className="section-heading">
              <div>
                <strong>
                  {job.request.spec.symbol} ·{" "}
                  {job.request.action === "suggest"
                    ? job.request.optimizer_version
                      ? "그리드 수익 탐색"
                      : "그리드 설정안"
                    : job.request.action === "revalidate"
                      ? "기간별 재검증"
                      : kindName[job.request.spec.kind]}
                </strong>
                <small>{when(job.created_at)}</small>
              </div>
              <span className="badge neutral">{labels[job.status]}</span>
            </div>
            {["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(job.status) && (
              <div className="search-progress">
                <p>
                  {job.result.progress?.stage || "작업 대기"}{" "}
                  {job.result.progress?.total
                    ? `· ${job.result.progress.completed} / ${job.result.progress.total}`
                    : ""}
                </p>
                <div
                  className="progress"
                  role="progressbar"
                  aria-label="설정 탐색 진행률"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={job.result.progress?.percent || 0}
                >
                  <i
                    style={{ width: `${job.result.progress?.percent || 0}%` }}
                  />
                </div>
                <button
                  className="text-button"
                  disabled={busy || job.status === "CANCEL_REQUESTED"}
                  onClick={() =>
                    act(async () => {
                      await api(`/backtests/${job.id}/cancel`, {});
                    })
                  }
                >
                  탐색·검증 중단
                </button>
              </div>
            )}
            {job.result.message && (
              <p className="danger-text">{job.result.message}</p>
            )}
            {job.result.synthetic && (
              <span className="badge warn">가상 예제 데이터</span>
            )}
            {job.result.profit != null && (
              <div className="result-summary">
                <span>
                  총 손익 <strong>{money(job.result.profit)}</strong>
                </span>
                <span>
                  최대 하락 <strong>{money(job.result.max_drawdown)}</strong>
                </span>
                <span>{job.result.trade_count}회 체결</span>
                <button
                  className="text-button"
                  onClick={() => act(() => onResult(job.id))}
                >
                  결과 자세히 보기 <Icon name="arrow" />
                </button>
              </div>
            )}
            {job.result.optimizer_version && (
              <OptimizationResult result={job.result} onAdopt={onAdopt} />
            )}
            {!job.result.optimizer_version &&
              !job.result.validation_version &&
              job.result.candidates?.map((candidate: Data, i: number) => (
                <div className="candidate" key={i}>
                  <div>
                    <strong>
                      {candidate.spec.grids}단계 ·{" "}
                      {candidate.spec.spacing === "geometric"
                        ? "동일 비율"
                        : "동일 금액"}
                    </strong>
                    <p>
                      {money(candidate.spec.lower)} ~{" "}
                      {money(candidate.spec.upper)} · 검증 손익{" "}
                      {money(candidate.validation.profit)} · 하락{" "}
                      {money(candidate.validation.max_drawdown)}
                    </p>
                    <small>{candidate.note}</small>
                  </div>
                  <button
                    className="outline"
                    onClick={() => onAdopt(candidate.spec)}
                  >
                    초안으로 가져오기
                  </button>
                </div>
              ))}
          </div>
        ))}
        {!jobs.length && (
          <p className="muted">
            검증 결과가 여기에 쌓입니다. 가장 좋은 결과만 골라 실제 성과로
            해석하지 마세요.
          </p>
        )}
      </section>
    </>
  );
}

function OptimizationResult({
  result,
  onAdopt,
}: {
  result: Data;
  onAdopt: (spec: Data) => void;
}) {
  const { money, unit } = useMarket();
  const selected = result.candidates.find(
    (c: Data) => c.id === result.selected_id,
  );
  const winner = result.candidates[0];
  return (
    <div className="optimization-result">
      <p className="search-summary">
        <strong>{result.tested_count}개 설정 비교 완료</strong> · 예산·최소 최소
        주문·왕복 비용 조건으로 {result.invalid_count}개 제외
      </p>
      {result.searches && (
        <>
          <p className="fine-print">
            선정 기준: {result.objective} · 시드 {result.options.seed} · Optuna{" "}
            {result.library.version}
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>탐색 기법</th>
                  <th>시도 / 고유 설정</th>
                  <th>최고 점수 후보 손익</th>
                  <th>최대 하락폭</th>
                  <th>선정 점수</th>
                </tr>
              </thead>
              <tbody>
                {result.searches.map((search: Data) => (
                  <tr key={search.method}>
                    <td>{optimizerNames[search.method]}</td>
                    <td>
                      {search.attempts} / {search.unique_count}
                    </td>
                    <td>{money(search.best_profit)}</td>
                    <td>{money(search.best_drawdown)}</td>
                    <td>{money(search.best_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="fine-print">
            위 표는 탐색 구간 비교입니다. 기법마다 최종 기간을 시험해서 승자를
            고르지 않습니다.
          </p>
        </>
      )}
      <div className="metrics compact">
        <Metric
          label="탐색 구간 선정 점수 1위의 손익"
          value={money(winner.training_profit)}
          sub="앞 60% · 이 순위만으로 채택하지 않음"
        />
        <Metric
          label="선정한 설정의 중간 손익"
          value={money(selected.validation.profit)}
          sub="중간 20% · 후보 선정에 사용"
        />
        <Metric
          label="선정한 설정의 최종 손익"
          value={money(result.holdout.profit)}
          sub="마지막 20% · 설정 선정 후 한 번 확인"
        />
      </div>
      <div
        className={`optimization-assessment ${result.recommendation_ready ? "ready" : ""}`}
      >
        <strong>
          {result.recommendation_ready
            ? "추가 모의 운영을 검토할 후보입니다"
            : "현재 결과로는 추천을 보류합니다"}
        </strong>
        {result.recommendation_reasons.map((reason: string) => (
          <p key={reason}>· {reason}</p>
        ))}
        <p>
          추천 상태와 관계없이 실제 매매는 별도의 설정 확인·시작 승인이
          필요합니다.
        </p>
      </div>
      <div className="selected-configuration">
        <span className="eyebrow">최종 확인한 설정</span>
        <h3>
          {money(selected.spec.lower)} ~ {money(selected.spec.upper)} ·{" "}
          {selected.spec.grids}단계 ·{" "}
          {selected.spec.spacing === "geometric" ? "동일 비율" : "동일 금액"}
        </h3>
        <p>
          {selected.spec.signal_gate
            ? `하락 시 신규 매수 보류 · ${selected.spec.timeframe}분 봉 · EMA ${selected.spec.fast}/${selected.spec.slow}`
            : "매수 신호 필터 사용 안 함"}
        </p>
        <p>
          최종 구간 최대 하락 {money(result.holdout.max_drawdown)} · 비용{" "}
          {money(result.holdout.costs)} · 완료 매수·매도{" "}
          {result.holdout.completed_cycles}회
        </p>
        <p>
          종료 보유 {Number(result.holdout.held_quantity)}
          {unit} · 평가손익 {money(result.holdout.unrealized)} · 단순 보유 손익{" "}
          {money(
            Number(result.holdout.buy_hold_equity) -
              Number(selected.spec.budget),
          )}
        </p>
        <button className="outline" onClick={() => onAdopt(selected.spec)}>
          {result.synthetic
            ? "가상 예제 설정을 초안으로 가져오기"
            : "검토용 초안으로 가져오기"}
        </button>
      </div>
      <details>
        <summary>상위 5개 설정 비교</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>탐색 순위</th>
                <th>설정</th>
                <th>탐색 총손익</th>
                <th>중간 총손익</th>
                <th>중간 최대 하락</th>
                <th>선정 판정</th>
              </tr>
            </thead>
            <tbody>
              {result.candidates.map((c: Data, i: number) => (
                <tr key={c.id}>
                  <td>
                    {i + 1}
                    {c.id === result.selected_id && (
                      <small>최종 확인 대상</small>
                    )}
                  </td>
                  <td>
                    {c.spec.grids}단계 ·{" "}
                    {c.spec.spacing === "geometric" ? "동일 비율" : "동일 금액"}
                    <small>
                      {money(c.spec.lower)} ~ {money(c.spec.upper)}
                      <br />
                      {c.spec.signal_gate
                        ? `${c.spec.timeframe}분 EMA ${c.spec.fast}/${c.spec.slow}`
                        : "신호 필터 없음"}
                    </small>
                  </td>
                  <td>
                    {money(c.training_profit)}
                    <small>
                      완료 매수·매도 {c.training.completed_cycles}회
                    </small>
                  </td>
                  <td>
                    {money(c.validation.profit)}
                    <small>
                      완료 매수·매도 {c.validation.completed_cycles}회
                    </small>
                  </td>
                  <td>{money(c.validation.max_drawdown)}</td>
                  <td>
                    {c.selection_failures.length ? (
                      c.selection_failures.map((reason: string) => (
                        <small key={reason}>{reason}</small>
                      ))
                    ) : (
                      <small>중간 검증 통과</small>
                    )}
                    <button
                      className="text-button"
                      onClick={() => onAdopt(c.spec)}
                    >
                      검토용 초안
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <details>
        <summary>사용 기간·비용·판정 기준</summary>
        {result.segments.map((s: Data) => (
          <p key={s.name}>
            {s.name}: {when(s.first)} ~ {when(s.last)} ·{" "}
            {s.count.toLocaleString()}봉
          </p>
        ))}
        <p>
          모든 구간에 같은 예산을 독립 배정합니다. 세 구간의 손익을 단순 합산한
          연속 운용 결과가 아닙니다.
        </p>
        <p>
          수수료율 {Number(result.assumptions.commission_rate) * 100}% · 편도
          호가 비용 {result.assumptions.slippage_bps}bp · 하루 손실{" "}
          {money(result.assumptions.daily_loss)} · 고점 대비{" "}
          {money(result.assumptions.drawdown)}
        </p>
        <p>
          각 구간의 비용 후 총손익 양수·완료 매수와 매도 3회 이상·중단 기준
          미도달, 실제 데이터 20일분 이상을 모두 충족해야 추천 후보로
          표시합니다.
        </p>
        {result.limitations.map((line: string) => (
          <p key={line}>· {line}</p>
        ))}
        <small className="mono">데이터 확인값 {result.data_hash}</small>
      </details>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  window.location.pathname === "/guide" ? <Guide /> : <TradingApp />,
);
