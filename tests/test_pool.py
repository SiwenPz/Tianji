"""池数据模型与 CLI 全链路测试(票 55)。"""

import json
import os

import pytest
from typer.testing import CliRunner
from tianji import integrations, ops, pool as pool_mod
from tianji.cli import app
from tianji.db import connect

runner = CliRunner()


def _invoke(args, env=None):
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    r = runner.invoke(app, args, env=full)
    assert r.exit_code == 0, f"CLI 失败 {args}: {r.output}\n{r.exception}"
    return json.loads(r.output) if r.output.strip() else {}


def _invoke_err(args, env=None):
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    return runner.invoke(app, args, env=full)


# ---- 共享 fixture: 在 conn 上预置 credential 条目 ---------------

@pytest.fixture
def cred_conn(conn, controller):
    """在 conn 上预置两个已登记的 credential。"""
    integrations._write_entry(
        conn, controller, "credential:cred-a",
        {"provider": "testp", "key_ref": "/tmp/a"}, "cred-a")
    integrations._write_entry(
        conn, controller, "credential:cred-b",
        {"provider": "testp", "key_ref": "/tmp/b"}, "cred-b")
    integrations._write_entry(
        conn, controller, "credential:cred-c",
        {"provider": "testp", "key_ref": "/tmp/c"}, "cred-c")
    return conn


# ---- Ops 层直接测试 ---------------------------------------------

class TestPoolCreate:
    """建池: 基本成功 / 重复 / 无总控 / 缺 request_id / 非法成员。"""

    def test_basic(self, cred_conn, controller):
        """总控建池→拿到 token,审计行存在,配置正确。"""
        r = pool_mod.pool_create(
            cred_conn, controller, "test-pool",
            members=["cred-a", "cred-b"],
            circuit={"threshold": 5},
            request_id="pc-1")
        assert r["name"] == "test-pool"
        assert r["members"] == ["cred-a", "cred-b"]
        assert r["cursor"] == 0
        assert r["circuit"] == {"threshold": 5}
        assert len(r["token"]) == 64

    def test_empty_members(self, cred_conn, controller):
        """建池时可以传空成员列表(后续 add-member 补)。"""
        r = pool_mod.pool_create(
            cred_conn, controller, "empty-pool", members=[],
            request_id="pc-2")
        assert r["members"] == []

    def test_duplicate_rejected(self, cred_conn, controller):
        """同名池已存在 → ValueError。"""
        pool_mod.pool_create(cred_conn, controller, "dup-pool",
                              members=[], request_id="pc-dup-1")
        with pytest.raises(ValueError, match="已存在"):
            pool_mod.pool_create(cred_conn, controller, "dup-pool",
                                 members=[], request_id="pc-dup-2")

    def test_unknown_member_rejected(self, cred_conn, controller):
        """成员不是已登记 credential → ValueError。"""
        with pytest.raises(ValueError, match="credential"):
            pool_mod.pool_create(
                cred_conn, controller, "bad-pool",
                members=["no-such-cred"],
                request_id="pc-bad-1")

    def test_requires_request_id(self, cred_conn, controller):
        """缺 request_id → ValueError。"""
        with pytest.raises(ValueError, match="request_id"):
            pool_mod.pool_create(cred_conn, controller, "req-pool")

    def test_invalid_name_rejected(self, cred_conn, controller):
        """池名不含冒号;含冒号 → ValueError。"""
        with pytest.raises(ValueError, match="冒号"):
            pool_mod.pool_create(cred_conn, controller, "bad:name",
                                 members=[], request_id="pc-name")

    def test_audit_written(self, cred_conn, controller):
        """审计行含 pool_create action。"""
        pool_mod.pool_create(cred_conn, controller, "aud-pool",
                              members=[], request_id="pc-audit")
        row = cred_conn.execute(
            "SELECT detail FROM audit WHERE action='pool_create'"
        ).fetchone()
        assert row is not None
        detail = json.loads(row["detail"])
        assert detail["name"] == "aud-pool"


