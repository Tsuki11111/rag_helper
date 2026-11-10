"""买小购客服 gRPC 服务入口。"""
import sys

from app.agent.api.grpc_service import serve


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    serve()


if __name__ == "__main__":
    main()
