"""产品目录文件必须能被 resolver 找到，且关键映射存在。"""
from app.agent.subagents.product_kb.application.product_resolver import (
    CATALOG_PATH,
    _load_config,
)


def test_catalog_path_and_content():
    assert CATALOG_PATH.exists(), f"产品目录不存在：{CATALOG_PATH}"
    config = _load_config()
    products = config.get("products") or {}
    assert len(products) >= 7
    hak = products.get("Brother HAK 180 烫金机") or {}
    assert "HAK180" in (hak.get("aliases") or [])
    assert hak.get("variants") == ["全幅", "半幅"]
    assert config.get("parts")
