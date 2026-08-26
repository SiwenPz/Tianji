"""可选总控评估(票 06 验收 4/9.2③): alloc_review 请求+alloc_review_result 回写参与软排序。"""

import json

import pytest

from tianji import ops


def _register_shell_key(conn, controller, key_name):
    ops.config_set(conn, controller, "shell:codex",
                   json.dumps({"binding": "env", "protocols": ["stdio"],
                               "isolated_dir_mode": "env_home"},
                              ensure_ascii=False),
                   request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}",
                   json.dumps({"base_url": f"https://api.example.com/{key_name}",
                               "models": [{"id": "step-router-v1",
                                           "display_name": "R",
                                           "context_window": 200000}],
                               "protocol": "stdio"},
                              ensure_ascii=False),
                   request_id=f"r-k-{key_name}")


def _make_worker(conn, controller, name, skills, context_window,
                 permission, score=60, key_name=None):
    if key_name is None:
        key_name = f"key-{name}"
    _register_shell_key(conn, controller, key_name)
    ops.instance_register(
        conn, name, "codex", "step-router-v1",
        skills=json.dumps(skills, ensure_ascii=False),
        context_window=context_window,
        permission_granularity=permission,
        key_name=key_name)
    conn.execute(
        "UPDATE ability_profiles SET score=? WHERE instance_name=?",
        (score, name))
    return name


def _to_dispatched(conn, controller, title, priority=0):
    tid = ops.task_new(conn, controller, title, priority=priority,
                       request_id=f"r-{title}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r-{title}-{s}")
    return tid


def _alloc_review_messages(conn):
    return conn.execute(
        "SELECT * FROM messages WHERE type='alloc_review'"
        " ORDER BY seq").fetchall()


def _alloc_review_result_messages(conn):
    return conn.execute(
        "SELECT * FROM messages WHERE type='alloc_review_result'"
        " ORDER BY seq").fetchall()


