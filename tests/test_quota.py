"""额度检测与上下文健康度(票 11 验收 1-6)。"""

import json
import sqlite3

import pytest

from tianji import ops, quota


def _reg(conn, name, window=100000):
    ops.instance_register(conn, name, "claude", "deepseek-v4-flash",
                          context_window=window)


def test_statusline_report_readable(conn, controller):
    """验收 1: statusline 上报进账本,占用百分比可读。"""
    _reg(conn, "报工")
    quota.report_context_pct(conn, "报工", 73.5)
    h = quota.context_health(conn, "报工")
    assert h["pct"] == 73.5 and h["window"] == 100000
    assert h["remaining"] == 26500


def test_transcript_usage_accumulate(conn, controller, tmp_path):
    """验收 2: 转录 usage 累加正确(claude 格式 message.usage)。"""
    tr = tmp_path / "t.jsonl"
    tr.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 100,
                                          "output_tokens": 40}}}) + "\n"
        + json.dumps({"usage": {"input_tokens": 60, "output_tokens": 10}}) + "\n"
        + "坏行不是JSON\n"
        + json.dumps({"message": {"content": "无usage"}}) + "\n",
        encoding="utf-8")
    r = quota.scan_transcript_usage(conn, "报工", str(tr))
    assert r["usage"]["input_tokens"] == 160
    assert r["usage"]["output_tokens"] == 50
    assert r["usage"]["lines"] == 2
    assert "skipped" in quota.scan_transcript_usage(conn, "报工",
                                                    str(tmp_path / "无.jsonl"))


def test_ccswitch_429_classified_not_fault(conn, controller, tmp_path):
    """验收 3: cc-switch 429→限流不判故障+实例档案归类;无库环境跳过。"""
    _reg(conn, "报工")
    db = tmp_path / "cc.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE proxy_request_logs (status_code INTEGER)")
    c.executemany("INSERT INTO proxy_request_logs VALUES (?)",
                  [(200,), (200,), (429,), (500,)])
    c.commit()
    c.close()
    r = quota.read_ccswitch(conn, str(db), "报工")
    assert r["summary"][429]["class"] == "限流"
    assert r["summary"][500]["class"] == "故障"
    notes = conn.execute("SELECT notes FROM ability_profiles"
                         " WHERE instance_name='报工'").fetchone()["notes"]
    assert "限流" in notes and "非故障" in notes
    assert quota.context_health(conn, "报工")["exhausted"] is True
    # 未装 cc-switch 的环境跳过该层
    assert "skipped" in quota.read_ccswitch(conn, str(tmp_path / "无.db"),
                                            "报工")


def test_allocator_health_check(conn, controller):
    """验收 4: 装不下→跳过;健康度低但装得下→提示续接不硬跳;已尽→暂停派新活。"""
    _reg(conn, "紧工", window=10000)
    _reg(conn, "宽工", window=100000)
    _reg(conn, "尽工", window=100000)
    quota.report_context_pct(conn, "紧工", 50)   # 剩余 5000 < normal 档 4000? 装得下;抬到 70→剩 3000 装不下
    quota.report_context_pct(conn, "紧工", 70)
    quota.report_context_pct(conn, "宽工", 90)   # 健康度低但剩 10000 装得下→提示
    quota.read_ccswitch(conn, "/nonexistent", "尽工")
    d = quota._load(conn, "尽工"); d["exhausted"] = True
    quota._save(conn, "尽工", d)
    tid = ops.task_new(conn, controller, "活", priority=1,
                       request_id="q-task")["task_id"]  # normal 档 expected 4000
    picked = ops.allocator_pick(conn, tid)
    assert picked == "宽工"      # 紧工装不下被跳,尽工已尽被跳,宽工带提示仍可选
    esc = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation'"
        " AND sender='allocator' ORDER BY seq DESC LIMIT 1").fetchone()
    assert "建议先续接" in esc["payload"]
    # 全部已尽 → 暂停派新活+通知可见
    quota.report_context_pct(conn, "宽工", 50)
    d = quota._load(conn, "宽工"); d["exhausted"] = True
    quota._save(conn, "宽工", d)
    assert ops.allocator_pick(conn, tid) is None
    esc2 = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation'"
        " AND sender='allocator' ORDER BY seq DESC LIMIT 1").fetchone()
    assert "额度已尽,暂停派新活" in esc2["payload"]


