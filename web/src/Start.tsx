import { useState } from "react";
import { useMarket } from "./Market";

type Data = Record<string, any>;
const kinds: Data = {
  grid: "그리드",
  trend: "추세 추종",
  rebound: "과매도 반등",
};
const states: Data = {
  COLLECTING: "시세 수집 중",
  RUNNING: "전략 비교 중",
  CANCEL_REQUESTED: "중단 처리 중",
  SUCCEEDED: "탐색 완료",
  FAILED: "탐색 실패",
  CANCELED: "중단됨",
  QUEUED: "대기",
  REJECTED: "수집 실패",
};
const time = (value: string) =>
  new Date(
    /[Z]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
  ).toLocaleString("ko-KR");
export function Start({
  data,
  api,
  act,
  busy,
  onDraft,
  onAdvanced,
}: {
  data: Data;
  api: (path: string, body?: unknown) => Promise<any>;
  act: (work: () => Promise<void>) => void;
  busy: boolean;
  onDraft: () => void;
  onAdvanced: () => void;
}) {
  const { venue, currency, money } = useMarket();
  const [budget, setBudget] = useState(
    data.plan?.request.budget ?? data.defaults.budget,
  );
  const [preference, setPreference] = useState(
    data.plan?.request.preference ?? "drawdown",
  );
  const [daily, setDaily] = useState(data.plan?.enabled ?? false);
  const [history, setHistory] = useState("");
  const active = data.runs.find((r: Data) =>
    ["COLLECTING", "RUNNING", "CANCEL_REQUESTED"].includes(r.status),
  );
  const run = data.runs.find((r: Data) => r.id === history) ?? data.runs[0];
  const result = run?.result;
  const selected = result?.selected;
  const spec = selected?.spec;
  const ready = result?.recommendation_ready;
  const start = () =>
    act(async () => {
      const created = await api("/discoveries", {
        venue,
        budget,
        preference,
        frequency: daily ? "daily" : "once",
      });
      setHistory(created.id);
    });
  return (
    <div className="easy-start">
      <section className="panel start-intro">
        <div>
          <span className="eyebrow">종목과 가격은 함께 찾아드립니다</span>
          <h2>
            예산만 정하고
            <br />첫 전략을 찾아보세요.
          </h2>
          <p>
            실제 시세 수집부터 종목·전략 비교, 그리드 가격 계산까지 서버가
            진행합니다. 창을 닫아도 계속됩니다.
          </p>
          <div className="start-universe">
            시작 후보{" "}
            {data.defaults.universe.map((u: Data) => (
              <span key={u.symbol}>
                <strong>{u.symbol}</strong> {u.name}
              </span>
            ))}
          </div>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            start();
          }}
        >
          <label htmlFor="discovery-budget">모의 예산 · {currency}</label>
          <input
            id="discovery-budget"
            type="number"
            required
            min={venue === "upbit" ? 5000 : 1}
            max={data.defaults.budget}
            step="any"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
          <p className="fine-print">
            가상 자금입니다. 입금이나 계좌 잔액 설정은 필요 없습니다.
          </p>
          <details className="start-options">
            <summary>탐색 방식과 반복 주기</summary>
            <label>
              비교 기준
              <select
                value={preference}
                onChange={(e) => setPreference(e.target.value)}
              >
                <option value="drawdown">하락폭을 더 엄격하게 평가</option>
                <option value="balanced">손익과 하락폭을 함께 평가</option>
              </select>
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={daily}
                onChange={(e) => setDaily(e.target.checked)}
              />
              매일 한 번 다시 탐색
            </label>
            <p className="fine-print">
              새 결과를 저장하고 텔레그램으로 알립니다. 기존 전략은 바뀌지
              않습니다.
            </p>
          </details>
          <button className="primary start-button" disabled={busy || !!active}>
            {" "}
            {active ? "서버에서 탐색 중" : "내 첫 전략 찾아보기 →"}
          </button>
          <p className="fine-print">
            최근 {data.defaults.lookback_days}일 · 세 가지 전략 비교 · 수집과
            계산에 수 분~30분 이상 걸릴 수 있습니다.
          </p>
        </form>
      </section>
      <div className="start-steps">
        <span>
          <b>1</b> 실제 시세 수집
        </span>
        <span>
          <b>2</b> 종목·설정 비교
        </span>
        <span>
          <b>3</b> 마지막 기간 확인
        </span>
        <span>
          <b>4</b> 모의 초안 저장
        </span>
      </div>
      {data.plan?.enabled && (
        <div className="banner">
          <div>
            <strong>매일 탐색 켜짐</strong>
            <p>
              다음 예정: {time(data.plan.next_run_at)} · 이전 30일 데이터와
              겹치므로 독립적인 재검증은 아닙니다.
            </p>
          </div>
          <button
            className="outline"
            disabled={busy}
            onClick={() =>
              act(async () => {
                await api(`/discoveries/schedule/stop?venue=${venue}`, {});
                setDaily(false);
              })
            }
          >
            매일 탐색 끄기
          </button>
        </div>
      )}
      {active && (
        <section className="panel start-progress" aria-live="polite">
          <h2>{active.progress.stage || states[active.status]}</h2>
          {active.status !== "COLLECTING" && (
            <progress
              max="100"
              value={active.progress.percent ?? 0}
              aria-label="전략 탐색 진행률"
            />
          )}
          {active.progress.collections?.map((c: Data) => (
            <div className="collection-row" key={c.symbol}>
              <strong>{c.symbol}</strong>
              <span>
                {states[c.status] || c.status} · 신규 {c.count.toLocaleString()}
                봉
              </span>
              {c.message && <p>{c.message}</p>}
            </div>
          ))}
          <button
            className="outline"
            disabled={busy || active.status === "CANCEL_REQUESTED"}
            onClick={() =>
              act(async () => {
                await api(`/discoveries/${active.id}/cancel`, {});
              })
            }
          >
            이번 탐색 중단
          </button>
        </section>
      )}
      {run && (
        <section className="panel start-result">
          <div className="section-heading">
            <div>
              <span className="eyebrow">내 탐색 기록</span>
              <h2>
                {selected ? "이번에 선정한 한 가지 설정" : states[run.status]}
              </h2>
            </div>
            <label>
              기록 선택
              <select
                aria-label="탐색 기록 선택"
                value={run.id}
                onChange={(e) => setHistory(e.target.value)}
              >
                {data.runs.map((r: Data) => (
                  <option key={r.id} value={r.id}>
                    {time(r.created_at)} · {states[r.status]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {result?.message && <p role="status">{result.message}</p>}
          {selected && (
            <>
              <div className="start-pick">
                <div>
                  <span className={`badge ${ready ? "green" : "amber"}`}>
                    {ready ? "모의 운용 검토 가능" : "추천 보류"}
                  </span>
                  <h3>
                    {spec.symbol} <span>· {kinds[spec.kind]}</span>
                  </h3>
                  {spec.kind === "grid" ? (
                    <p>
                      자동 계산 가격 범위{" "}
                      <strong>
                        {money(spec.lower)} ~ {money(spec.upper)}
                      </strong>{" "}
                      · {spec.grids}단계
                    </p>
                  ) : (
                    <p>
                      {spec.timeframe}분봉 · 이동평균 {spec.fast} / {spec.slow}
                      {spec.kind === "rebound" &&
                        ` · RSI ${spec.rsi_period}, 진입 ${spec.rsi_entry}, 청산 ${spec.rsi_exit}`}
                    </p>
                  )}
                  <p>
                    모의 예산 {money(spec.budget)} · 비교를 통과하지 못한
                    경우에는 실행보다 추가 검증을 먼저 하세요.
                  </p>
                </div>
                <button
                  className={ready ? "primary" : "outline"}
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/discoveries/${run.id}/draft`, {});
                      onDraft();
                    })
                  }
                >
                  {run.strategy_id
                    ? "저장한 모의 초안 보기 →"
                    : ready
                      ? "모의 전략으로 저장 →"
                      : "연구용 초안 저장"}
                </button>
              </div>
              <h3>설정을 고른 뒤 마지막 20%에서 확인한 결과</h3>
              <div className="start-metrics">
                <div>
                  <span>비용 후 총손익</span>
                  <strong>{money(selected.final.profit)}</strong>
                </div>
                <div>
                  <span>최대 하락폭</span>
                  <strong>{money(selected.final.max_drawdown)}</strong>
                </div>
                <div>
                  <span>매수·매도 완료</span>
                  <strong>{selected.final.completed_cycles}회</strong>
                </div>
              </div>
              {!ready && (
                <div className="start-reasons">
                  <strong>지금 추천하지 않는 이유</strong>
                  <ul>
                    {result.recommendation_reasons.map((r: string) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p>
                저장은 모의 초안만 만듭니다. ‘내 전략’에서 설정을 확인하고
                시작을 눌러야 모의매매가 실행됩니다.
              </p>
              <details>
                <summary>선정 근거와 다른 후보 보기</summary>
                <p>{result.selection_rule}</p>
                <p>
                  총 {result.compared_count}개 후보를 비교했습니다. 아래는 중간
                  검증 기간의 상위 후보이며, 마지막 기간은 선정한 한 가지 설정만
                  확인했습니다.
                </p>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>종목</th>
                        <th>전략</th>
                        <th>중간 기간 손익</th>
                        <th>최대 하락폭</th>
                        <th>조건 충족</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.candidates.map((c: Data) => (
                        <tr key={c.id}>
                          <td>
                            {c.spec.symbol}
                            {c.id === selected.id && " · 선정"}
                          </td>
                          <td>{kinds[c.spec.kind]}</td>
                          <td>{money(c.validation.profit)}</td>
                          <td>{money(c.validation.max_drawdown)}</td>
                          <td>
                            {c.selection_failures.length ? "미충족" : "충족"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {result.datasets.map((d: Data) => (
                  <p key={d.symbol}>
                    {d.symbol}: {d.count.toLocaleString()}봉 · {time(d.first)} ~{" "}
                    {time(d.last)} · {d.sources.join(", ")}
                  </p>
                ))}
                {result.excluded.map((d: Data, i: number) => (
                  <p key={i}>
                    제외: {d.symbol} {d.kind && kinds[d.kind]} · {d.reason}
                  </p>
                ))}
                <p>
                  편도 수수료 {Number(result.assumptions.commission_rate) * 100}
                  % · 편도 호가 비용{" "}
                  {Number(result.assumptions.slippage_bps) / 100}% · 하루 손실{" "}
                  {money(result.assumptions.daily_loss)}, 최대 하락폭{" "}
                  {money(result.assumptions.drawdown)} 도달 시 중단
                </p>
                <ul>
                  {result.limitations.map((l: string) => (
                    <li key={l}>{l}</li>
                  ))}
                </ul>
              </details>
            </>
          )}
        </section>
      )}
      <section className="start-footer">
        <p>
          시작용 후보 안에서 찾는 검증 결과입니다. 앞으로의 수익을 보장하지
          않으며, 조건을 만족하는 전략이 없을 수도 있습니다.
        </p>
        <button className="outline" onClick={onAdvanced}>
          종목·기간을 직접 골라 연구하기 →
        </button>
        <a href="/guide">사용 가이드</a>
      </section>
    </div>
  );
}
