"""账本 schema: 11 张表 + DB CHECK 兜底(主校验在 CLI 层,否决触发器)。"""

# 消息类型 20 种三族(3.1 + 架构师裁判 8.2 + 工人求助 20 + 可选总控评估 9.2③)
MSG_TYPES = (
    "task_suggest", "dispatch", "worker_done", "review_verdict", "escalation",
    "grill_round", "grill_answer", "plan_confirm", "plan_reject",
    "final_confirm", "reopen",
    "instance_register", "instance_unbind",
    "event",
    "architect_verdict", "architect_confirm",
    "worker_help", "worker_help_reply",
    "alloc_review", "alloc_review_result",
)

# 收件角色(3.1: recipient_role 按角色寻址;event 不寻址)
ROLES = ("controller", "architect", "allocator", "monitor", "reviewer", "worker")

# 事件 8 类公共交集(6.3)
EVENT_TYPES = (
    "session_start", "session_end", "stop", "user_prompt",
    "pre_tool_use", "post_tool_use", "permission_request",
    "subagent_start", "subagent_stop",
)

# 任务九态(4.1)
TASK_STATES = (
    "new", "discussing", "awaiting_plan_confirm", "dispatched", "executing",
    "reviewing", "awaiting_final_confirm", "archived", "reopened",
)

# 派单七态(5.1): 五态 + cancelled(强制干预专属终态)
DISPATCH_STATES = ("issued", "active", "done", "stale", "requeue", "escalate", "cancelled")

# 登记行三态(11.3)
REG_STATES = ("spawned", "active", "closed")

# 派生状态四态(6.3)
SESSION_STATES = ("working", "waiting", "done", "idle")

# 审核轴(8.1)
AXES = ("spec", "quality")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  type TEXT NOT NULL CHECK (type IN (/*MSG_TYPES*/)),
  sender TEXT NOT NULL,
  recipient_role TEXT CHECK (recipient_role IN (/*ROLES*/) OR recipient_role IS NULL),
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
  consumer_id TEXT PRIMARY KEY,
  last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS receipts (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permission_rulings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT NOT NULL,
  dispatch_id INTEGER,
  session_id TEXT NOT NULL DEFAULT '',
  tool TEXT NOT NULL DEFAULT '',
  request_payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','allowed','denied')),
  decided_by TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN (/*TASK_STATES*/)),
  priority INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'user',
  project_dir TEXT NOT NULL DEFAULT '',
  verify_cmd TEXT NOT NULL DEFAULT '',
  scope_guard TEXT NOT NULL DEFAULT '',
  architect_verdict TEXT NOT NULL DEFAULT '',
  retry_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  worker_id TEXT NOT NULL,
  worker_role TEXT NOT NULL DEFAULT 'worker' CHECK (worker_role IN ('worker','reviewer')),
  axis TEXT NOT NULL DEFAULT '' CHECK (axis IN ('spec','quality','')),
  status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN (/*DISPATCH_STATES*/)),
  dcap_hash TEXT NOT NULL,
  expect_min INTEGER NOT NULL DEFAULT 10,
  task_dir TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  worktree_path TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
  name TEXT PRIMARY KEY,
  shell TEXT NOT NULL,
  model TEXT NOT NULL,
  key_name TEXT NOT NULL DEFAULT '',
  isolated_dir TEXT NOT NULL DEFAULT '',
  launch_cmd TEXT NOT NULL DEFAULT '',
  display_mode TEXT NOT NULL DEFAULT '前台',
  thinking_level TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_registrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instance_name TEXT NOT NULL,
  dispatch_id INTEGER,
  status TEXT NOT NULL DEFAULT 'spawned' CHECK (status IN (/*REG_STATES*/)),
  session_id TEXT,
  dcap_hash TEXT NOT NULL,
  task_path TEXT NOT NULL DEFAULT '',
  pid INTEGER,
  abnormal INTEGER NOT NULL DEFAULT 0,
  offline_suspicion INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS ability_profiles (
  instance_name TEXT PRIMARY KEY,
  shell TEXT NOT NULL,
  model TEXT NOT NULL,
  key_name TEXT NOT NULL DEFAULT '',
  isolated_dir TEXT NOT NULL DEFAULT '',
  skills TEXT NOT NULL DEFAULT '[]',
  permission_granularity TEXT NOT NULL DEFAULT '',
  context_window INTEGER NOT NULL DEFAULT 0,
  score REAL NOT NULL DEFAULT 60,
  model_source_score REAL NOT NULL DEFAULT 0,
  key_body_score REAL NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS configs (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_states (
  session_id TEXT PRIMARY KEY,
  instance_name TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (/*SESSION_STATES*/)),
  last_seq INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


def render_schema() -> str:
    """把 Python 常量展开进 DDL(唯一真实来源在常量,避免两处漂移)。"""
    ddl = SCHEMA
    ddl = ddl.replace("/*MSG_TYPES*/", ",".join(repr(t) for t in MSG_TYPES))
    ddl = ddl.replace("/*ROLES*/", ",".join(repr(r) for r in ROLES))
    ddl = ddl.replace("/*TASK_STATES*/", ",".join(repr(s) for s in TASK_STATES))
    ddl = ddl.replace("/*DISPATCH_STATES*/", ",".join(repr(s) for s in DISPATCH_STATES))
    ddl = ddl.replace("/*REG_STATES*/", ",".join(repr(s) for s in REG_STATES))
    ddl = ddl.replace("/*SESSION_STATES*/", ",".join(repr(s) for s in SESSION_STATES))
    return ddl



