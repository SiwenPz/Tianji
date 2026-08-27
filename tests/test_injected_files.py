"""注入文件收敛到统一目录(票 53): injected_dir() 路径派生 + 存量迁移。

验收覆盖:
1. injected_dir() 默认路径默认 TIANJI_HOME/injected/
2. TIANJI_INJECTED_DIR env 可覆盖默认目录
3. 旧位置文件自动迁移到 injected/ (幂等,可重复)
4. 迁移标记文件 .injected_migrated 留审计痕迹
5. init_bootstrap 产物落到 injected/
6. 各读取方能从 injected/ 读到文件
"""

import json
import os
from pathlib import Path

import pytest

from tianji import wizard
from tianji.db import connect, injected_dir, migrate_injected_files, tianji_home
from tianji.ctrlprotocols import _read_secret
from tianji.webapp import _read_ctrl_secret


# ===================================================================
# injected_dir 路径派生
# ===================================================================

class TestInjectedDir:
    def test_default_path(self, monkeypatch, tmp_path):
        """默认 = TIANJI_HOME/injected/。"""
        monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
        # 重建缓存(同进程第二次调用不受 env 影响)
        import tianji.db as db_mod
        if hasattr(db_mod, '_injected_dir_cache'):
            delattr(db_mod, '_injected_dir_cache')
        p = injected_dir()
        assert p == tmp_path / "injected"

    def test_env_override(self, monkeypatch, tmp_path):
        """TIANJI_INJECTED_DIR 有效覆盖默认。"""
        override = tmp_path / "custom_injected"
        monkeypatch.setenv("TIANJI_INJECTED_DIR", str(override))
        p = injected_dir()
        assert p == override


# ===================================================================
# 存量迁移
# ===================================================================

class TestMigrateInjectedFiles:
    def test_migrate_from_old_layout(self, monkeypatch, tmp_path):
        """旧位置有文件时,首次调用迁移到 injected/。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("TIANJI_HOME", str(home))
        # 造旧布局文件
        (home / "settings-controller.json").write_text("{}", encoding="utf-8")
        (home / "ctrl-secret.txt").write_text("old-secret", encoding="utf-8")
        keys_dir = home / "keys"
        keys_dir.mkdir()
        (keys_dir / "master.key").write_text("key-content", encoding="utf-8")

        moved = migrate_injected_files()
        assert set(moved) == {
            "settings-controller.json", "ctrl-secret.txt", "master.key"}
        dst = injected_dir()
        assert (dst / "settings-controller.json").exists()
        assert (dst / "ctrl-secret.txt").exists()
        assert (dst / "master.key").exists()
        # 旧位置已清
        assert not (home / "settings-controller.json").exists()
        assert not (home / "ctrl-secret.txt").exists()
        assert not (keys_dir / "master.key").exists()
        # 标记文件
        flag = home / ".injected_migrated"
        assert flag.exists()
        assert "settings-controller.json" in flag.read_text(encoding="utf-8")

    def test_migrate_idempotent(self, monkeypatch, tmp_path):
        """第二次调用不重复搬、不动老文件。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("TIANJI_HOME", str(home))
        (home / "settings-controller.json").write_text("{}", encoding="utf-8")
        moved1 = migrate_injected_files()
        assert len(moved1) == 1
        # 二跑: 空返回
        moved2 = migrate_injected_files()
        assert moved2 == []

    def test_migrate_no_old_files(self, monkeypatch, tmp_path):
        """无旧文件时静默返回空列表。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("TIANJI_HOME", str(home))
        moved = migrate_injected_files()
        assert moved == []
        assert not (home / ".injected_migrated").exists()

    def test_migrate_partial_existing_dst(self, monkeypatch, tmp_path):
        """目标已有同名文件时跳过旧文件(不覆盖)。"""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("TIANJI_HOME", str(home))
        # 旧 settings
        (home / "settings-controller.json").write_text("old", encoding="utf-8")
        # 目标已有一份
        dst = injected_dir()
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "settings-controller.json").write_text("new", encoding="utf-8")

        moved = migrate_injected_files()
        assert moved == []
        assert (dst / "settings-controller.json").read_text(
            encoding="utf-8") == "new"  # 不被旧文件覆盖
        assert (home / "settings-controller.json").exists()  # 旧文件还在


# ===================================================================
# init_bootstrap 产物落到 injected/
# ===================================================================

class TestInitBootstrapInjected:
    def test_secret_and_settings_in_injected(self, monkeypatch, tmp_path):
        """init_bootstrap 的 ctrl-secret.txt + settings-controller.json 落到 injected/。"""
        home = tmp_path / "home"
        monkeypatch.setenv("TIANJI_HOME", str(home))
        r = wizard.init_bootstrap(
            home=str(home), shell="claude", model="test-m",
            base_url="https://x", key_value="sk-x")
        dst = injected_dir()
        assert (dst / "ctrl-secret.txt").exists()
        assert (dst / "settings-controller.json").exists()
        settings = json.loads(
            (dst / "settings-controller.json").read_text(encoding="utf-8"))
        assert "env" in settings
        assert settings["env"]["TIANJI_WORKER_ID"] == "总控"

    def test_key_file_in_injected(self, monkeypatch, tmp_path):
        """land_cards 的 key 文件落到 injected/。"""
        home = tmp_path / "home"
        monkeypatch.setenv("TIANJI_HOME", str(home))
        conn = connect()
        ops = __import__("tianji.ops", fromlist=["ensure_defaults"])
        ops.ensure_defaults(conn)
        ident = {"worker_id": "总控", "secret": ""}
        key_name = "k1"
        wizard.land_cards(conn, Path(home), ident, [{
            "shell": "codex", "source": "key", "key_value": "sk-123",
            "base_url": "https://x", "model": "m", "key_name": key_name}])
        conn.close()
        assert (injected_dir() / f"{key_name}.key").exists()
        assert not (home / "keys").exists()  # 不再建 home/keys


# ===================================================================
# 读取方从 injected/ 能读到
# ===================================================================

class TestReadersFindFiles:
    def test_read_secret_finds_injected(self, monkeypatch, tmp_path):
        """_read_secret 从 injected/ 读 ctrl-secret.txt。"""
        home = tmp_path / "home"
        monkeypatch.setenv("TIANJI_HOME", str(home))
        injected_dir().mkdir(parents=True, exist_ok=True)
        (injected_dir() / "ctrl-secret.txt").write_text(
            "test-s", encoding="utf-8")
        assert _read_secret(Path(home)) == "test-s"

    def test_read_ctrl_secret_finds_injected(self, monkeypatch, tmp_path):
        """webapp._read_ctrl_secret 从 injected/ 读 ctrl-secret.txt。"""
        home = tmp_path / "home"
        monkeypatch.setenv("TIANJI_HOME", str(home))
        injected_dir().mkdir(parents=True, exist_ok=True)
        (injected_dir() / "ctrl-secret.txt").write_text(
            "web-s", encoding="utf-8")
        assert _read_ctrl_secret() == "web-s"
