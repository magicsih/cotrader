import { marketInfo, Venue } from "./Market";

type Metrics = Record<
  "total_net" | "realized_net" | "unrealized" | "realized_gross" | "costs",
  string | null
>;
export type ProfitReport = {
  mode: string;
  complete: boolean;
  total: Metrics;
  issues: string[];
  markets: {
    venue: Venue;
    label: string;
    status: string;
    status_label: string;
    checked_at: string | null;
    metrics: Metrics;
    converted: Metrics | null;
  }[];
  fx: {
    currency: string;
    rate: string | null;
    source: string | null;
    checked_at: string | null;
    valid_until: string | null;
  }[];
};

const money = (
  value: string | null | undefined,
  venue: Venue = "upbit",
  showUnit = true,
) => {
  if (value == null) return "확인 불가";
  const unit = marketInfo(venue).currency;
  return (
    new Intl.NumberFormat("ko-KR", {
      maximumFractionDigits: unit === "KRW" ? 0 : unit === "USD" ? 2 : 4,
    }).format(Number(value)) + (showUnit ? " " + unit : "")
  );
};
const when = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) +
      " KST"
    : "확인 불가";
const fields: [string, keyof Metrics][] = [
  ["비용 후 총손익", "total_net"],
  ["비용 후 실현손익", "realized_net"],
  ["평가손익", "unrealized"],
  ["누적 비용", "costs"],
];

export function ProfitPanel({ report }: { report: ProfitReport | null }) {
  return (
    <section className="panel profit-panel">
      <div className="section-heading">
        <div>
          <h2>전체 마켓 운용 수익</h2>
          <p>
            {report?.mode === "paper" ? "모의 운용" : "실거래"} · 운용 시작 이후
            누적 · 원화 환산
          </p>
        </div>
        <span className={`badge ${report?.complete ? "success" : "warn"}`}>
          {report?.complete ? "집계 완료" : "집계 확인 필요"}
        </span>
      </div>
      <div className="metrics">
        {fields.map(([label, key]) => (
          <div className="metric" key={key}>
            <span>{label}</span>
            <strong
              data-trend={
                key === "costs" ||
                report?.total[key] == null ||
                Number(report.total[key]) === 0
                  ? "neutral"
                  : Number(report?.total[key]) < 0
                    ? "down"
                    : "up"
              }
            >
              {money(report?.total[key], "upbit", false)}
            </strong>
            <small>KRW</small>
          </div>
        ))}
      </div>
      {report?.issues.length ? (
        <p role="status" className="profit-notice">
          합산 확인 필요: {report.issues.join(" / ")}
        </p>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>마켓</th>
              <th>원래 통화 순손익</th>
              <th>원화 환산</th>
              <th>집계 상태</th>
            </tr>
          </thead>
          <tbody>
            {report?.markets.map((market) => (
              <tr key={market.venue}>
                <td>{market.label}</td>
                <td>{money(market.metrics.total_net, market.venue)}</td>
                <td>{money(market.converted?.total_net)}</td>
                <td>{market.status_label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report?.markets.map((market) => (
        <details key={market.venue}>
          <summary>{market.label} · 손익 상세</summary>
          <dl className="profit-breakdown">
            {(
              [["실현손익 · 비용 전", "realized_gross"], ...fields] as [
                string,
                keyof Metrics,
              ][]
            ).map(([label, key]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{money(market.metrics[key], market.venue)}</dd>
              </div>
            ))}
          </dl>
          <p>
            집계 {when(market.checked_at)} · 갱신 필요 시 마지막 기록을
            표시합니다.
          </p>
        </details>
      ))}
      <details>
        <summary>환산 기준과 적용 환율</summary>
        <p>
          누적 손익을 현재 참고 환율로 환산합니다. 보유 기간의 환차손익이나 실제
          환전 비용을 계산한 값은 아닙니다. USD와 USDT를 1:1로 취급하지
          않습니다.
        </p>
        {report?.fx.map((rate) => (
          <p key={rate.currency}>
            {rate.source || `${rate.currency}/KRW`} · 1 {rate.currency} ={" "}
            {money(rate.rate)}
            <br />
            조회 {when(rate.checked_at)} · 유효 종료 {when(rate.valid_until)}
          </p>
        ))}
      </details>
      <p>
        순손익 = 실현손익 − 매수·매도 전체 기록 비용 + 평가손익. 보유 편입 당시
        평가 기준가부터 계산하며 계좌 전체 손익·입출금·배당·리워드는 포함하지
        않습니다. 실거래와 모의 운용은 별도로 집계합니다.
      </p>
    </section>
  );
}
