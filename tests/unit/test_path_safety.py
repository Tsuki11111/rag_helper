"""路径与文件名安全：上传、撤回、文档名都只接受单段安全名字。"""
from io import BytesIO
from pathlib import Path

import pytest

from app.utils.path_util import (
    FileTooLargeError,
    UnsafePathError,
    copy_upload_limited,
    ensure_within,
    safe_filename,
    validate_file_title,
)


@pytest.mark.parametrize("name", ["说明书.pdf", "HL-3040CN v1.2.md", "a b.md"])
def test_safe_filename_accepts_normal_names(name):
    assert safe_filename(name) == name


@pytest.mark.parametrize("name", [
    "../x.md", "..\\x.md", "/etc/passwd", "C:\\x.md", "C:x.md",
    ".", "..", "a\x00.md", "CON.md", "name.",
])
def test_safe_filename_rejects_traversal(name):
    with pytest.raises(UnsafePathError):
        safe_filename(name)


@pytest.mark.parametrize("title", ["HL-3040CN v1.2", "烫金机说明书"])
def test_file_title_accepts_normal_names(title):
    assert validate_file_title(title) == title


@pytest.mark.parametrize("title", ["../x", "a/b", "a\\b", "C:x", ".", ".."])
def test_file_title_rejects_traversal(title):
    with pytest.raises(UnsafePathError):
        validate_file_title(title)


def test_ensure_within_rejects_escape(tmp_path):
    base = tmp_path
    assert ensure_within(base, base / "child.txt") == (base / "child.txt").resolve()
    with pytest.raises(UnsafePathError):
        ensure_within(base, base / ".." / "evil.txt")
    with pytest.raises(UnsafePathError):
        ensure_within(base, base)


def test_copy_upload_limited(tmp_path):
    dest = tmp_path / "upload.bin"
    assert copy_upload_limited(BytesIO(b"12345"), str(dest), 5) == 5
    with pytest.raises(FileTooLargeError):
        copy_upload_limited(BytesIO(b"123456"), str(dest), 5)
