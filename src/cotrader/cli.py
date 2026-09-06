import argparse
import asyncio
import logging
import signal

import uvicorn

from cotrader.config import Settings


class PrivateQueryFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            address, method, path, version, status = record.args
            record.args = (address, method, path.split("?", 1)[0], version, status)
        return True


async def run_daemon(work):
    task = asyncio.create_task(work)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description="Cotrader 실행 역할")
    parser.add_argument("role", choices=["api", "engine", "research"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--connect", action="store_true", help="지정한 로컬 env 파일로 Telegram·계좌 조회 연결"
    )
    parser.add_argument("--identity-file", help="api 역할의 GitHub 로그인 설정 파일")
    args = parser.parse_args()
    from pathlib import Path

    options = {"runtime_role": args.role}
    if args.connect:
        options["live_enabled"] = False
    if args.connect and args.role in {"api", "engine"}:
        filename = "telegram.env" if args.role == "api" else "openapi.env"
        path = Path.home() / ".config/tossinvest" / filename
        if not path.is_file():
            parser.error(f"연결 자격증명 파일을 찾을 수 없습니다: {path}")
        options["_env_file"] = path
        options["telegram_enabled" if args.role == "api" else "account_reads_enabled"] = True
    if args.identity_file:
        if args.role != "api":
            parser.error("로그인 설정 파일은 api 역할에서만 읽습니다")
        identity_path = Path(args.identity_file).expanduser()
        if not identity_path.is_file():
            parser.error("로그인 설정 파일을 찾을 수 없습니다")
        options["_env_file"] = [*([options["_env_file"]] if "_env_file" in options else []), identity_path]
    settings = Settings(**options)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(PrivateQueryFilter())
    if args.role == "api":
        if settings.auth_mode == "local" and args.host not in {"127.0.0.1", "::1"}:
            parser.error("로컬 인증 모드는 loopback 주소에서만 실행할 수 있습니다")
        from cotrader.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=False)
    elif args.role == "engine":
        from cotrader.engine import Engine

        asyncio.run(run_daemon(Engine(settings).run()))
    else:
        from cotrader.worker import run_worker

        asyncio.run(run_daemon(run_worker(settings)))


if __name__ == "__main__":
    main()
