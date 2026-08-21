"""身份模型(11.4): env 注入 worker_id/secret/dispatch_id/任务书路径。

secret 明文只在 env,SHA-256 摘要存账本,恒定时间比较;适配器与 CLI 同读同一组 env。
"""

import hashlib
import hmac
import os
import secrets

# 身份 env 一套命名(11.4)
ENV_WORKER_ID = "TIANJI_WORKER_ID"
ENV_SECRET = "TIANJI_SECRET"
ENV_DISPATCH_ID = "TIANJI_DISPATCH_ID"
ENV_TASK_PATH = "TIANJI_TASK_PATH"

# 总控身份存 configs(用户主会话不是 spawn 出来的,secret 由 register 时一次性配置)
CFG_CONTROLLER_ID = "controller_worker_id"
CFG_CONTROLLER_HASH = "controller_secret_hash"


def generate_secret() -> str:
    return secrets.token_hex(32)


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def env_identity(env: dict = None) -> dict:
    """读身份 env,缺一即无身份。返回 {worker_id, secret} 或 None。"""
    env = os.environ if env is None else env
    wid = env.get(ENV_WORKER_ID)
    secret = env.get(ENV_SECRET)
    if not wid or not secret:
        return None
    return {"worker_id": wid, "secret": secret}


def require_identity(env: dict = None) -> dict:
    """未注入 env 的调用一律拒绝。"""
    ident = env_identity(env)
    if ident is None:
        raise PermissionError(
            f"身份缺失: 未注入 {ENV_WORKER_ID}/{ENV_SECRET}(启动器应注入)"
        )
    return ident


def check_controller(conn, ident: dict, env: dict = None) -> bool:
    """总控身份校验(new 创建者限定 10.1,与派单同强度)。"""
    if not ident or not ident.get("worker_id") or not ident.get("secret"):
        return False
    cfg = _get_configs(conn)
    cid = cfg.get(CFG_CONTROLLER_ID)
    chash = cfg.get(CFG_CONTROLLER_HASH)
    if not cid or not chash:
        return False
    if ident["worker_id"] != cid:
        return False
    return constant_time_eq(secret_hash(ident["secret"]), chash)


def _get_configs(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM configs").fetchall()
    return {r["key"]: r["value"] for r in rows}


def require_controller(conn, ident: dict, env: dict = None):
    """非总控身份调用特权命令直接拒绝。"""
    if not check_controller(conn, ident):
        raise PermissionError(
            f"非总控身份({ident['worker_id']})无权执行该操作"
        )


def check_dispatch_secret(conn, dispatch_id: int, worker_id: str, secret: str) -> bool:
    """派单身份校验: 与派单 dcap_hash 恒定时间比较。"""
    row = conn.execute(
        "SELECT dcap_hash FROM dispatches WHERE id=?", (dispatch_id,)
    ).fetchone()
    if row is None:
        return False
    if row["dcap_hash"] != secret_hash(secret):
        return False
    # 派单必须属于该 worker(防拿别人派单的 secret 混用)
    drow = conn.execute(
        "SELECT worker_id FROM dispatches WHERE id=?", (dispatch_id,)
    ).fetchone()
    return drow is not None and drow["worker_id"] == worker_id
