import { useEffect, useState } from "react";

type Data = Record<string, any>;
const number = (value: unknown, digits = 2) =>
  value == null
    ? "확인 전"
    : Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits });
const time = (value?: string) =>
  value ? new Date(value).toLocaleString("ko-KR") : "확인 전";
const action: Data = { BUY: "매수", SELL: "매도", HOLD: "유지", WAIT: "대기" };

export function PaperLab({ api }: { api: (path: string) => Promise<any> }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      try {
        const next = await api("/api/paper-lab");
        if (!stopped) {
          setData(next);
          setError("");
        }
      } catch {
        if (!stopped)
          setError(
            "모의 운용 상태를 새로 확인하지 못했습니다. 이전 표시를 현재 상태로 판단하지 마세요.",
          );
      }
    };
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [api]);
  return (
    <div className="paper-lab">
      <div className="banner">
        ETF와 암호화폐를 각각 독립된 가상 계좌에서 비교합니다. 같은 시장의
        계좌별 예산은 동일하며 합산 투자금이 아닙니다. 실제 주문은 발생하지
        않습니다.
      </div>
      {error && (
        <div className="banner danger" role="alert">
          {error}
        </div>
      )}
      {data?.runtime &&
        Date.now() - new Date(data.runtime.checked_at).getTime() > 120000 && (
          <div className="banner danger">
            운용 상태 갱신이 2분 이상 멈췄습니다. 아래는 마지막으로 저장된
            판단입니다.
          </div>
        )}
      {data?.runtime?.crypto_data_error && (
        <div className="banner danger">{data.runtime.crypto_data_error}</div>
      )}
      {!data ? (
        <p>모의 계좌 확인 중…</p>
      ) : !data.portfolios.length ? (
        <p>
          모의 실험 시작 전입니다. 예산과 시세 연결이 준비되면 계좌가
          표시됩니다.
        </p>
      ) : (
        <>
          {!data.enabled && (
            <div className="banner danger">
              현재 모의 운용 설정이 꺼져 있습니다. 아래는 저장된 기록입니다.
            </div>
          )}
          {(["toss", "upbit_usdt"] as const).map((venue) => {
            const portfolios: Data[] = data.portfolios.filter(
              (p: Data) => p.venue === venue,
            );
            const evaluation = data.evaluations.find(
              (e: Data) => e.venue === venue,
            );
            const unit = venue === "toss" ? "USD" : "USDT";
            return (
              <section key={venue} className="paper-market">
                <h2>
                  {venue === "toss" ? "미국 ETF" : "암호화폐 · BTC와 SOL"}
                </h2>
                <p>
                  각 계좌 시작 예산 {number(portfolios[0]?.budget)} {unit} ·
                  세전 · 수수료와 체결 비용 차감 · 배당 권리는 별도 누적
                </p>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>비교 계좌</th>
                        <th>현재 판단</th>
                        <th>평가액</th>
                        <th>수익률</th>
                        <th>최대 하락</th>
                        <th>현금</th>
                      </tr>
                    </thead>
                    <tbody>
                      {portfolios.map((p) => (
                        <tr key={p.id}>
                          <td>
                            <a href={`#${p.id}`}>{p.name}</a>
                          </td>
                          <td>{action[p.state.action] || "확인 전"}</td>
                          <td>
                            {number(p.state.nav)} {unit}
                          </td>
                          <td>{number(p.state.return_pct)}%</td>
                          <td>
                            {p.state.max_drawdown == null
                              ? "확인 전"
                              : number(Number(p.state.max_drawdown) * 100) +
                                "%"}
                          </td>
                          <td>{number(p.state.cash)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="banner">
                  {evaluation?.status === "REVIEW"
                    ? "추가 검증할 후보가 있습니다. "
                    : "개선 확인 전 · "}
                  {evaluation?.reason} 다음 정기 검토:{" "}
                  {time(evaluation?.next_review_at)}. 규칙 변경이나 실거래
                  전환은 자동으로 하지 않습니다.
                </div>
                <div className="paper-cards">
                  {portfolios.map((p) => (
                    <article className="paper-card" id={p.id} key={p.id}>
                      <h3>
                        {p.name}{" "}
                        <small>
                          v{p.rules.version} · {p.days}일 관찰
                        </small>
                      </h3>
                      <strong>
                        {action[p.state.action]} · {p.state.reason}
                      </strong>
                      <p>
                        판단 확인 {time(p.state.checked_at)}
                        <br />
                        다음 확인 {time(p.state.next_check_at)}
                        <br />
                        평가 기준 {time(p.state.nav_at)} ·{" "}
                        {p.state.valuation === "previous_close"
                          ? "직전 정규장 종가"
                          : "매수 호가"}
                      </p>
                      <p>
                        {p.rules.variant === "hold"
                          ? "최초 동일 비중 매수 후 수량 유지"
                          : `${p.rules.lookback}${venue === "toss" ? "개월" : "일"} 평균 추세 · 진입과 이탈 여유 폭 ${number(Number(p.rules.buffer) * 100)}%`}
                        <br />
                        수수료 {number(Number(p.rules.fee) * 100)}% + 추가 체결
                        비용 {number(Number(p.rules.execution_cost) * 100)}%
                      </p>
                      <details>
                        <summary>종목별 판단 근거와 보유 수량</summary>
                        <div className="table-scroll">
                          <table>
                            <thead>
                              <tr>
                                <th>종목</th>
                                <th>
                                  {venue === "toss" ? "배당 포함 지수" : "종가"}
                                </th>
                                <th>추세 평균</th>
                                <th>진입 / 이탈 조건</th>
                                <th>목표 비중</th>
                                <th>보유 수량</th>
                              </tr>
                            </thead>
                            <tbody>
                              {p.rules.symbols.map((s: string) => {
                                const c = p.state.conditions?.[s];
                                return (
                                  <tr key={s}>
                                    <td>{s}</td>
                                    <td>{number(c?.value, 4)}</td>
                                    <td>{number(c?.average, 4)}</td>
                                    <td>
                                      {c
                                        ? `${c.enter_pass ? "충족" : "미충족"} / ${c.exit_pass ? "충족" : "미충족"}`
                                        : "확인 전"}
                                    </td>
                                    <td>
                                      {c
                                        ? number(Number(c.target) * 100) + "%"
                                        : "확인 전"}
                                    </td>
                                    <td>
                                      {number(
                                        p.state.positions?.[s]?.quantity || 0,
                                        8,
                                      )}
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                        <p>
                          신호 기준일 {p.state.signal_date || "확인 전"} · 규칙
                          식별자 <code>{p.rules_hash.slice(0, 12)}</code>
                        </p>
                      </details>
                      <details>
                        <summary>
                          최근 모의 체결과 미체결 기록 · 누적 체결{" "}
                          {p.state.fill_count || 0}건
                        </summary>
                        {p.recent.length ? (
                          <ul>
                            {p.recent.map((e: Data, i: number) => (
                              <li key={i}>
                                {time(e.at)} · {e.symbol} ·{" "}
                                {e.kind === "fill"
                                  ? `${action[e.side]} ${number(e.quantity, 8)} · 체결가 ${number(e.price, 6)} · 비용 ${number(Number(e.fee) + Number(e.execution_cost), 4)} ${unit}`
                                  : e.kind === "cancel"
                                    ? e.reason
                                    : `배당 권리 ${number(e.amount)} ${unit}`}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <p>아직 체결 기록이 없습니다.</p>
                        )}
                      </details>
                    </article>
                  ))}
                </div>
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}