class TestPoolMembers:
    """加成员 / 摘成员逻辑。"""

    def _setup(self, conn, ctrl):
        """建池含一个成员,池名固定。"""
        pool_mod.pool_create(conn, ctrl, "m-pool",
                              members=["cred-a"], request_id="mm-1")

    def test_add_member(self, cred_conn, controller):
        self._setup(cred_conn, controller)
        r = pool_mod.pool_add_member(
            cred_conn, controller, "m-pool", "cred-b",
            request_id="am-1")
        assert "cred-b" in r["members"]
        assert "cred-a" in r["members"]

    def test_add_duplicate_member(self, cred_conn, controller):
        """已存在的成员再添加 → 幂等,返回当前列表无报错。"""
        self._setup(cred_conn, controller)
        r = pool_mod.pool_add_member(
            cred_conn, controller, "m-pool", "cred-a",
            request_id="am-dup")
        assert r["note"] == "成员已在池中"
        assert r["members"] == ["cred-a"]

    def test_remove_member(self, cred_conn, controller):
        self._setup(cred_conn, controller)
        r = pool_mod.pool_remove_member(
            cred_conn, controller, "m-pool", "cred-a",
            request_id="rm-1")
        assert "cred-a" not in r["members"]
        assert r["members"] == []

    def test_remove_last_member_warns(self, cred_conn, controller):
        """摘光最后一个成员 → warning 字段。"""
        self._setup(cred_conn, controller)
        r = pool_mod.pool_remove_member(
            cred_conn, controller, "m-pool", "cred-a",
            request_id="rm-last")
        assert r["warning"] == "池已空: 已移除最后一个成员"

    def test_remove_non_member_rejected(self, cred_conn, controller):
        self._setup(cred_conn, controller)
        with pytest.raises(ValueError, match="不在池"):
            pool_mod.pool_remove_member(
                cred_conn, controller, "m-pool", "cred-b",
                request_id="rm-nm")

    def test_add_unknown_credential_rejected(self, cred_conn, controller):
        self._setup(cred_conn, controller)
        with pytest.raises(ValueError, match="credential"):
            pool_mod.pool_add_member(
                cred_conn, controller, "m-pool", "no-such",
                request_id="am-unk")

    def test_add_to_nonexistent_pool(self, cred_conn, controller):
        with pytest.raises(KeyError, match="不存在"):
            pool_mod.pool_add_member(
                cred_conn, controller, "ghost", "cred-a",
                request_id="am-ghost")

    def test_requires_request_id(self, cred_conn, controller):
        self._setup(cred_conn, controller)
        with pytest.raises(ValueError, match="request_id"):
            pool_mod.pool_add_member(cred_conn, controller, "m-pool", "cred-b")


class TestPoolList:
    """列出全部池(只读)。"""

    def test_empty(self, conn):
        assert pool_mod.pool_list(conn) == []

    def test_lists_pools(self, cred_conn, controller):
        pool_mod.pool_create(cred_conn, controller, "lst-a",
                              members=[], request_id="la-1")
        pool_mod.pool_create(cred_conn, controller, "lst-b",
                              members=["cred-a"], request_id="la-2")
        pools = pool_mod.pool_list(cred_conn)
        names = {p["name"] for p in pools}
        assert "lst-a" in names and "lst-b" in names


class TestPoolStatus:
    """查池详情。"""

    def test_basic(self, cred_conn, controller):
        pool_mod.pool_create(cred_conn, controller, "st-pool",
                              members=["cred-a"], circuit={"t": 3},
                              request_id="st-1")
        s = pool_mod.pool_status(cred_conn, "st-pool")
        assert s["name"] == "st-pool"
        assert s["members"] == ["cred-a"]
        assert s["circuit"] == {"t": 3}
        assert s["has_token"] is True

    def test_not_found(self, conn):
        with pytest.raises(KeyError, match="不存在"):
            pool_mod.pool_status(conn, "ghost")


class TestPoolRotateToken:
    """令牌轮换。"""

    def test_rotate(self, cred_conn, controller):
        pool_mod.pool_create(cred_conn, controller, "rot-pool",
                              members=[], request_id="rot-1")
        token1 = cred_conn.execute(
            "SELECT value FROM configs WHERE key='pool:token:rot-pool'"
        ).fetchone()["value"]
        r = pool_mod.pool_rotate_token(
            cred_conn, controller, "rot-pool", request_id="rot-2")
        assert len(r["token"]) == 64
        assert r["token"] != token1

    def test_rotate_nonexistent(self, cred_conn, controller):
        with pytest.raises(KeyError, match="不存在"):
            pool_mod.pool_rotate_token(
                cred_conn, controller, "ghost", request_id="rot-ghost")


class TestPoolDelete:
    """删池。"""

    def test_delete(self, cred_conn, controller):
        pool_mod.pool_create(cred_conn, controller, "del-pool",
                              members=[], request_id="del-1")
        r = pool_mod.pool_delete(cred_conn, controller, "del-pool",
                                  request_id="del-2")
        assert r["deleted"] is True
        assert pool_mod._read_pool(cred_conn, "del-pool") is None
        row = cred_conn.execute(
            "SELECT 1 FROM configs WHERE key='pool:token:del-pool'"
        ).fetchone()
        assert row is None

    def test_delete_nonexistent(self, cred_conn, controller):
        with pytest.raises(KeyError, match="不存在"):
            pool_mod.pool_delete(cred_conn, controller, "ghost",
                                  request_id="del-ghost")