class TestAllocReviewDisabled:
    """默认(开关关): 不写请求消息,纯按表现分软排序。"""

    def test_no_request_message_when_disabled(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-dr1")
        _make_worker(conn, controller, "w2", ["coding"], 50000,
                     "project", score=90, key_name="ak-dr2")
        tid = _to_dispatched(conn, controller, "测试任务", priority=0)
        assert ops._config(conn, "allocator_review_enabled") == "0"
        pick = ops.allocator_pick(conn, tid)
        assert pick == "w2"
        assert _alloc_review_messages(conn) == []


class TestAllocReviewRequest:
    """开关开: 候选+任务特征写账本(alloc_review),同任务幂等只发一次。"""

    def test_request_written_with_candidates_and_task_features(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-req1")
        _make_worker(conn, controller, "w2", ["data"], 50000,
                     "project", score=70, key_name="ak-req2")
        ops.config_set(conn, controller, "allocator_review_enabled", "1",
                       request_id="r-en1")
        tid = _to_dispatched(conn, controller, "数据分析", priority=2)
        pick = ops.allocator_pick(conn, tid)
        # 无评估结果时行为与关闭一致: 按表现分降序
        assert pick == "w1"
        msgs = _alloc_review_messages(conn)
        assert len(msgs) == 1
        payload = json.loads(msgs[0]["payload"])
        assert payload["task_id"] == tid
        assert payload["task"]["title"] == "数据分析"
        assert payload["task"]["priority"] == 2
        assert "expected_size" in payload["task"]
        names = {c["name"] for c in payload["candidates"]}
        assert names == {"w1", "w2"}
        assert payload["candidates"][0]["score"] == 80
        # 收件角色: 请求发往总控
        assert msgs[0]["recipient_role"] == "controller"

    def test_request_idempotent_single_message(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-idem1")
        ops.config_set(conn, controller, "allocator_review_enabled", "1",
                       request_id="r-en2")
        tid = _to_dispatched(conn, controller, "重复调用", priority=0)
        ops.allocator_pick(conn, tid)
        ops.allocator_pick(conn, tid)
        ops.allocator_pick(conn, tid)
        assert len(_alloc_review_messages(conn)) == 1


class TestAllocReviewSubmit:
    """总控回写评估结果: 入参校验 + 幂等 + 账本审计。"""

    def test_submit_writes_result_and_audit(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-sub1")
        tid = _to_dispatched(conn, controller, "评估回写", priority=0)
        ops.alloc_review_submit(conn, controller, tid,
                                {"w1": 95}, request_id="r-sub1")
        msgs = _alloc_review_result_messages(conn)
        assert len(msgs) == 1
        payload = json.loads(msgs[0]["payload"])
        assert payload["task_id"] == tid
        assert payload["scores"] == {"w1": 95}
        # 结果回执发往分配器
        assert msgs[0]["recipient_role"] == "allocator"
        # 审计留痕
        audits = conn.execute(
            "SELECT * FROM audit WHERE action='alloc_review_submit'"
        ).fetchall()
        assert len(audits) == 1
        assert json.loads(audits[0]["detail"])["task_id"] == tid

    def test_submit_unknown_task_raises(self, conn, controller):
        with pytest.raises(KeyError):
            ops.alloc_review_submit(conn, controller, 99999, {"w1": 90},
                                    request_id="r-bad1")

    def test_submit_unregistered_instance_raises(self, conn, controller):
        tid = _to_dispatched(conn, controller, "未注册对象", priority=0)
        with pytest.raises(ValueError, match="未注册实例"):
            ops.alloc_review_submit(conn, controller, tid, {"幽灵": 90},
                                    request_id="r-bad2")

    def test_submit_requires_request_id(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-sub2")
        tid = _to_dispatched(conn, controller, "缺幂等键", priority=0)
        with pytest.raises(ValueError, match="request_id"):
            ops.alloc_review_submit(conn, controller, tid, {"w1": 90})

    def test_submit_idempotent_replay(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-sub3")
        tid = _to_dispatched(conn, controller, "重放", priority=0)
        ops.alloc_review_submit(conn, controller, tid, {"w1": 90},
                                request_id="r-replay")
        ops.alloc_review_submit(conn, controller, tid, {"w1": 90},
                                request_id="r-replay")
        assert len(_alloc_review_result_messages(conn)) == 1


class TestAllocReviewSorting:
    """评估结果参与软排序: 评估分按 bonus 折算加成,可反超表现分。"""

    def test_review_score_flips_selection(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-sort1")
        _make_worker(conn, controller, "w2", ["coding"], 50000,
                     "project", score=75, key_name="ak-sort2")
        ops.config_set(conn, controller, "allocator_review_enabled", "1",
                       request_id="r-en3")
        tid = _to_dispatched(conn, controller, "评估反超", priority=0)
        # 无评估: w1(80) 胜出
        assert ops.allocator_pick(conn, tid) == "w1"
        # 总控评估: w2 满分 100 → +10(bonus=10 全量), 75+10=85 > 80
        ops.alloc_review_submit(conn, controller, tid, {"w2": 100},
                                request_id="r-sort")
        assert ops.allocator_pick(conn, tid) == "w2"

    def test_bonus_scaled_by_review_score(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-scale1")
        _make_worker(conn, controller, "w2", ["coding"], 50000,
                     "project", score=75, key_name="ak-scale2")
        ops.config_set(conn, controller, "allocator_review_enabled", "1",
                       request_id="r-en4")
        ops.config_set(conn, controller, "allocator_review_bonus", "8",
                       request_id="r-bonus1")
        tid = _to_dispatched(conn, controller, "部分加成", priority=0)
        # w2 评估 50 分 → 加成 8*0.5=4, 75+4=79 < 80, 仍 w1 胜
        ops.alloc_review_submit(conn, controller, tid, {"w2": 50},
                                request_id="r-scale")
        assert ops.allocator_pick(conn, tid) == "w1"

    def test_latest_result_wins(self, conn, controller):
        _make_worker(conn, controller, "w1", ["coding"], 50000,
                     "project", score=80, key_name="ak-latest1")
        _make_worker(conn, controller, "w2", ["coding"], 50000,
                     "project", score=75, key_name="ak-latest2")
        ops.config_set(conn, controller, "allocator_review_enabled", "1",
                       request_id="r-en5")
        tid = _to_dispatched(conn, controller, "取最新评估", priority=0)
        ops.alloc_review_submit(conn, controller, tid, {"w2": 100},
                                request_id="r-latest-a")
        ops.alloc_review_submit(conn, controller, tid, {"w2": 20},
                                request_id="r-latest-b")
        # 最新评估 w2=20 → 加成 2, 75+2=77 < 80 → w1 胜
        assert ops.allocator_pick(conn, tid) == "w1"
