"""表单回传：失败可重试，并发提交只认一次。"""
import app.agent.runtime.forms as forms
import app.agent.subagents.order_service.api.actions as order_actions


class _FakeRedis:
    def __init__(self):
        self.keys = set()
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    def delete(self, key):
        self.keys.discard(key)
        self.deleted.append(key)


def _form():
    return forms.build_form(
        form_type="refund_reason", title="退款", fields=[],
        meta={"orderNo": "NO1"})


def _history(form):
    return [{"role": "assistant", "forms": [form]}]


def test_failed_tool_releases_claim(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(forms, "get_redis", lambda: redis)
    monkeypatch.setattr(
        order_actions, "prepare_refund",
        lambda order_no, reason="": "操作未完成：平台服务暂时不可用")

    form = _form()
    result = forms.dispatch_form_result(form["form_id"], {"reason": "瑕疵"}, _history(form))

    assert result["ok"] is False
    assert redis.keys == set()
    assert redis.deleted


def test_duplicate_submit_is_rejected(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(forms, "get_redis", lambda: redis)
    monkeypatch.setattr(
        order_actions, "prepare_refund",
        lambda order_no, reason="": "退款申请草稿已生成（尚未提交）。")

    form = _form()
    first = forms.dispatch_form_result(form["form_id"], {"reason": "瑕疵"}, _history(form))
    second = forms.dispatch_form_result(form["form_id"], {"reason": "瑕疵"}, _history(form))

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["kind"] == "duplicate"
