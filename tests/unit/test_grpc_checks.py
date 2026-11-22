"""gRPC 映射回归用例（从 grpc_service.py 迁出）。"""
import pytest

import app.agent.api.grpc_service as _gs

globals().update({k: v for k, v in vars(_gs).items() if not k.startswith("__")})


def _check_form_grpc_mapping() -> list:
    """回归：表单 dict → proto FormRequest，且 final 事件带 forms（2026-11-07）。"""
    problems = []
    form = {
        "form_id": "form_1", "form_type": "refund_reason", "title": "选择退款原因",
        "description": "选一个原因", "submit_label": "提交退款原因", "expires_in_seconds": 600,
        "fields": [{"name": "reason", "label": "退款原因", "type": "select", "required": True,
                    "placeholder": "请选择", "default_value": "",
                    "options": [{"value": "商品有瑕疵", "label": "商品有瑕疵"}]}],
        "meta": {"orderNo": "NO1"},
    }
    pb = _form_request(form)
    if pb.form_id != "form_1" or pb.form_type != "refund_reason":
        problems.append(f"FormRequest 字段映射不对：{pb}")
    if not pb.fields or pb.fields[0].name != "reason" or pb.fields[0].type != "select":
        problems.append(f"FormField 映射不对：{pb.fields}")
    if not pb.fields[0].options or pb.fields[0].options[0].value != "商品有瑕疵":
        problems.append(f"FormOption 映射不对：{pb.fields[0].options}")
    if pb.meta.get("orderNo") != "NO1":
        problems.append(f"meta 映射不对：{pb.meta}")
    mapped = AgentServiceServicer._to_event({"type": "final", "text": "x", "forms": [form]})
    if not mapped.HasField("final") or len(mapped.final.forms) != 1:
        problems.append(f"final 事件未带 forms：{mapped}")
    return problems


def _check_stream_flags() -> list:
    """回归：客服设置开关的 proto 映射（optional 语义）+ delta 事件映射。"""
    problems = []
    plain = agent_pb2.ConverseRequest(message="hi")
    web, stream = read_stream_flags(plain)
    if (web, stream) != (True, False):
        problems.append(f"未设置时应为（联网开、流式关），实为 {(web, stream)}")
    tuned = agent_pb2.ConverseRequest(enable_web_search=False, stream=True)
    web2, stream2 = read_stream_flags(tuned)
    if (web2, stream2) != (False, True):
        problems.append(f"显式设置未生效：{(web2, stream2)}")
    mapped = AgentServiceServicer._to_event({"type": "delta", "text": "你好"})
    if not mapped.HasField("delta") or mapped.delta.delta != "你好":
        problems.append(f"delta 事件映射不对：{mapped}")
    return problems


def _check_grpc_entrypoint() -> list:
    """回归：python -m 启动必须执行 serve()，否则进程静默退出、8003 不监听。"""
    import inspect
    import re

    source = inspect.getsource(_gs)
    if not re.search(r'if\s+__name__\s*==\s*["\']__main__["\']\s*:\s*\n\s*serve\(\)', source):
        return ["缺少 __main__ 入口；python -m app.agent.api.grpc_service 会直接退出"]
    return []


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    agent_pb2_grpc.add_AgentServiceServicer_to_server(AgentServiceServicer(), server)
    server.add_insecure_port(f"{GRPC_HOST}:{GRPC_PORT}")   # 内网明文；TLS 按需再加（§6）
    server.start()
    logger.info(f"[{NODE_NAME}] gRPC 服务已启动，{GRPC_HOST}:{GRPC_PORT}（内网）")
    server.wait_for_termination()

CHECKS = [
    ("_check_form_grpc_mapping", _check_form_grpc_mapping),
    ("_check_stream_flags", _check_stream_flags),
    ("_check_grpc_entrypoint", _check_grpc_entrypoint),
]

@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_checks_pure(name, fn):
    assert fn() == []
