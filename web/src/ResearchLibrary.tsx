import { useState } from "react";

type Data = Record<string, any>;
type Props = {
  jobs: Data[];
  datasets: Data[];
  saved: Data[];
  busy: boolean;
  request: (path: string, body?: unknown) => Promise<any>;
  act: (fn: () => Promise<void>) => Promise<void>;
  money: (value: unknown) => string;
  when: (value: string) => string;
};

export function ResearchLibrary({
  jobs,
  datasets,
  saved,
  busy,
  request,
  act,
  money,
  when,
}: Props) {
  const completed = jobs.filter(
    (j) => j.status === "SUCCEEDED" && j.result.optimizer_version,
  );
  const finished = jobs.filter(
    (j) =>
      j.status === "SUCCEEDED" &&
      (j.result.optimizer_version || j.result.validation_version),
  );
  const [compared, setCompared] = useState<string[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [candidateIds, setCandidateIds] = useState<string[]>([]);
  const [periods, setPeriods] = useState([
    { start: "", end: "" },
    { start: "", end: "" },
  ]);
  const [saveJobId, setSaveJobId] = useState("");
  const [saveCandidateId, setSaveCandidateId] = useState("");
  const [name, setName] = useState("");
  const [notice, setNotice] = useState("");
  const [savedNotice, setSavedNotice] = useState("");
  const source = completed.find((j) => j.id === sourceId);
  const saveJob = finished.find((j) => j.id === saveJobId);
  const compareJobs = completed.filter((j) => compared.includes(j.id));
  const comparisonKey = (j: Data) =>
    JSON.stringify([
      j.result.data_hash,
      j.request.spec.venue || "toss",
      j.request.spec.symbol,
      j.request.spec.budget,
      j.result.assumptions.daily_loss,
      j.result.assumptions.drawdown,
      j.result.assumptions.commission_rate,
      j.result.assumptions.slippage_bps,
    ]);
  const sameConditions = new Set(compareJobs.map(comparisonKey)).size === 1;
  const selectedConfig = (j: Data) =>
    j.result.candidates.find((c: Data) => c.id === j.result.selected_id);
  const label = (j: Data) =>
    `${j.request.spec.symbol} · ${j.result.validation_version ? "기간별 재검증" : j.request.optimization?.method || "전수 탐색"} · ${when(j.created_at)}`;
  const configLabel = (c: Data) =>
    `${c.spec.grids}단계 · ${money(c.spec.lower)}~${money(c.spec.upper)} · ${c.spec.signal_gate ? "신호 필터" : "필터 없음"}`;
  return (
    <>
      <section className="panel research-library">
        <div className="section-heading">
          <div>
            <h2>최적화 결과 비교</h2>
            <p>완료된 탐색을 최대 5개 선택해 같은 조건인지 확인합니다.</p>
          </div>
        </div>
        {completed.length === 0 && (
          <p className="muted">
            최적화를 완료하면 기법과 기간에 따른 차이를 비교할 수 있습니다.
          </p>
        )}
        <div className="choice-list">
          {completed.map((j) => (
            <label key={j.id}>
              <input
                type="checkbox"
                checked={compared.includes(j.id)}
                disabled={!compared.includes(j.id) && compared.length >= 5}
                onChange={(e) =>
                  setCompared(
                    e.target.checked
                      ? [...compared, j.id]
                      : compared.filter((id) => id !== j.id),
                  )
                }
              />
              {label(j)}
            </label>
          ))}
        </div>
        {compareJobs.length >= 2 && (
          <>
            <p className={`banner ${sameConditions ? "" : "danger"}`}>
              {sameConditions
                ? "데이터·종목·예산·비용·손실 한도가 같습니다."
                : "데이터 또는 예산·비용·손실 조건이 다릅니다. 수익 숫자로 기법의 우열을 판정할 수 없습니다."}
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>탐색</th>
                    <th>배정 예산</th>
                    <th>후보 수</th>
                    <th>최종 수익률</th>
                    <th>최종 손익</th>
                    <th>최종 최대 하락</th>
                    <th>판정</th>
                  </tr>
                </thead>
                <tbody>
                  {compareJobs.map((j) => (
                    <tr key={j.id}>
                      <td>
                        {label(j)}
                        <small>{configLabel(selectedConfig(j))}</small>
                      </td>
                      <td>{money(j.request.spec.budget)}</td>
                      <td>{j.result.tested_count}</td>
                      <td>{Number(j.result.holdout.return_pct).toFixed(2)}%</td>
                      <td>{money(j.result.holdout.profit)}</td>
                      <td>{money(j.result.holdout.max_drawdown)}</td>
                      <td>
                        {j.result.recommendation_ready
                          ? "모의 운영 검토"
                          : "추천 보류"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="fine-print">
              비교 후 가장 좋은 결과를 고르면 최종 기간도 선정에 사용한
              셈입니다. 채택 전 새 기간에서 다시 확인하세요.
            </p>
          </>
        )}
      </section>
      <section className="panel research-library">
        <div className="section-heading">
          <div>
            <h2>기간별 재검증</h2>
            <p>탐색 종료 이후의 2~6개 기간에서 설정을 고정해 비교합니다.</p>
          </div>
          <span className="badge neutral">주문 실행 없음</span>
        </div>
        <label>
          원래 최적화 결과
          <select
            value={sourceId}
            onChange={(e) => {
              setSourceId(e.target.value);
              const job = completed.find((j) => j.id === e.target.value);
              setCandidateIds(job ? [job.result.selected_id] : []);
            }}
          >
            <option value="">탐색 결과 선택</option>
            {completed.map((j) => (
              <option key={j.id} value={j.id}>
                {label(j)}
              </option>
            ))}
          </select>
        </label>
        {source && (
          <>
            <p>
              사용 가능한 시작 시각:{" "}
              {new Date(source.request.end).toLocaleString("ko-KR", {
                timeZone: "UTC",
              })}{" "}
              UTC 이후
            </p>
            <p className="fine-print">
              저장된 데이터:{" "}
              {datasets
                .filter(
                  (d) =>
                    d.symbol === source.request.spec.symbol &&
                    d.interval === "1m",
                )
                .map((d) => `${when(d.first)}~${when(d.last)} (${d.source})`)
                .join(" / ") || "없음"}
            </p>
            <div className="choice-list">
              {source.result.candidates.map((c: Data) => (
                <label key={c.id}>
                  <input
                    type="checkbox"
                    checked={candidateIds.includes(c.id)}
                    onChange={(e) =>
                      setCandidateIds(
                        e.target.checked
                          ? [...candidateIds, c.id]
                          : candidateIds.filter((id) => id !== c.id),
                      )
                    }
                  />
                  {configLabel(c)}{" "}
                  {c.id === source.result.selected_id && (
                    <span className="badge neutral">원래 선정한 설정</span>
                  )}
                </label>
              ))}
            </div>
          </>
        )}
        <div className="validation-periods">
          {periods.map((p, i) => (
            <div className="form-grid" key={i}>
              <label>
                기간 {i + 1} 시작일 · UTC
                <input
                  type="date"
                  value={p.start}
                  onChange={(e) =>
                    setPeriods(
                      periods.map((v, n) =>
                        n === i ? { ...v, start: e.target.value } : v,
                      ),
                    )
                  }
                />
              </label>
              <label>
                기간 {i + 1} 종료일 · UTC, 포함
                <input
                  type="date"
                  value={p.end}
                  onChange={(e) =>
                    setPeriods(
                      periods.map((v, n) =>
                        n === i ? { ...v, end: e.target.value } : v,
                      ),
                    )
                  }
                />
              </label>
            </div>
          ))}
        </div>
        <div className="button-row">
          <button
            className="outline"
            disabled={periods.length >= 6}
            onClick={() => setPeriods([...periods, { start: "", end: "" }])}
          >
            기간 추가
          </button>
          <button
            className="outline"
            disabled={periods.length <= 2}
            onClick={() => setPeriods(periods.slice(0, -1))}
          >
            마지막 기간 제거
          </button>
          <button
            className="primary"
            disabled={
              busy ||
              !source ||
              !candidateIds.length ||
              periods.some((p) => !p.start || !p.end)
            }
            onClick={() =>
              act(async () => {
                await request(`/backtests/${sourceId}/revalidate`, {
                  candidate_ids: candidateIds,
                  periods: periods.map((p) => ({
                    start: `${p.start}T00:00:00Z`,
                    end: new Date(
                      Date.parse(`${p.end}T00:00:00Z`) + 86400000,
                    ).toISOString(),
                  })),
                });
                setNotice(
                  "기간별 재검증을 접수했습니다. 결과는 아래에 표시됩니다.",
                );
              })
            }
          >
            고정 설정 재검증
          </button>
        </div>
        <p className="fine-print">
          기간당 최소 300봉, 전체 최대 100,000봉입니다. 각 기간은 같은 예산과 빈
          보유 상태로 시작하며 앞선 데이터는 신호 준비에만 사용합니다.
        </p>
        {notice && (
          <p role="status" className="banner">
            {notice}
          </p>
        )}
      </section>
      {jobs
        .filter((j) => j.result.validation_version && j.status === "SUCCEEDED")
        .map((j) => (
          <section className="panel research-library" key={j.id}>
            <div className="section-heading">
              <h2>{label(j)}</h2>
              <span className="badge neutral">고정 설정</span>
            </div>
            {j.result.synthetic && (
              <p className="banner">
                가상 데이터가 포함되어 있습니다. 연구용 결과로 저장됩니다.
              </p>
            )}
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>설정</th>
                    <th>기간별 평균 수익률</th>
                    <th>가장 큰 하락폭</th>
                    <th>수익이 난 기간</th>
                    <th>판정</th>
                  </tr>
                </thead>
                <tbody>
                  {j.result.candidates.map((c: Data) => (
                    <tr key={c.id}>
                      <td>
                        {configLabel(c)}
                        {c.id === j.result.selected_id && (
                          <small>원래 선정한 설정</small>
                        )}
                      </td>
                      <td>{Number(c.mean_return_pct).toFixed(2)}%</td>
                      <td>{money(c.worst_drawdown)}</td>
                      <td>
                        {c.positive_periods}/{c.periods.length}
                      </td>
                      <td>
                        {c.recommendation_ready
                          ? "추가 모의 운영 검토"
                          : "연구용 · 추천 보류"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {j.result.candidates.map((c: Data) => (
              <details key={c.id}>
                <summary>{configLabel(c)} · 기간별 근거</summary>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>기간</th>
                        <th>비용 후 손익</th>
                        <th>수익률</th>
                        <th>최대 하락</th>
                        <th>완료 매수·매도</th>
                        <th>판정</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.periods.map((p: Data, i: number) => (
                        <tr key={i}>
                          <td>
                            {when(p.requested_start)}~{when(p.requested_end)}
                            <small>종료 시각 미포함 · {p.bar_count}봉</small>
                          </td>
                          <td>{money(p.profit)}</td>
                          <td>{Number(p.return_pct).toFixed(2)}%</td>
                          <td>{money(p.max_drawdown)}</td>
                          <td>{p.completed_cycles}회</td>
                          <td>
                            {p.failures.length
                              ? p.failures.join(" · ")
                              : "최소 기준 충족"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {c.recommendation_reasons.map((reason: string) => (
                  <p key={reason}>· {reason}</p>
                ))}
              </details>
            ))}
            {j.result.limitations.map((line: string) => (
              <p key={line} className="fine-print">
                {line}
              </p>
            ))}
          </section>
        ))}
      <section className="panel research-library">
        <div className="section-heading">
          <div>
            <h2>추천 설정 보관함</h2>
            <p>
              설정·비용 가정·검증 결과를 함께 저장합니다. 추천 보류 결과도
              연구용으로 보관할 수 있습니다.
            </p>
          </div>
        </div>
        <div className="form-grid">
          <label>
            저장할 결과
            <select
              value={saveJobId}
              onChange={(e) => {
                setSaveJobId(e.target.value);
                const job = finished.find((j) => j.id === e.target.value);
                setSaveCandidateId(
                  job
                    ? job.result.candidates.some(
                        (c: Data) => c.id === job.result.selected_id,
                      )
                      ? job.result.selected_id
                      : job.result.candidates[0].id
                    : "",
                );
              }}
            >
              <option value="">결과 선택</option>
              {finished.map((j) => (
                <option value={j.id} key={j.id}>
                  {label(j)}
                </option>
              ))}
            </select>
          </label>
          <label>
            설정
            <select
              value={saveCandidateId}
              onChange={(e) => setSaveCandidateId(e.target.value)}
            >
              <option value="">설정 선택</option>
              {saveJob?.result.candidates.map((c: Data) => (
                <option value={c.id} key={c.id}>
                  {configLabel(c)}
                </option>
              ))}
            </select>
          </label>
          <label>
            보관할 이름
            <input
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
              placeholder="예: ETF 그리드 · 가을 검증"
            />
          </label>
        </div>
        <button
          className="primary"
          disabled={busy || !saveJobId || !saveCandidateId || !name.trim()}
          onClick={() =>
            act(async () => {
              await request("/recommendations", {
                job_id: saveJobId,
                candidate_id: saveCandidateId,
                name,
              });
              setSavedNotice("검증 근거와 설정을 보관함에 저장했습니다.");
            })
          }
        >
          근거와 함께 저장
        </button>
        {savedNotice && (
          <p role="status" className="banner">
            {savedNotice}
          </p>
        )}
        {saved.map((s) => (
          <div className="saved-recommendation" key={s.id}>
            <div>
              <h3>{s.name}</h3>
              <p>
                {s.evidence.spec.symbol} ·{" "}
                {configLabel({ spec: s.evidence.spec })}
              </p>
              <span
                className={`badge ${s.evidence.recommendation_ready ? "success" : "neutral"}`}
              >
                {s.evidence.recommendation_ready
                  ? "모의 운영 검토 후보"
                  : "연구용 · 추천 보류"}
              </span>
              <small>{when(s.created_at)}</small>
            </div>
            <details>
              <summary>저장한 검증 근거</summary>
              {s.evidence.recommendation_reasons.map((reason: string) => (
                <p key={reason}>· {reason}</p>
              ))}
              <p>
                예산 {money(s.evidence.spec.budget)} · 수수료율{" "}
                {Number(s.evidence.spec.commission_rate) * 100}% · 편도 호가
                비용 {s.evidence.spec.slippage_bps}bp
              </p>
              <p>
                하루 손실 {money(s.evidence.assumptions.daily_loss)} · 고점 대비{" "}
                {money(s.evidence.assumptions.drawdown)}
              </p>
              <p className="mono">
                근거 확인값 {s.evidence.fingerprint.slice(0, 24)}
              </p>
            </details>
            <button
              className="outline"
              disabled={busy || !!s.strategy_id}
              onClick={() =>
                act(async () => {
                  await request(`/recommendations/${s.id}/draft`, {});
                  setSavedNotice(
                    "모의 전략 초안을 만들었습니다. 전략 화면에서 설정과 현재 손실 한도를 확인하고 별도로 시작할 수 있습니다.",
                  );
                })
              }
            >
              {s.strategy_id ? "모의 초안 생성됨" : "모의 전략 초안 만들기"}
            </button>
          </div>
        ))}
        {!saved.length && <p className="muted">저장한 설정이 없습니다.</p>}
      </section>
    </>
  );
}
