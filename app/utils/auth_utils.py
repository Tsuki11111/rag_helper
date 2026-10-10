"""
访问鉴权（FastAPI 依赖）

提供 `current_tenant`：校验调用方身份，返回 `{tenant_id, name, role}`。

**两种密钥来源**：

- `Authorization: Bearer <key>` —— 给脚本 / 程序化调用
- Cookie `kb_session` —— 给浏览器

为什么必须支持 Cookie：查询服务的流式接口用的是 `EventSource`，
而 **EventSource 无法自定义请求头**，Bearer 头在那条路上根本发不出去。
Cookie 由浏览器自动携带，页面里十来处调用一行都不用改。

**目前只做「认证」、不做「授权过滤」**：`tenant_id` 已经拿到，但数据还没按它隔离
（给四个存储加租户维度是停机迁移级别的改动）。所以现阶段**任何有效密钥都能看到全部数据**——
这一层的价值是把身份打通，并为后续隔离留好接口，不是现在就实现隔离。
"""
import os
from typing import Any, Dict, Optional

from fastapi import Cookie, Header, HTTPException, Response

from app.clients.mongo_user_utils import verify_key

# 浏览器侧会话 Cookie 名
COOKIE_NAME = "kb_session"
# Cookie 有效期（秒）：30 天，够用且不至于永久
COOKIE_MAX_AGE = 30 * 24 * 3600


def _extract_key(authorization: Optional[str], kb_session: Optional[str]) -> str:
    """从请求头或 Cookie 里取出明文密钥，优先请求头"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (kb_session or "").strip()


def current_tenant(
    authorization: Optional[str] = Header(None),
    kb_session: Optional[str] = Cookie(None),
) -> Dict[str, Any]:
    """
    校验调用方身份，作为路由依赖使用

    用法：`@app.get("/xxx", dependencies=[Depends(current_tenant)])`；
    需要用到身份时把 `user: Dict = Depends(current_tenant)` 写进参数。

    :raises HTTPException: 401 —— 未带密钥、密钥无效或已撤销
    """
    raw_key = _extract_key(authorization, kb_session)
    if not raw_key:
        raise HTTPException(status_code=401, detail="缺少访问密钥")

    user = verify_key(raw_key)
    if not user:
        raise HTTPException(status_code=401, detail="访问密钥无效或已撤销")
    return user


def set_session_cookie(response: Response, raw_key: str) -> None:
    """
    登录成功时把密钥写进 HttpOnly Cookie

    HttpOnly 让页面 JS 读不到，XSS 也偷不走；SameSite=Lax 挡掉跨站发起的写操作。
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=raw_key,
        httponly=True,
        samesite="lax",
        # 本机跑的是 http，置 True 会导致 Cookie 根本存不下来；
        # 部署到 https 后务必把 .env 的 COOKIE_SECURE 设为 1
        secure=os.getenv("COOKIE_SECURE") == "1",
        max_age=COOKIE_MAX_AGE,
    )


def clear_session_cookie(response: Response) -> None:
    """退出登录：清掉会话 Cookie"""
    response.delete_cookie(COOKIE_NAME)


def _check_auth_required() -> list:
    """
    回归用例：**受保护接口必须挡住没密钥 / 坏密钥的调用**（要 Mongo）

    这是 P5「越权」那一类里**现在能真断言**的部分 —— 认证层。
    （数据隔离层面的越权——A 读 B 的会话——现在**测不了也不该测**：这个系统还没有按租户
    隔离数据，任何有效密钥都能看全部，那是已知状态、也是 `java-integration.md` 要解决的。
    在那之前，这里的边界就是「有没有有效密钥」。）

    三个方向都断言：**没密钥要拒**、**坏密钥要拒**、**有效密钥要放行**（只验前两个会漏掉
    「把所有人都拒了」这种坏法）。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    from app.clients.mongo_user_utils import get_user_tool

    # ① 没带密钥
    try:
        current_tenant(authorization=None, kb_session=None)
        problems.append("没带密钥竟然通过了")
    except HTTPException as e:
        if e.status_code != 401:
            problems.append(f"没带密钥应 401，实得 {e.status_code}")

    # ② 密钥无效 / 已撤销
    try:
        current_tenant(authorization="Bearer 这个密钥不可能存在", kb_session=None)
        problems.append("无效密钥竟然通过了")
    except HTTPException as e:
        if e.status_code != 401:
            problems.append(f"无效密钥应 401，实得 {e.status_code}")

    # ③ 有效密钥要放行 —— 临时建一个用户，用完撤销（别动用户自己的密钥）
    from app.clients.mongo_user_utils import create_user, get_user_tool
    name = "selftest_auth"
    try:
        get_user_tool().collection.delete_many({"name": name})   # 清掉上次残留
        raw_key = create_user(name, role="viewer", tenant_id="t_selftest")["raw_key"]
        user = current_tenant(authorization=f"Bearer {raw_key}", kb_session=None)
        if not user or not user.get("tenant_id"):
            problems.append(f"有效密钥没返回租户标识：{user!r}")
    except Exception as e:
        problems.append(f"有效密钥这条路抛异常：{type(e).__name__}: {e}")
    finally:
        try:
            # **删掉**，不是只 `revoke_user` —— 撤销只是打个标记，那会在用户的 `users`
            # 表里留一条名为 `selftest_auth` 的垃圾记录（踩过：跑完回归后表里多一条）
            get_user_tool().collection.delete_many({"name": name})
        except Exception:
            pass
    return problems
