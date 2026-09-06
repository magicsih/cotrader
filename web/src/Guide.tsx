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
          주식은 SPY·QQQ, 코인은 KRW-BTC·KRW-ETH 안에서 비교합니다. 특정 종목의
          매수 추천 목록은 아닙니다. 최근 30일의 공통 시각 데이터를 사용해
          그리드·추세 추종·과매도 반등을 비교하며, 그리드 가격 범위도 자동으로
          계산합니다.
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
        <a href="#faq">막혔을 때</a>
      </nav>
      <section id="start" className="panel guide-step">
        <span className="step-number">01</span>
        <h2>먼저 시장을 고르세요</h2>
        <p>
          대시보드 위쪽에서 <strong>토스 미국 주식</strong> 또는{" "}
          <strong>업비트 코인</strong>을 선택합니다. 주식은 USD, 코인은 KRW로
          표시하며 예산·손익·손실 중단 기준을 따로 관리합니다.
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
          주식은 가격선마다 최소 1주, 업비트는 최소 5,000원 규모가 필요합니다.
          단계 수를 늘릴수록 각 가격선의 주문이 작아집니다. 수수료보다 촘촘한
          설정이나 최소 주문 조건을 만족하지 않는 설정은 제외됩니다.
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
          실제 계좌의 잔액·보유 자산은 조회만 합니다. 모의매매는 실제 계좌에
          돈을 넣거나 주식을 보유할 필요가 없습니다. 며칠간 체결·중단·재시작을
          확인한 뒤 운용 방식을 판단하세요.
        </p>
        <p>
          <strong>전체 주문 중단</strong>은 두 시장의 전략을 중단하고 대기
          주문을 정리합니다. 보유 자산은 자동으로 시장가 매도하지 않습니다.
          그리드 하단 이탈이나 손실 한도 도달 시에도 보유분은 남습니다.
        </p>
      </section>
      <section id="live" className="panel guide-step">
        <span className="step-number">06</span>
        <h2>실제 주문은 별도 단계입니다</h2>
        <p>
          현재 서버의 실제 주문은 비활성화되어 있습니다. 상단{" "}
          <strong>실거래 기록</strong>은 기록 조회 화면을 고르는 버튼이며 주문
          권한을 켜는 스위치가 아닙니다.
        </p>
        <p>
          토스 실거래 전환 전에는 본인이 종목·총예산·손실 한도·기존 보유/미체결
          주문을 확인하고 별도 운영 승인을 해야 합니다. 입금한 돈이 곧 봇 예산이
          되지는 않으며, USD 주문 가능 금액과 전략별 배정 예산을 각각
          확인합니다. 자동 입금·환전은 실행하지 않습니다.
        </p>
        <p>
          <strong>
            업비트는 계좌 조회·데이터 수집·백테스트·최적화·모의매매를
            지원합니다. 코인 실제 주문·출금 경로는 제공하지 않습니다.
          </strong>
        </p>
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
