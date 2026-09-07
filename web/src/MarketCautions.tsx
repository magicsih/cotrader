import { useId, useState } from "react";

const cautions = [
  ["PRICE_FLUCTUATIONS", "가격 급등락"],
  ["TRADING_VOLUME_SOARING", "거래량 급등"],
  ["DEPOSIT_AMOUNT_SOARING", "입금량 급등"],
  ["GLOBAL_PRICE_DIFFERENCES", "글로벌 가격 차이"],
  ["CONCENTRATION_OF_SMALL_ACCOUNTS", "소수 계정 거래 집중"],
] as const;

export function MarketCautionSummary({ allowed = [] }: { allowed?: string[] }) {
  const names = cautions
    .filter(([key]) => allowed.includes(key))
    .map(([, name]) => name);
  return (
    <p className={names.length ? "banner" : "fine-print"}>
      {names.length
        ? `주의가 발생해도 거래 허용: ${names.join(" · ")}`
        : "주의 발생 시 모든 항목 차단"}
    </p>
  );
}

export function MarketCautionFields({
  allowed,
  onChange,
  disabled = false,
}: {
  allowed: string[];
  onChange: (value: string[]) => void;
  disabled?: boolean;
}) {
  const description = useId();
  return (
    <fieldset
      className="market-cautions"
      disabled={disabled}
      aria-describedby={description}
    >
      <legend>업비트 주의 항목별 거래 허용</legend>
      <p id={description} className="fine-print">
        켠 항목은 주의가 발생해도 이 전략의 거래를 허용합니다. 기본값은 모두
        차단이며, 거래유의 지정과 상태 확인 실패는 항상 차단합니다.
      </p>
      {cautions.map(([key, label]) => {
        const enabled = allowed.includes(key);
        return (
          <div className="caution-row" key={key}>
            <span>{label} 주의</span>
            <button
              type="button"
              role="switch"
              aria-label={`${label} 주의 시 거래 허용`}
              aria-checked={enabled}
              className="caution-switch"
              onClick={() =>
                onChange(
                  enabled
                    ? allowed.filter((item) => item !== key)
                    : [...allowed, key],
                )
              }
            >
              <span aria-hidden="true" className="switch-track">
                <i />
              </span>
              {enabled ? "허용" : "차단"}
            </button>
          </div>
        );
      })}
    </fieldset>
  );
}

export function MarketCautionsForm({
  symbol,
  allowed = [],
  busy,
  onSave,
  onClose,
}: {
  symbol: string;
  allowed?: string[];
  busy: boolean;
  onSave: (value: string[]) => void;
  onClose: () => void;
}) {
  const [selection, setSelection] = useState(allowed);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form
        className="modal form-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${symbol} 주의 설정`}
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          onSave(selection);
        }}
      >
        <div className="section-heading">
          <h2>{symbol} 주의 설정</h2>
          <button
            type="button"
            className="close"
            onClick={onClose}
            aria-label="닫기"
          >
            ×
          </button>
        </div>
        <MarketCautionFields
          allowed={selection}
          onChange={setSelection}
          disabled={busy}
        />
        <p className="fine-print">
          저장 후 설정 확인·시작에서 다시 확인합니다. 설정 저장만으로 주문을
          시작하지 않습니다.
        </p>
        <div className="card-actions">
          <button type="button" className="outline" onClick={onClose}>
            돌아가기
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "저장 요청 중…" : "주의 설정 저장"}
          </button>
        </div>
      </form>
    </div>
  );
}
