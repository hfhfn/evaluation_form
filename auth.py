"""管理员认证 — Session Cookie 管理"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from starlette.requests import Request
from starlette.responses import Response

from db import get_db
from config import config


def _get_session(request: Request) -> Optional[str]:
    """从 Cookie 中读取 session_id"""
    return request.cookies.get("session_id")


def _set_session_cookie(response: Response, session_id: str):
    """设置 session Cookie"""
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,  # 7天
    )


def _clear_session_cookie(response: Response):
    """清除 session Cookie"""
    response.delete_cookie("session_id")


def _store_session(session_id: str, username: str):
    """将 session 存入 Redis / 内存 / 文件（这里用简单文件存储）"""
    session_file = f".sessions/{session_id}"
    import os
    os.makedirs(".sessions", exist_ok=True)
    with open(session_file, "w") as f:
        f.write(username)


def _load_session(session_id: str) -> Optional[str]:
    """从存储中读取 session 对应的用户名；超过 7 天的 session 视为过期并清除。"""
    import os
    import time
    session_file = f".sessions/{session_id}"
    try:
        # 过期检查：与 Cookie 的 max_age 保持一致（7 天）
        if time.time() - os.path.getmtime(session_file) > 86400 * 7:
            _remove_session(session_id)
            return None
        with open(session_file, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _remove_session(session_id: str):
    """删除 session"""
    import os
    session_file = f".sessions/{session_id}"
    try:
        os.remove(session_file)
    except FileNotFoundError:
        pass


async def login(username: str, password: str) -> bool:
    """验证管理员账号"""
    d = get_db()
    try:
        return d.verify_admin(username, password)
    finally:
        d.close()


async def register_session(username: str) -> str:
    """创建新 session 并存储"""
    session_id = secrets.token_urlsafe(32)
    _store_session(session_id, username)
    return session_id


async def get_logged_in_user(request: Request) -> Optional[str]:
    """获取当前登录的用户名，未登录返回 None"""
    session_id = _get_session(request)
    if not session_id:
        return None
    return _load_session(session_id)


async def logout(request: Request) -> Response:
    """登出"""
    session_id = _get_session(request)
    if session_id:
        _remove_session(session_id)
    resp = Response(status_code=204)
    _clear_session_cookie(resp)
    return resp
