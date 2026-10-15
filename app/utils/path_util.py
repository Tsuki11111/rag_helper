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