class TestPoolIdempotency:
    """幂等回放: 同 request_id 重放返回原回执。"""

    def test_create_idem(self, cred_conn, controller):
        r1 = pool_mod.pool_create(
            cred_conn, controller, "idem-pool", members=[],
            request_id="idem-1")
        r2 = pool_mod.pool_create(
            cred_conn, controller, "idem-pool", members=[],
            request_id="idem-1")
        # 回放结果覆盖原始字段 + replay:True
        assert r2["replay"] is True
        assert r2["name"] == r1["name"]
        assert r2["members"] == r1["members"]

    def test_add_member_idem(self, cred_conn, controller):
        pool_mod.pool_create(cred_conn, controller, "idem-m",
                              members=[], request_id="idem-m-1")
        r1 = pool_mod.pool_add_member(
            cred_conn, controller, "idem-m", "cred-a",
            request_id="idem-m-2")
        r2 = pool_mod.pool_add_member(
            cred_conn, controller, "idem-m", "cred-a",
            request_id="idem-m-2")
        assert r2["replay"] is True
        assert r2["members"] == r1["members"]


# ---- CLI 集成测试 -----------------------------------------------
# CLI 路径通过覆写 TIANJI_* 环境变量注入总控身份

class TestPoolCLI:
    """tianji pool 子命令 CLI 层。"""

    def _setup_env(self, tmp_path, monkeypatch):
        """初始化 TIANJI_HOME + 总控实例 + credential,返回 controller secret。"""
        monkeypatch.setenv("TIANJI_HOME", str(tmp_path / "home"))
        conn = connect()
        ops.ensure_defaults(conn)
        r = ops.instance_register(
            conn, "控制器", "claude", "deepseek-v4-flash",
            controller=True)
        integrations._write_entry(
            conn, r, "credential:cli-cred",
            {"provider": "testp", "key_ref": "/tmp/cli"},
            "cred-setup")
        conn.close()
        return r["secret"]

    def test_cli_create(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        r = _invoke(["pool", "create", "cli-pool",
                      "--members", "cli-cred",
                      "--circuit", '{"timeout": 10}',
                      "--request-id", "cli-pc-1"], env=env)
        assert r["name"] == "cli-pool"
        assert r["members"] == ["cli-cred"]
        assert len(r["token"]) == 64
        assert "明文仅此一次" in r["note"]

    def test_cli_add_member(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        conn = connect()
        integrations._write_entry(
            conn, {"worker_id": "控制器", "secret": secret},
            "credential:cli-cred2",
            {"provider": "testp", "key_ref": "/tmp/cli2"},
            "cred2-setup")
        conn.close()
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-mpool",
                  "--members", "cli-cred",
                  "--request-id", "cli-mp-1"], env=env)
        r = _invoke(["pool", "add-member", "cli-mpool", "cli-cred2",
                      "--request-id", "cli-am-1"], env=env)
        assert "cli-cred2" in r["members"]

    def test_cli_remove_member(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-rmpool",
                  "--members", "cli-cred",
                  "--request-id", "cli-rm-1"], env=env)
        r = _invoke(["pool", "remove-member", "cli-rmpool", "cli-cred",
                      "--request-id", "cli-rm-2"], env=env)
        assert "cli-cred" not in r["members"]

    def test_cli_list(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-l1",
                  "--request-id", "cli-l1"], env=env)
        _invoke(["pool", "create", "cli-l2",
                  "--members", "cli-cred",
                  "--request-id", "cli-l2"], env=env)
        r = _invoke(["pool", "list"], env=env)
        names = {p["name"] for p in r}
        assert "cli-l1" in names and "cli-l2" in names

    def test_cli_status(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-s1",
                  "--members", "cli-cred",
                  "--circuit", '{"max_errors": 3}',
                  "--request-id", "cli-s-1"], env=env)
        r = _invoke(["pool", "status", "cli-s1"], env=env)
        assert r["name"] == "cli-s1"
        assert r["has_token"] is True
        assert r["circuit"] == {"max_errors": 3}

    def test_cli_rotate_token(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-rt",
                  "--request-id", "cli-rt-1"], env=env)
        r = _invoke(["pool", "rotate-token", "cli-rt",
                      "--request-id", "cli-rt-2"], env=env)
        assert len(r["token"]) == 64
        assert "作废" in r["note"]

    def test_cli_delete(self, tmp_path, monkeypatch):
        secret = self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "控制器", "TIANJI_SECRET": secret}
        _invoke(["pool", "create", "cli-del",
                  "--request-id", "cli-del-1"], env=env)
        r = _invoke(["pool", "delete", "cli-del",
                      "--request-id", "cli-del-2"], env=env)
        assert r["deleted"] is True

    def test_cli_permission_denied(self, tmp_path, monkeypatch):
        """非总控身份 → 拒绝。"""
        self._setup_env(tmp_path, monkeypatch)
        env = {"TIANJI_WORKER_ID": "入侵者", "TIANJI_SECRET": "bad"}
        r = _invoke_err(["pool", "create", "x",
                          "--members", "cli-cred",
                          "--request-id", "cli-err-1"], env=env)
        assert r.exit_code != 0
