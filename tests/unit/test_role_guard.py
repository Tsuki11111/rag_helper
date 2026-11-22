"""角色守卫：viewer 不能执行管理端写操作。"""
import pytest
from fastapi import HTTPException

from app.clients.mongo_user_clients import ROLE_ADMIN
from app.utils.auth_utils import require_admin


def test_viewer_is_rejected():
    with pytest.raises(HTTPException) as exc:
        require_admin({"user_id": "v1", "role": "viewer"})
    assert exc.value.status_code == 403


def test_admin_is_allowed():
    user = require_admin({"user_id": "a1", "role": ROLE_ADMIN})
    assert user["role"] == ROLE_ADMIN
