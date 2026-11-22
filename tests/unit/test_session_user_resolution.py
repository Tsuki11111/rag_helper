"""HTTP 会话 user_id 选择：viewer 固定自己，admin 沿用原 owner。"""
import pytest
from fastapi import HTTPException

from app.agent.api.query_service import _effective_user_id, _viewer_user_id


def _viewer():
    return {"user_id": "v1", "role": "viewer", "tenant_id": "t1"}


def _admin():
    return {"user_id": "a1", "role": "admin", "tenant_id": "t1"}


def test_viewer_uses_own_user_id():
    assert _effective_user_id(_viewer(), "s1", owner_lookup=lambda _: "v2") == "v1"


def test_admin_keeps_existing_owner_or_uses_own():
    assert _effective_user_id(_admin(), "s1", owner_lookup=lambda _: "v2") == "v2"
    assert _effective_user_id(_admin(), "new", owner_lookup=lambda _: "") == "a1"


def test_viewer_filter_and_missing_user_id():
    assert _viewer_user_id(_viewer()) == "v1"
    assert _viewer_user_id(_admin()) is None
    with pytest.raises(HTTPException) as exc:
        _viewer_user_id({"role": "viewer"})
    assert exc.value.status_code == 401
