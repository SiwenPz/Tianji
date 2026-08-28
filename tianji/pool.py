"""池数据层(票 55): 号池的增删改查+令牌管理。

数据存在账本 configs 表,零新表:
  pool:<名>   → {members: [credential 名...], cursor: int, circuit: {}}
  pool:token:<名> → 明文令牌(建池机械生成;rotate-token 时轮换)
"""

from __future__ import annotations

import json
import secrets

import sqlite3

from . import auth, integrations, messages, ops
from .db import now, tx


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _pool_key(name: str) -> str:
    return f"pool:{name}"


def _token_key(name: str) -> str:
    return f"pool:token:{name}"


def _generate_token() -> str:
    return secrets.token_hex(32)


def _read_pool(conn, name: str) -> dict | None:
    """读取池配置行,返回 dict 或 None(不存在/JSON 损坏)。"""
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (_pool_key(name),)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def _validate_credential(conn, credential_name: str) -> bool:
    """成员必须是集成注册表里已登记的 credential。"""
    row = conn.execute(
        "SELECT 1 FROM configs WHERE key=?",
        (f"credential:{credential_name}",),
    ).fetchone()
    return row is not None


def _with_idem(conn, request_id, operation, fn):
    """不可逆转换必带 request_id;重放返回原回执(复刻 ops._with_idem 语义)。"""
    if not request_id:
        raise ValueError(f"{operation} 是不可逆转换,必须带 request_id(幂等回执)")
    return messages.idempotent(conn, request_id, operation, fn)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def pool_create(conn, ident, name: str, members=None,
                circuit=None, request_id=None):
    """建池(总控+审计+幂等)。

    返回 {"name", "members", "cursor", "circuit", "token", "note"}。
    token 明文仅此一次输出。
    """
    if not name or ":" in name:
        raise ValueError("池名须为非空且不含冒号")
    if not auth.check_controller(conn, ident):
        raise PermissionError("pool create 仅总控身份可执行")
    if not request_id:
        raise ValueError("建池必须带 request_id")
    members = list(members or [])
    circuit = dict(circuit or {})
    # 预校验成员资格
    for m in members:
        if not _validate_credential(conn, m):
            raise ValueError(
                f"成员 {m} 不是已登记的 credential(先 tianji wizard credential-add)")
    with tx(conn) as c:
        def _do():
            if _read_pool(c, name) is not None:
                raise ValueError(f"池 {name} 已存在(池名唯一)")
            token = _generate_token()
            ts = now()
            pool_value = json.dumps({
                "members": members,
                "cursor": 0,
                "circuit": circuit,
                "created_at": ts,
                "updated_at": ts,
            }, ensure_ascii=False)
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?)",
                (_pool_key(name), pool_value, ts))
            # token 用 UPSERT,防 rotate-token 前后不一致
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (_token_key(name), token, ts))
            ops.audit(c, "pool_create", {
                "name": name,
                "members": members,
                "circuit": circuit,
                "by": ident["worker_id"],
            })
            return {
                "name": name,
                "members": members,
                "cursor": 0,
                "circuit": circuit,
                "token": token,
                "note": "token 明文仅此一次显示,请妥善保存",
            }
        return _with_idem(c, request_id, "pool_create", _do)


