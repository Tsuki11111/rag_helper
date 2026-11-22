"""MinIO 图片前缀：带尾斜杠，兼容历史前导斜杠写法。"""
from app.conf.minio_config import (
    minio_image_object_prefix,
    minio_image_prefix,
    minio_image_prefixes,
)


def test_prefix_has_trailing_slash_and_no_overlap():
    base = minio_image_prefix("HAK180")
    longer = minio_image_prefix("HAK180 说明书")
    assert base.endswith("/")
    assert not longer.startswith(base)
    assert minio_image_prefix("/HAK180/") == base


def test_object_prefix_and_both_listing_variants():
    object_base = minio_image_object_prefix("HAK180")
    assert object_base.endswith("/")
    assert object_base.lstrip("/").endswith("HAK180/")
    base = minio_image_prefix("HAK180")
    assert minio_image_prefixes("HAK180") == [base, f"/{base}"]