def test_monitor_scan_full_alert(conn, controller):
    """验收 5: 巡检复查——派单后涨满(≥98%)可检出,升级可见。"""
    _reg(conn, "满工")
    quota.report_context_pct(conn, "满工", 99)
    quota.monitor_scan(conn)
    esc = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation'"
        " AND sender='monitor' ORDER BY seq DESC LIMIT 1").fetchone()
    assert "将尽" in esc["payload"]


def test_quota_visible_in_cockpit(conn, controller):
    """验收 6: 额度状态驾驶舱/CLI 快照可见。"""
    _reg(conn, "见工")
    quota.report_context_pct(conn, "见工", 66)
    from tianji.cockpit import snapshot
    snap = snapshot(conn)
    cards = [c for cl in snap.values() if isinstance(cl, list)
             for c in cl if isinstance(c, dict)]
    card = [c for c in cards if c["instance_name"] == "见工"][0]
    assert card["quota_pct"] == 66


# ====================================================================
# 票 48: 死代码接线(14.1②③)与 13.1 上下文窗口读取侧
# ====================================================================

def test_transcript_usage_scan_skips_unchanged(conn, controller, tmp_path):
    """票48(14.1②): 尺寸守卫——文件没长过不重扫;追加后重扫补上不翻倍。"""
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"usage": {"input_tokens": 10,
                                        "output_tokens": 5}}) + "\n",
                  encoding="utf-8")
    r1 = quota.scan_transcript_usage(conn, "报工", str(tr))
    assert r1["usage"]["input_tokens"] == 10
    r2 = quota.scan_transcript_usage(conn, "报工", str(tr))
    assert r2["unchanged"] is True and r2["usage"]["input_tokens"] == 10
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"usage": {"input_tokens": 7,
                                      "output_tokens": 3}}) + "\n")
    r3 = quota.scan_transcript_usage(conn, "报工", str(tr))
    assert r3["usage"]["input_tokens"] == 17  # 重扫=全量重算,不翻倍


def test_monitor_scan_wires_transcript_usage(conn, controller, tmp_path,
                                             monkeypatch):
    """票48(14.1②): 巡检顺带累加转录 usage——壳声明 transcript 且登记过会话。

    假象消除证明: 之前 scan_transcript_usage 全仓无调用方,这里证明
    monitor_scan 会把转录 usage 落进该实例的额度账本。
    """
    ops.instance_register(conn, "转录工", "dsh", "deepseek-v4-flash",
                          context_window=100000)
    # dsh 转录根=DSH_HOME 环境变量;glob: sessions/*/{session_id}/session.jsonl
    home = tmp_path / "dsh-home"
    tr = home / "sessions" / "t1" / "sess-01" / "session.jsonl"
    tr.parent.mkdir(parents=True)
    tr.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 100,
                                          "output_tokens": 40}}}) + "\n"
        + json.dumps({"usage": {"input_tokens": 60, "output_tokens": 10}}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    conn.execute(
        "INSERT INTO instance_registrations"
        " (instance_name, dispatch_id, status, dcap_hash, session_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("转录工", 0, "active", "h", "sess-01", ops.now()))
    quota.monitor_scan(conn)
    d = quota._load(conn, "转录工")
    assert d["usage"]["input_tokens"] == 160
    assert d["usage"]["output_tokens"] == 50


def test_monitor_scan_wires_ccswitch(conn, controller, tmp_path):
    """票48(14.1③): 配了 cc-switch 库路径后,巡检读账目+错误码归类写档案。

    429→exhausted(暂停派新活的消费侧在 allocator_pick);补读
    usage_daily_rollups/provider_health 两张表,缺表如实报 absent。
    """
    _reg(conn, "账目工", window=100000)  # claude: 声明 transcript+ccswitch 层
    db = tmp_path / "cc.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE proxy_request_logs (status_code INTEGER)")
    c.executemany("INSERT INTO proxy_request_logs VALUES (?)",
                  [(200,), (429,)])
    c.execute("CREATE TABLE usage_daily_rollups"
              " (day TEXT, input_tokens INTEGER, output_tokens INTEGER)")
    c.execute("INSERT INTO usage_daily_rollups VALUES" " ('2026-08-01', 500, 100)")
    c.execute("CREATE TABLE provider_health (provider TEXT, healthy INTEGER)")
    c.execute("INSERT INTO provider_health VALUES ('kimi', 1)")
    c.commit()
    c.close()
    ops.config_set(conn, controller, "ccswitch_db_path", str(db),
                   request_id="cc-db")
    quota.monitor_scan(conn)
    d = quota._load(conn, "账目工")
    assert d["exhausted"] is True
    cc = d["ccswitch"]
    # 账本 blob 经 JSON 往返,status_code 键是字符串(直读返回里才是 int 键)
    assert cc["proxy_request_logs"]["429"]["class"] == "限流"
    assert cc["usage_daily_rollups"]["present"] is True
    assert cc["usage_daily_rollups"]["input_tokens"] == 500
    assert cc["provider_health"]["present"] is True
    notes = conn.execute(
        "SELECT notes FROM ability_profiles WHERE instance_name='账目工'"
    ).fetchone()["notes"]
    assert "限流" in notes and "非故障" in notes


def test_ccswitch_403_classified_not_exhausted(conn, controller, tmp_path):
    """票48(14.1③): 403=权限/封禁归类写实例档案,但不是额度用尽。"""
    _reg(conn, "封工")
    db = tmp_path / "c403.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE proxy_request_logs (status_code INTEGER)")
    c.executemany("INSERT INTO proxy_request_logs VALUES (?)", [(403,), (403,)])
    c.commit()
    c.close()
    r = quota.read_ccswitch(conn, str(db), "封工")
    assert r["summary"][403]["class"] == "权限/封禁"
    notes = conn.execute(
        "SELECT notes FROM ability_profiles WHERE instance_name='封工'"
    ).fetchone()["notes"]
    assert "权限/封禁" in notes and "非限流" in notes
    assert quota.context_health(conn, "封工")["exhausted"] is False


