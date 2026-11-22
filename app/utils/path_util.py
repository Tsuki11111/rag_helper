# app/utils/path_utils.py
from pathlib import Path
from dotenv import load_dotenv
import os
from pathlib import Path

def get_path_dir(ps:int = 0)->Path:
    """
    pathlib.Path 提供了 parents 属性，这是一个有序的路径上级目录迭代器，直接通过索引取值就能快速获取「上 N 级目录」，完美解决多层 .parent 繁琐的问题，这也是官方推荐的简化写法！
    核心规则：parents[N] 索引对应「向上的层级数」
    parents[0] → 等价于 .parent（当前路径的上 1 级目录）
    parents[1] → 等价于 .parent.parent（当前路径的上 2 级目录）
    parents[2] → 等价于 .parent.parent.parent（当前路径的上 3 级目录）
    以此类推，parents[N] → 直接获取上 N+1 级目录，索引越⼤，层级越靠上
    :param ps:
    :return:
    """
    dir_path = Path(__file__).parents[ps]
    return dir_path


def get_project_root(identifier: str = ".env") -> Path:
    # 第一步：优先读取环境变量（生产环境用）
    env_root = os.getenv("PROJECT_ROOT")
    if env_root and Path(env_root).absolute().exists():
        return Path(env_root).absolute()

    # 第二步：依次尝试多个根标识 —— `.env`（本机开发环境）→ `pyproject.toml`（仓库自带的
    # 兜底）。**没有 .env 的环境（CI、新克隆的仓库）不该 import 即崩**：2026-10-15
    # 首个 CI 跑就栽在这里（FileNotFoundError 把一半回归用例带塌）。
    # 命中 .env 时照旧加载它；命中 pyproject.toml 时没有 dotenv 可加载，直接返回
    for marker in (identifier, "pyproject.toml"):
        current_dir = Path(__file__).absolute().parent
        while current_dir != current_dir.parent:
            if (current_dir / marker).exists():
                if marker == identifier:
                    load_dotenv(dotenv_path=current_dir / marker)
                return current_dir
            current_dir = current_dir.parent

    raise FileNotFoundError(
        f"未找到项目根目录标识「{identifier} / pyproject.toml」，且环境变量PROJECT_ROOT未配置")


PROJECT_ROOT = get_project_root(".env")


# ── 文件名与路径安全（批次 1）──
# 上传文件名、文档名、撤回目标都来自请求参数，不能直接拼路径。
# 规则是「只接受单段名字」：拒绝任何路径成分、控制字符与系统保留名。

class UnsafePathError(ValueError):
    """文件名/文档名包含路径穿越或非法字符。"""


class FileTooLargeError(ValueError):
    """上传文件超过大小上限。"""


_WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _validate_name(raw: str, kind: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise UnsafePathError(f"{kind}不能为空")
    if name in {".", ".."}:
        raise UnsafePathError(f"{kind}不能是 . 或 ..")
    if "/" in name or "\\" in name:
        raise UnsafePathError(f"{kind}不能包含路径分隔符")
    if any(ord(ch) < 32 for ch in name):
        raise UnsafePathError(f"{kind}不能包含控制字符")
    # Windows 上 Path("C:foo").is_absolute() 为 False，但仍然是带盘符路径
    if Path(name).is_absolute() or Path(name).drive or (len(name) >= 2 and name[1] == ":"):
        raise UnsafePathError(f"{kind}不能是绝对路径或带盘符")
    if name != name.rstrip(" ."):
        raise UnsafePathError(f"{kind}不能以点或空格结尾")
    if len(name) > 200:
        raise UnsafePathError(f"{kind}过长（最多 200 字符）")
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise UnsafePathError(f"{kind}不能使用系统保留名 {stem}")
    return name


def safe_filename(filename: str) -> str:
    """上传文件名 → 安全 basename；只接受单段名字，拒绝任何路径成分。"""
    return _validate_name(filename, "文件名")


def validate_file_title(file_title: str) -> str:
    """文档名 → 安全单段名字；规则同文件名，但不要求带后缀。"""
    return _validate_name(file_title, "文档名")


def ensure_within(base: Path, target: Path, allow_base: bool = False) -> Path:
    """把 target 解析后限制在 base 内；越界抛 UnsafePathError。"""
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if target_resolved == base_resolved:
        if allow_base:
            return target_resolved
        raise UnsafePathError(f"路径越界：{target} 不能是 {base} 本身")
    if not target_resolved.is_relative_to(base_resolved):
        raise UnsafePathError(f"路径越界：{target} 不在 {base} 内")
    return target_resolved


def copy_upload_limited(src, dest_path: str, max_bytes: int) -> int:
    """
    分块把上传流写入磁盘，累计字节超过 max_bytes 立即抛 FileTooLargeError。

    :return: 实际写入字节数
    """
    total = 0
    with open(dest_path, "wb") as buffer:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FileTooLargeError(
                    f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
            buffer.write(chunk)
    return total


def _check_path_safety() -> list:
    """路径安全：文件名/文档名单段校验、越界限制、上传大小流式拦截。"""
    import tempfile
    from io import BytesIO

    problems = []

    for raw in ("说明书.pdf", "HL-3040CN v1.2.md", "a b.md"):
        try:
            if safe_filename(raw) != raw:
                problems.append(f"安全文件名被改动：{raw!r}")
        except UnsafePathError as e:
            problems.append(f"正常文件名被拒：{raw!r}（{e}）")

    for raw in ("../x.md", "..\\x.md", "/etc/passwd", "C:\\x.md", "C:x.md",
                ".", "..", "a\x00.md", "CON.md", "name."):
        try:
            safe_filename(raw)
            problems.append(f"非法文件名竟然通过：{raw!r}")
        except UnsafePathError:
            pass

    for raw in ("HL-3040CN v1.2", "烫金机说明书"):
        try:
            if validate_file_title(raw) != raw:
                problems.append(f"安全文档名被改动：{raw!r}")
        except UnsafePathError as e:
            problems.append(f"正常文档名被拒：{raw!r}（{e}）")

    for raw in ("../x", "a/b", "a\\b", "C:x", ".", ".."):
        try:
            validate_file_title(raw)
            problems.append(f"非法文档名竟然通过：{raw!r}")
        except UnsafePathError:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            ensure_within(base, base / "child.txt")
        except UnsafePathError as e:
            problems.append(f"合法子路径被拒：{e}")
        try:
            ensure_within(base, base / ".." / "evil.txt")
            problems.append("越界路径竟然通过")
        except UnsafePathError:
            pass

        dest = base / "upload.bin"
        try:
            copy_upload_limited(BytesIO(b"123456"), str(dest), 5)
            problems.append("超限上传竟然通过")
        except FileTooLargeError:
            pass
        if copy_upload_limited(BytesIO(b"12345"), str(dest), 5) != 5:
            problems.append("正常上传字节数不对")

    return problems
