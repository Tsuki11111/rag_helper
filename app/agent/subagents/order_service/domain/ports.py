"""订单子智能体端口：应用服务只依赖“调用 Java”的能力，不依赖具体客户端。"""
from typing import Any, Optional, Protocol


class OrderGateway(Protocol):
    def __call__(self, path: str, payload: Optional[dict] = None,
                 method: str = "POST", read_only: bool = False,
                 params: Optional[dict] = None) -> tuple[Any, str]:
        """调用平台内部接口，返回 `(data, error)`。"""
        ...
