# GitHub 로그인

GitHub OAuth로 본인 확인 후 **설정된 숫자 GitHub 사용자 ID 한 명만** 접근을 허용한다. 공개 저장소 사용자는 자신의 서버와 OAuth 앱을 구성해야 하며, 소스 공개가 다른 운영자의 계좌 접근 권한을 주지 않는다.

## 설정

GitHub Settings → Developer settings → OAuth Apps에서 로그인 전용 앱을 등록한다.

- Homepage URL: `https://trading.example.com`
- Authorization callback URL: `https://trading.example.com/api/auth/github/callback`
- Device Flow: 사용 안 함

위 도메인은 예제이므로 실제 운영 도메인으로 두 URL을 함께 바꾼다. 다른 서비스의 OAuth 앱을 공유하지 않는다. 이 앱은 scope를 요청하지 않으며 저장소·조직·이메일 권한이 있는 토큰을 거부한다.

비공개 환경변수 또는 소유자만 읽을 수 있는 파일에 다음 값을 설정한다. 실제 값을 Git, 이미지, CI, 대화에 넣지 않는다.

```dotenv
COTRADER_AUTH_MODE=github
COTRADER_PUBLIC_URL=https://trading.example.com
COTRADER_GITHUB_CLIENT_ID=replace-locally
COTRADER_GITHUB_CLIENT_SECRET=replace-locally
COTRADER_GITHUB_USER_ID=0
COTRADER_SESSION_SECRET=replace-with-at-least-32-random-characters
COTRADER_LIVE_ENABLED=false
```

허용 ID는 본인 계정으로 `gh api user --jq .id`를 실행해 확인한다. 사용자 이름은 바뀔 수 있으므로 허용 여부에 사용하지 않는다. `0`은 설정 전 예시이며 실제 로그인 모드에서는 거부한다.

API에만 위 설정을 주입한다. 로컬 설정 파일을 명시하려면 `uv run cotrader api --connect --identity-file /path/to/private/github.env`를 사용한다. 환경변수가 파일보다 우선하므로 `COTRADER_AUTH_MODE=local` 등 기존 실행 환경의 값을 먼저 확인한다. engine/research에는 GitHub 앱 키나 세션 서명을 주입하지 않는다.

로그인 요청 저장 테이블을 추가하므로 새 버전 시작 전에 `uv run alembic upgrade head`를 실행한다. 외부 서비스는 유효한 HTTPS가 필요하며 로컬 인증 모드를 외부에 노출하지 않는다.

## 확인 흐름

1. 서버가 임의의 state와 PKCE 검증 값을 만들고, 5분 유효 요청을 DB에 저장한다.
2. GitHub 인증 화면으로 이동한다. 브라우저에는 Secure·HttpOnly·SameSite=Lax 쿠키로 요청을 연결한다.
3. 콜백에서 브라우저 쿠키·state·유효시간을 확인하고 DB 요청을 원자적으로 한 번만 소비한다.
4. 서버에서 코드를 토큰으로 교환하고 `/user`로 실제 사용자 ID를 재확인한다. 토큰은 DB나 브라우저에 저장하지 않는다.
5. 허용 ID에만 공급자 구분·1시간 만료·서명 검증을 적용한 세션 쿠키를 발급한다. 로그아웃은 브라우저 쿠키를 제거한다.

인증 공급자 또는 허용 ID가 바뀌면 이전 세션은 사용할 수 없다. 계좌·전략·주문·데이터·연구 API는 모두 서버에서 인증하며 변경 요청은 Origin도 확인한다. 콜백 code/state는 접근 로그에서 제거한다. 서버 서명 방식의 세션은 로그아웃 전에 복제된 쿠키까지 즉시 폐기하지는 않으며 최대 1시간 유효하다.

Telegram 명령의 개인 대화방 제한은 별도로 유지한다. GitHub 모드의 `/web`은 일반 웹 링크를 보내 GitHub에서 로그인할 수 있게 한다. 기존 Telegram Mini App 로그인은 `auth_mode=telegram`을 선택한 설치 환경에서만 허용한다.

[GitHub 공식 OAuth 인증 흐름](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)을 기준으로 구현했다. 실제 운영 검증은 허용 계정 로그인, 다른 계정 거절, 로그아웃, 만료, 재시작, 비인증 API 차단을 실제 HTTPS 주소에서 확인해야 한다.
