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