def test_instance_register_fills_context_window_from_key(conn, controller):
    """票48(13.1 读取侧): 探测缓存里的上下文窗口在实例注册时带出,
    14.2 健康度与 9.2 硬过滤读的 ability_profiles.context_window 因此有值。
    """
    ops.config_set(conn, controller, "shell:codex", json.dumps(
        {"binding": "env", "protocols": ["openai_chat"]},
        ensure_ascii=False), request_id="cw-shell")
    ops.config_set(conn, controller, "key:kk", json.dumps({
        "base_url": "https://api.example/v1", "protocol": "openai_chat",
        "models": [{"id": "m1", "context_window": 8000},
                   {"id": "m2", "context_window": None,
                    "context_window_status": "待实测"}],
    }, ensure_ascii=False), request_id="cw-key")
    ops.instance_register(conn, "窗工", "codex", "m1", key_name="kk")
    prof = conn.execute(
        "SELECT context_window FROM ability_profiles"
        " WHERE instance_name='窗工'").fetchone()
    assert prof["context_window"] == 8000
    # 待实测模型: 拿不到数→保持 0=如实未知,不瞎填
    ops.instance_register(conn, "窗工2", "codex", "m2", key_name="kk")
    prof2 = conn.execute(
        "SELECT context_window FROM ability_profiles"
        " WHERE instance_name='窗工2'").fetchone()
    assert prof2["context_window"] == 0


def test_instance_register_fills_window_from_provider_entry(conn, controller):
    """票48(13.1): 集成注册表路径同样带出——credential→供应商条目→models。"""
    ops.config_set(conn, controller, "shell:codex", json.dumps(
        {"binding": "env", "protocols": ["openai_chat"]},
        ensure_ascii=False), request_id="cw-shell2")
    ops.config_set(conn, controller, "key:kk2", json.dumps({
        "base_url": "https://api.example/v1", "protocol": "openai_chat",
        "models": [{"id": "pm"}],
    }, ensure_ascii=False), request_id="cw-key2")
    ops.config_set(conn, controller, "integration_provider:rp", json.dumps({
        "base_url": "https://rp.example/v1", "protocol": "openai_chat",
        "models": [{"id": "pm", "context_window": 16000}],
    }, ensure_ascii=False), request_id="cw-prov")
    ops.config_set(conn, controller, "credential:kk2", json.dumps({
        "provider": "rp", "key_ref": "x.key"}, ensure_ascii=False),
        request_id="cw-cred")
    ops.instance_register(conn, "窗工3", "codex", "pm", key_name="kk2")
    prof = conn.execute(
        "SELECT context_window FROM ability_profiles"
        " WHERE instance_name='窗工3'").fetchone()
    assert prof["context_window"] == 16000
