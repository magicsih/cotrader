export function Guide() {
  return (
    <main className="guide-page">
      <header className="guide-header">
        <a className="brand-text" href="/">
          cotrader
        </a>
        <a className="primary" href="/">
          대시보드 열기 →
        </a>
      </header>
      <div className="guide-hero">
        <span className="eyebrow">처음 시작하는 분을 위한 안내</span>
        <h1>
          실제 종목으로
          <br />첫 전략을 검증해보세요.
        </h1>
        <p>
          처음에는 ‘쉽게 시작’에서 시장과 가상 예산만 정하세요. 종목 선택, 시세
          수집, 그리드 가격 계산과 전략 비교는 서버가 진행합니다. 실제 종목의
          시세를 쓰는 모의매매에는 입금이 필요하지 않습니다.
        </p>
      </div>
      <section className="panel guide-step">
        <span className="step-number">START</span>
        <h2>처음이라면 이 세 가지만 하세요</h2>
        <ol>
          <li>
            대시보드 위에서 <strong>토스 미국 주식</strong> 또는{" "}
            <strong>업비트 코인</strong>을 선택합니다.
          </li>
          <li>
            <strong>쉽게 시작</strong>에서 모의 예산을 정하고{" "}
            <strong>내 첫 전략 찾아보기</strong>를 누릅니다.
          </li>
          <li>
            완료되면 마지막 기간의 손익과 추천 보류 사유를 확인합니다.{" "}
            <strong>모의 전략으로 저장</strong> 후 ‘내 전략’에서 시작을 누르면
            모의매매가 실행됩니다.
          </li>
        </ol>
        <p>
          주식은 SPY·QQQ, 코인은 선택한 KRW·USDT 시장의 BTC·ETH 안에서
          비교합니다. 특정 종목의 매수 추천 목록은 아닙니다. 최근 30일의 공통
          시각 데이터를 사용해 그리드·추세 추종·과매도 반등을 비교하며, 그리드
          가격 범위도 자동으로 계산합니다.
        </p>
        <p>
          창을 닫아도 수집과 계산은 계속됩니다. ‘탐색 방식과 반복 주기’에서 매일
          탐색을 직접 켤 수 있고, 완료 결과는 텔레그램으로 알립니다. 모의 초안
          저장과 실제 주문은 자동으로 실행되지 않습니다.
        </p>
        <p>
          ‘추천 보류’는 실패한 화면이 아닙니다. 비용 후 손익, 거래 횟수, 손실
          기준, 관측일 조건을 통과하지 못해 운용을 권하지 않는 결과입니다.
          설정을 자꾸 바꾸기보다 이후의 새 기간으로 재검증하세요.
        </p>
        <div className="guide-examples">
          <a className="primary" href="/?venue=toss">
            주식 쉽게 시작 →
          </a>
          <a className="primary" href="/?venue=upbit">
            코인 쉽게 시작 →
          </a>
        </div>
        <p className="fine-print">
          아래는 직접 종목·기간·최적화 기법을 정하고 싶을 때 사용하는 상세
          가이드입니다.
        </p>
      </section>
      <nav className="guide-toc" aria-label="사용 가이드 목차">
        <a href="#start">1. 시장 선택</a>
        <a href="#data">2. 데이터 수집</a>
        <a href="#optimize">3. 설정 찾기</a>
        <a href="#validate">4. 결과 검증</a>
        <a href="#paper">5. 모의매매</a>
        <a href="#live">6. 실제 주문</a>
        <a href="#usdt-grid">7. USDT 보유 이관</a>
        <a href="#faq">막혔을 때</a>
      </nav>
      <section id="start" className="panel guide-step">
        <span className="step-number">01</span>
        <h2>먼저 시장을 고르세요</h2>
        <p>
          대시보드 위쪽에서 <strong>토스 미국 주식</strong> 또는{" "}
          <strong>업비트 코인</strong>을 선택합니다. 주식은 USD, 코인은 KRW 또는
          USDT로 표시하며 예산·손익·손실 중단 기준을 따로 관리합니다.
        </p>
        <div className="guide-examples">
          <div>
            <h3>미국 주식</h3>
            <p>
              종목 코드 예: AAPL. 일반 미국 주식·일반 ETF를 지원하며 휴장일에는
              모의 주문도 대기합니다.
            </p>
            <a
              className="outline"
              href="/?view=research&venue=toss&symbol=AAPL"
            >
              AAPL 연구 화면 열기 →
            </a>
          </div>
          <div>
            <h3>업비트 원화 코인</h3>
            <p>
              거래쌍 예: KRW-BTC, KRW-ETH. 소수점 수량과 24시간 시세로
              검증합니다. 원화 마켓을 지원합니다.
            </p>
            <a
              className="outline"
              href="/?view=research&venue=upbit&symbol=KRW-BTC"
            >
              KRW-BTC 연구 화면 열기 →
            </a>
          </div>
        </div>
        <p className="fine-print">
          예시는 화면 조작을 위한 종목 코드입니다. 링크는 값을 채울 뿐 데이터
          수집·전략 실행·주문을 시작하지 않습니다.
        </p>
      </section>
      <section id="data" className="panel guide-step">
        <span className="step-number">02</span>
        <h2>전략 연구실에서 실제 데이터를 수집하세요</h2>
        <ol>
          <li>
            <strong>시장 데이터</strong>에 종목 코드와 수집 시작일을 입력합니다.
            처음에는 최근 7일 정도로 연결과 화면 사용을 확인하세요.
          </li>
          <li>
            <strong>토스에서 수집 요청</strong> 또는{" "}
            <strong>업비트에서 수집 요청</strong>을 누릅니다. 시각은 UTC이며
            한국 시각보다 9시간 느립니다.
          </li>
          <li>
            화면의 수집 기록이 <strong>완료</strong>인지, 데이터 표에 출처·봉
            수·처음/마지막 시각이 있는지 확인합니다. 실패했다면 아래 메시지를
            확인합니다.
          </li>
        </ol>
        <p>
          설정 탐색에는 최소 300개 1분봉이 필요합니다. 추천 판단에는 실제 관측일
          20일 이상 등 추가 조건을 적용하므로, 7일 연습 결과는 곧바로 추천되지
          않을 수 있습니다. 한 번의 연구는 최대 100,000봉이므로 긴 기간은 나눠서
          검증하세요.
        </p>
        <p>
          토스가 제공하는 과거 범위와 거래일에 따라 수집량이 달라집니다.
          업비트는 거래가 없던 분의 봉을 제공하지 않습니다. 없는 봉을 만들지
          않으므로 공백과 실제 수집 범위를 꼭 확인하세요.{" "}
          <strong>
            가상 시세가 섞인 결과는 운용 설정으로 추천하지 않습니다.
          </strong>
        </p>
      </section>
      <section id="optimize" className="panel guide-step">
        <span className="step-number">03</span>
        <h2>최적화로 그리드 설정을 찾아보세요</h2>
        <ol>
          <li>
            <strong>그리드 설정 찾기</strong>에 수집한 종목과 시험할 예산을
            입력합니다. 이 예산은 연구용 가상 자금입니다.
          </li>
          <li>
            시작일·종료일을 지정합니다. 비워두면 저장된 전체 기간을 사용합니다.
          </li>
          <li>
            처음에는 <strong>전수 탐색 · 작은 범위</strong>로 시작합니다.
            익숙해지면 무작위 탐색, TPE, NSGA-II 또는 기법 비교를 선택합니다.
          </li>
          <li>
            수수료와 호가 비용을 확인하고 <strong>그리드 설정 탐색</strong>을
            누릅니다. 연구는 서버에서 진행되므로 창을 닫아도 계속됩니다.
          </li>
        </ol>
        <div className="guide-callout">
          <h3>그리드가 무엇인가요?</h3>
          <p>
            하단과 상단 사이에 여러 가격선을 만듭니다. 가격이 아래 선을 통과하면
            사고, 매수한 수량을 다음 위 선에서 팝니다. 계속 하락하면 자산을
            보유한 채 손실이 커질 수 있어 하단 이탈 시 신규 주문을 중단합니다.
          </p>
        </div>
        <p>
          주식은 가격선마다 최소 1주, 업비트는 최소 5,000 KRW 또는 0.5 USDT
          규모가 필요합니다. 단계 수를 늘릴수록 각 가격선의 주문이 작아집니다.
          수수료보다 촘촘한 설정이나 최소 주문 조건을 만족하지 않는 설정은
          제외됩니다.
        </p>
      </section>
      <section id="validate" className="panel guide-step">
        <span className="step-number">04</span>
        <h2>수익 숫자보다 검증 근거를 먼저 보세요</h2>
        <p>
          앞 60%에서 후보를 찾고, 다음 20%에서 하나를 선정한 뒤, 마지막 20%에서
          결과를 확인합니다.{" "}
          <strong>
            마지막 결과를 보고 반복해서 설정을 바꾸면 그 기간도 학습에 쓴 셈
          </strong>
          이 됩니다.
        </p>
        <ul>
          <li>
            <strong>비용 후 총손익</strong>: 팔지 않고 남은 자산의 평가손익까지
            포함합니다.
          </li>
          <li>
            <strong>최대 하락폭</strong>: 중간에 고점 대비 얼마나 손실이 났는지
            봅니다.
          </li>
          <li>
            <strong>매수·매도 완료 횟수</strong>: 한두 번의 운 좋은 거래에
            의존한 결과인지 확인합니다.
          </li>
          <li>
            <strong>단순 보유 비교</strong>: 같은 돈으로 사서 보유했을 때보다
            나았는지 봅니다.
          </li>
        </ul>
        <p>
          <strong>최적화 결과 비교</strong>에서 최대 5개 기록을 비교합니다. 같은
          종목·기간·예산·비용 조건인지 확인하세요. 수익 1등 설정이 앞으로도 가장
          높은 수익을 내는 것은 아닙니다.
        </p>
        <p>
          <strong>기간별 재검증</strong>에는 원래 최적화 종료 이후의 새 기간을
          2~6개 지정합니다. 각 기간은 서로 겹치지 않고 최소 300봉이 있어야
          합니다. 설정은 고정되고 매 기간 같은 초기 예산으로 별도 시험합니다.
          기간 평균은 연속 운용의 복리 수익률이 아닙니다.
        </p>
        <p>
          <strong>추천 설정 보관함</strong>에 근거와 함께 저장합니다. ‘추천
          보류’도 연구용으로 저장할 수 있지만 실전 승인을 의미하지 않습니다.
        </p>
      </section>
      <section id="paper" className="panel guide-step">
        <span className="step-number">05</span>
        <h2>실제 시세로 모의매매를 시작하세요</h2>
        <ol>
          <li>
            보관한 설정에서 <strong>모의 전략 초안 만들기</strong>를 누르거나,{" "}
            <strong>새 전략</strong>에서 값을 입력합니다.
          </li>
          <li>
            <strong>내 전략</strong>에서 예산·그리드 가격선·수량·수수료·손실
            기준을 확인합니다. 저장만으로 실행되지는 않습니다.
          </li>
          <li>
            <strong>설정 확인·시작</strong>에서 확인한 내용을 승인하면 가상
            자금을 배정하고 감시를 시작합니다.
          </li>
          <li>
            <strong>주문과 기록</strong>에서 신호 이유와 모의 체결을 확인합니다.
            처음 가격을 본 즉시 주문하지 않고, 이후 가격선 통과와 새로운 호가를
            기다립니다.
          </li>
        </ol>
        <p>
          모의매매 모드에서는 실제 계좌에 돈을 넣거나 주식을 보유할 필요가
          없습니다. 며칠간 체결·중단·재시작을 확인한 뒤 운용 방식을 판단하세요.
        </p>
        <p>
          <strong>전체 주문 중단</strong>은 모든 시장의 전략을 중단하고 대기
          주문을 정리합니다. 보유 자산은 자동으로 시장가 매도하지 않습니다.
          그리드 하단 이탈이나 손실 한도 도달 시에도 보유분은 남습니다.
        </p>
      </section>
      <section id="live" className="panel guide-step">
        <span className="step-number">06</span>
        <h2>실제 계좌로 자동매매 시작하기</h2>
        <ol>
          <li>
            시장을 선택하고 상단 <strong>실거래</strong>를 선택합니다. 이 버튼은
            화면의 실행 모드를 고르며 주문을 시작하지 않습니다.
          </li>
          <li>
            <strong>새 전략</strong>에서 종목·예산·그리드 가격을 정하고 실거래
            초안으로 저장합니다. 연구 결과와 모의 전략은 자동으로 실거래로
            바뀌지 않습니다.
          </li>
          <li>
            <strong>실거래 준비 점검</strong>을 누릅니다. 계좌 현금·기존
            보유·미체결·수수료를 확인하고, 업비트는 실제 주문을 만들지 않는 주문
            테스트도 실행합니다. 결과는 요청 기록에서 확인합니다.
          </li>
          <li>
            <strong>설정 확인·시작</strong>에서 실제 금액·손실 한도를 확인한 뒤
            요청합니다. 서버가 다시 점검하고 통과하면 신호에 따라 자동으로
            주문합니다. 토스는 거래 세션을, 업비트는 24시간 시세를 따릅니다.
          </li>
          <li>
            중단할 때는 웹의 <strong>전체 주문 중단</strong> 또는 텔레그램{" "}
            <strong>/pause</strong>를 사용합니다. 미체결은 취소 확인을 진행하고
            이미 보유한 자산은 유지합니다.
          </li>
        </ol>
        <p>
          현금으로 시작하는 전략은 토스 USD, 업비트 KRW 또는 USDT 사용 가능
          현금이 전략 예산을 충족해야 합니다. 입금만으로 봇 예산이 늘어나지
          않습니다. 같은 종목의 기존 보유분·수동 주문이 있으면 현금 시작을
          거절합니다. 보유 코인은 아래의 전량 편입 절차를 따릅니다.
        </p>
        <p>
          토스는 당일 지정가, 업비트는 지정가 주문을 사용합니다. 기본 방식은
          종목당 한 주문을 처리하고 60초가 지나면 취소를 요청합니다. USDT 보유
          그리드의 메이커 전용 주문은 단계별로 두며 60초 만료를 적용하지
          않습니다. 업비트 주문 응답이 끊기면 식별자로 조회하며 새 주문으로
          재전송하지 않습니다.
        </p>
        <p>
          준비 점검 통과는 실제 체결이나 수익성 검증 완료를 의미하지 않습니다.
          실거래 잠금은 서버 설정이며, 거래소 점검·권한·잔고 상태에 따라 시작이
          거절될 수 있습니다. 입출금·자동 환전 기능은 제공하지 않습니다.
        </p>
      </section>
      <section id="inventory-grid" className="panel guide-step">
        <h2>보유 코인으로 매도부터 시작하고 반복하기</h2>
        <ol>
          <li>
            업비트 → 포트폴리오 → 실제 계좌에서 원하는 코인의{" "}
            <strong>전량 반복 그리드 준비</strong>를 누릅니다.
          </li>
          <li>
            첫 매도 기준을 고릅니다. 평균 매수가 기준은 비용을 고려하고 현재가
            한 단계 위보다 높은 가격에서 시작합니다. 현재가 기준은 기존 보유
            손실을 확정할 수 있습니다.
          </li>
          <li>
            최신 매도 가능 전량을 기본 5단계로 나눕니다. 원화는 2%, USDT는 0.75%
            간격입니다. 단계 수와 간격은 조정할 수 있으며 이 기본값은 수익
            최적화 결과가 아닙니다.
          </li>
          <li>
            내 전략 → 실거래에서 초안의 <strong>설정 확인·시작</strong>을 누르고
            수량·매도가·재매수가·손실 한도를 확인합니다. 준비 중 오류는 주문과
            기록에 표시됩니다.
          </li>
        </ol>
        <p>
          각 단계는 보유 코인을 먼저 매도하고, 그 매도대금에서 수수료를 뺀 금액
          안에서 한 단계 아래 가격에 재매수합니다. 재매수한 코인은 같은
          매도가에서 다시 매도합니다. 단계별 최대 수량을 유지하고 이익은 선택한
          KRW 또는 USDT로 남습니다. 매도되지 않은 단계의 코인이나 다른 원화
          잔액을 재매수에 사용하지 않습니다.
        </p>
        <p>
          원화 기본 방식은 가격에 도달할 때 종목당 한 주문씩 처리합니다.
          미체결은 60초 후 취소를 요청하며 다음 시세에서 다시 판단합니다. 부분
          체결과 재시작 후에도 단계별 코인·매도대금을 유지합니다.
        </p>
        <p>
          운용 손익은 초안의 평가 기준가부터 계산합니다. 계좌의 과거 평균
          매수가와 기존 손익은 별도입니다. 초안의 수량이 현재 계좌와 다르거나
          평가 기준가가 0.5% 넘게 변했으면 최신 초안을 다시 준비하세요. 오래된
          미시작 초안은 운영 종료로 정리할 수 있습니다.
        </p>
        <p>
          보유 시작 그리드는 가격 범위 아래에서도 재매수와 대기를 이어갑니다.
          하루 손실·고점 대비 손실 한도는 계속 적용되며 중단 시 보유분을
          유지합니다. 이 계좌 편입 초안으로 과거 백테스트는 실행하지 않습니다.
        </p>
      </section>
      <section id="usdt-grid" className="panel guide-step">
        <h2>SOL을 환전 없이 USDT 반복 그리드로 옮기기</h2>
        <ol>
          <li>
            기존 원화 전략에서 중단을 요청하고 미체결·미확인 주문이 없어질
            때까지 기다립니다.
          </li>
          <li>
            원화의 내 전략에서 <strong>USDT로 보유 이관·초안 준비</strong>를
            누릅니다. 같은 SOL의 장부만 이관하며 실제 매도·환전은 없습니다. 기존
            원화 현금·체결 기록은 남습니다.
          </li>
          <li>
            USDT 시장 → 실거래 → 내 전략에서 새 초안의{" "}
            <strong>설정 확인·시작</strong>을 엽니다. 매도·재매수 가격표와 손실
            한도를 확인한 뒤 직접 시작합니다.
          </li>
        </ol>
        <p>
          기본값은 5단계·약 0.75%, 첫 매도는 현재 매도 호가보다 한 틱 위입니다.
          호가 단위 때문에 실제 간격은 조금 다릅니다. 시작 후에는 각 단계의
          메이커 전용 지정가를 미리 올리고, 체결 대금을 같은 단계의 낮은 가격
          재매수에 사용합니다. 이익은 USDT로 남으며 추가 USDT나 원화 환전이
          필요하지 않습니다.
        </p>
        <p>
          즉시 체결될 주문은 post_only 조건으로 거절·취소됩니다. 가격을 쫓거나
          시장가로 바꾸지 않습니다. 대기 주문은 60초마다 취소하지 않고 체결 또는
          전략 중단까지 유지합니다. 따라서 가격이 지나가도 미체결로 남을 수
          있습니다.
        </p>
        <p>
          계좌의 과거 평균 매수가가 원화라면 원화로 표시합니다. 새 장부 손익은
          이관 시 USDT 평가액부터 계산합니다. 원화 기록의 이관 평가액은 실제
          현금·실현 이익이 아니며 시장별 평가액을 합산하지 않습니다.
        </p>
        <p>
          이벤트 수수료는 주문 가능 정보에서 확인하고 실제 확정 비용을
          기록합니다. 기본 비용 가정은 이벤트 이후를 고려한 편도 0.25%입니다.
          이벤트 리워드는 지급 전 수익으로 계산하지 않으며 지급 후에도 봇의 매매
          손익에 자동 편입하지 않습니다. 원화보다 거래가 적은 거래쌍에서는
          체결이 오래 걸릴 수 있습니다.
        </p>
        <a
          className="primary"
          href="/?venue=upbit_usdt&mode=live&view=strategies"
        >
          USDT 전략 확인하기
        </a>
      </section>
      <section id="faq" className="panel guide-step">
        <h2>막혔을 때 확인할 것</h2>
        <details open>
          <summary>수집을 눌렀는데 아무것도 없어요</summary>
          <p>
            수집 기록의 상태와 메시지를 확인하세요. 잘못된 종목, API 접근 허용
            IP, 데이터 제공 범위, 호출 제한이 원인일 수 있습니다. 제한 오류가
            나면 곧바로 반복 요청하지 마세요.
          </p>
        </details>
        <details>
          <summary>코인 잔액이 안 나와요</summary>
          <p>
            ‘조회 확인 필요’의 오류를 확인하세요. 키에 자산 조회 권한이 있고
            클러스터의 출구 IP가 업비트 허용 목록에 있어야 합니다. 계좌 조회
            실패는 공개 시세 자체의 실패를 뜻하지 않습니다. 이전 잔액은 현재
            잔액으로 취급하지 않습니다.
          </p>
        </details>
        <details>
          <summary>전략이 실행 중인데 체결이 없어요</summary>
          <p>
            휴장, 그리드 가격선 대기, 신호용 완성 봉 부족, 호가 차이, 최신 호가
            확인, 최소 주문 금액 등의 이유를 ‘내 전략’에서 확인하세요. 주문을
            만들기 위해 조건을 무작정 완화하지 마세요.
          </p>
        </details>
        <details>
          <summary>추천 보류가 나와요</summary>
          <p>
            비용 후 손실, 거래 횟수 부족, 관측 기간 부족, 손실 기준 도달, 가상
            데이터 등 표시된 사유를 확인하세요. 추천을 받기 위해 같은 최종
            기간을 계속 재사용하기보다 새 기간을 더 모아 검증하세요.
          </p>
        </details>
        <details>
          <summary>텔레그램에서는 무엇을 할 수 있나요?</summary>
          <p>
            /web 화면 열기 · /guide 사용 가이드 · /account 토스 계좌 · /crypto
            업비트 계좌 · /status 시장별 상태 · /strategies 설정 확인 · /pause
            전체 중단
          </p>
        </details>
      </section>
      <footer className="guide-sources">
        <p>
          공식 참고:{" "}
          <a
            href="https://developers.tossinvest.com/docs#description/introduction"
            target="_blank"
            rel="noreferrer"
          >
            토스증권 API
          </a>{" "}
          ·{" "}
          <a
            href="https://docs.upbit.com/kr/docs/krw-market-info"
            target="_blank"
            rel="noreferrer"
          >
            업비트 거래 단위·최소 주문
          </a>{" "}
          ·{" "}
          <a
            href="https://docs.upbit.com/kr/reference/list-candles-minutes"
            target="_blank"
            rel="noreferrer"
          >
            업비트 분봉
          </a>
        </p>
        <a className="primary" href="/">
          대시보드로 이동 →
        </a>
      </footer>
    </main>
  );
}