def pool_add_member(conn, ident, name: str, credential_name: str,
                    request_id=None):
    """加成员(总控+审计+幂等)。

    成员已在池中 → 直接返回当前成员列表(幂等友好)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("pool add-member 仅总控身份可执行")
    if not request_id:
        raise ValueError("add-member 必须带 request_id")
    if not _validate_credential(conn, credential_name):
        raise ValueError(
            f"成员 {credential_name} 不是已登记的 credential"
            "(先 tianji wizard credential-add)")
    with tx(conn) as c:
        def _do():
            pool = _read_pool(c, name)
            if pool is None:
                raise KeyError(f"池 {name} 不存在")
            members = list(pool.get("members", []))
            if credential_name in members:
                return {"name": name, "members": members,
                        "note": "成员已在池中"}
            members.append(credential_name)
            pool["members"] = members
            pool["updated_at"] = now()
            c.execute(
                "UPDATE configs SET value=?, updated_at=? WHERE key=?",
                (json.dumps(pool, ensure_ascii=False), now(), _pool_key(name)))
            ops.audit(c, "pool_add_member", {
                "name": name,
                "member": credential_name,
                "by": ident["worker_id"],
            })
            return {"name": name, "members": members}
        return _with_idem(c, request_id, "pool_add_member", _do)


def pool_remove_member(conn, ident, name: str, credential_name: str,
                       request_id=None):
    """摘成员(总控+审计+幂等)。

    摘光最后一个成员时在返回中加入 warning 字段。移除不存在的成员 → ValueError。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("pool remove-member 仅总控身份可执行")
    if not request_id:
        raise ValueError("remove-member 必须带 request_id")
    with tx(conn) as c:
        def _do():
            pool = _read_pool(c, name)
            if pool is None:
                raise KeyError(f"池 {name} 不存在")
            members = list(pool.get("members", []))
            if credential_name not in members:
                raise ValueError(f"成员 {credential_name} 不在池 {name} 中")
            members.remove(credential_name)
            pool["members"] = members
            pool["updated_at"] = now()
            c.execute(
                "UPDATE configs SET value=?, updated_at=? WHERE key=?",
                (json.dumps(pool, ensure_ascii=False), now(), _pool_key(name)))
            warning = ""
            if not members:
                warning = "池已空: 已移除最后一个成员"
            ops.audit(c, "pool_remove_member", {
                "name": name,
                "member": credential_name,
                "remaining": len(members),
                "warning": warning,
                "by": ident["worker_id"],
            })
            result = {"name": name, "members": members}
            if warning:
                result["warning"] = warning
            return result
        return _with_idem(c, request_id, "pool_remove_member", _do)


def pool_list(conn) -> list:
    """列出全部池(只读,无需总控身份)。"""
    pools = []
    for row in conn.execute(
        "SELECT key, value FROM configs "
        "WHERE key LIKE 'pool:%' AND key NOT LIKE 'pool:token:%' "
        "ORDER BY key"
    ).fetchall():
        try:
            cfg = json.loads(row["value"])
            cfg["name"] = row["key"][len("pool:"):]
            pools.append(cfg)
        except json.JSONDecodeError:
            pass
    return pools


def list_credential_names(conn) -> list:
    """列出全部已登记 credential 名称(只读)。"""
    return [r["key"][len("credential:"):]
            for r in conn.execute(
                "SELECT key FROM configs WHERE key LIKE 'credential:%'"
            ).fetchall()]


def pool_status(conn, name: str) -> dict:
    """查池详情+令牌状态(只读)。"""
    pool = _read_pool(conn, name)
    if pool is None:
        raise KeyError(f"池 {name} 不存在")
    token_row = conn.execute(
        "SELECT 1 FROM configs WHERE key=?", (_token_key(name),)
    ).fetchone()
    result = dict(pool)
    result["name"] = name
    result["has_token"] = token_row is not None
    return result


def pool_rotate_token(conn, ident, name: str, request_id=None):
    """令牌轮换(总控+审计+幂等): 生成新 token,旧 token 作废。

    返回 {"name", "token", "note"};token 明文仅此一次输出。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("pool rotate-token 仅总控身份可执行")
    if not request_id:
        raise ValueError("rotate-token 必须带 request_id")
    with tx(conn) as c:
        def _do():
            pool = _read_pool(c, name)
            if pool is None:
                raise KeyError(f"池 {name} 不存在")
            new_token = _generate_token()
            ts = now()
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (_token_key(name), new_token, ts))
            ops.audit(c, "pool_rotate_token", {
                "name": name,
                "by": ident["worker_id"],
            })
            return {
                "name": name,
                "token": new_token,
                "note": "token 明文仅此一次显示,旧 token 已作废",
            }
        return _with_idem(c, request_id, "pool_rotate_token", _do)


def pool_delete(conn, ident, name: str, request_id=None):
    """删池(总控+审计+幂等): 同时删除池配置和 token。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("pool delete 仅总控身份可执行")
    if not request_id:
        raise ValueError("pool delete 必须带 request_id")
    with tx(conn) as c:
        def _do():
            pool = _read_pool(c, name)
            if pool is None:
                raise KeyError(f"池 {name} 不存在")
            c.execute("DELETE FROM configs WHERE key=?", (_pool_key(name),))
            c.execute("DELETE FROM configs WHERE key=?", (_token_key(name),))
            ops.audit(c, "pool_delete", {
                "name": name,
                "by": ident["worker_id"],
            })
            return {"name": name, "deleted": True}
        return _with_idem(c, request_id, "pool_delete", _do)
