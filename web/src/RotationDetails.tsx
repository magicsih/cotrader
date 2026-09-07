export default function RotationDetails({
  state,
  data,
}: {
  state?: Record<string, any>;
  data?: Record<string, any> | null;
}) {
  const positions = Object.entries(state?.positions || {}).filter(
    ([, value]) => Number((value as Record<string, any>).quantity) > 0,
  );
  return (
    <div className="rotation-details">
      <p>
        <strong>ETF 7종 중 상위 2개 · 각 50%</strong>
      </p>
      <p>SPY · QQQ · IWM · IEF · TLT · GLD · SHY</p>
      {!state?.funded && data?.reference_targets && (
        <div className="rotation-reference">
          <strong>신규 시작 참고 배정 · {data.last_session} 종가 기준</strong>
          <p>
            {Object.keys(data.reference_targets).length
              ? Object.entries(data.reference_targets)
                  .map(
                    ([symbol, weight]) => `${symbol} ${Number(weight) * 100}%`,
                  )
                  .join(" · ")
              : "양수 후보 없음 · USD 현금 100%"}
          </p>
          <small>
            실제 보유분이 아닙니다. 시작 시 최신 자료로 다시 판단합니다.
          </small>
        </div>
      )}
      <p>
        최근 252거래일 배당 포함 수익률로 선정합니다. 음수 후보 몫은 USD
        현금으로 유지합니다.
      </p>
      <p>
        매월 첫 거래일 종가로 선정하고 다음 정규장에 교체합니다. 목표와 5%p 이상
        벌어진 비중은 일별로 조절합니다.
      </p>
      <p className="fine-print">
        최초 진입은 직전 완성 정규장 기준입니다. 매도 체결 확인 후 매수하며,
        미국 정규장에만 주문합니다. 두 주식 ETF가 함께 선정될 수도 있습니다.
      </p>
      {positions.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>보유 ETF</th>
              <th>수량</th>
            </tr>
          </thead>
          <tbody>
            {positions.map(([symbol, value]) => (
              <tr key={symbol}>
                <td>{symbol}</td>
                <td>{Number((value as Record<string, any>).quantity)}주</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {state?.target_date && (
        <p className="muted">최근 선정 기준일 {state.target_date}</p>
      )}
      {data !== undefined && (
        <p className="muted">
          {data
            ? `일봉 확인 ${data.last_session} · ${data.source}`
            : "ETF 7개 일봉 준비 중 · 준비가 끝나야 시작할 수 있습니다."}
        </p>
      )}
      <p className="fine-print">
        배당은 순위 계산에 반영합니다. 실제 배당금은 전략 예산에 자동 편입하지
        않으며, 전략 운용 손익은 배당을 제외한 매매·평가 손익입니다.
      </p>
    </div>
  );
}
