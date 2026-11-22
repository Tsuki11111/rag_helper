"""导入收尾守卫：产品名未写入时必须失败，政策导入不受影响。"""
from app.importing.api import (
    KNOWLEDGE_BASE_POLICY,
    KNOWLEDGE_BASE_PRODUCT,
    _import_result_problem,
)


def test_product_name_failure_is_reported():
    assert _import_result_problem({"item_name_saved": False}, KNOWLEDGE_BASE_PRODUCT)


def test_product_name_success_passes():
    assert _import_result_problem({"item_name_saved": True}, KNOWLEDGE_BASE_PRODUCT) == ""


def test_policy_import_is_not_checked():
    assert _import_result_problem({"item_name_saved": False}, KNOWLEDGE_BASE_POLICY) == ""
