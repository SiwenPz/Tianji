"""钩子脚本分发与管理(票 13,规格书 17 章): 副本+版本指纹+对账重装。

- 分发=副本(否决指针共享方案): 启动器按壳模板+实例参数渲染,写入各 worker
  隔离配置目录;文件头带模板版本指纹;适配器/钩子/statusline 全进应然清单(11.5)
- 对账三处(17.2): ①spawn 前必查(先写后跑) ②监控器巡检(~30min 节流)
  ③`tianji hooks reinstall` 手动刷一批
- 三态指纹(17.3): 指纹一致+版本旧→机械重生成;指纹不一致=用户手工改过→
  不自动碰,差异报告+升级总控,用户裁决保留或 `hooks reinstall` 重置官方版
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import messages, ops
from .db import now

HOOK_TEMPLATE_VERSION = "v1"

_FP_PREFIX = "# tianji-hook-fingerprint:"

_STATUSLINE_PY = '''"""天机 statusline 上报脚本(14.1①): claude 状态栏调用,上下文占用%上报账本。"""

import json
import os
import subprocess
import sys


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    pct = (data.get("context_pct") or data.get("used_percentage")
           or (data.get("context") or {}).get("used_percentage"))
    if pct is None:
        return
    worker = os.environ.get("TIANJI_WORKER_ID")
    if not worker:
        return
    subprocess.run([sys.executable, "-m", "tianji", "quota", "report",
                    worker, str(pct)], capture_output=True)


main()
'''

_HOOKS_MANIFEST = {
    "note": "天机钩子清单(按壳模板 6.2 适配器映射;装壳配置时合并本清单)",
    "hooks": ["session_start", "session_end", "stop", "user_prompt",
              "pre_tool_use", "post_tool_use", "permission_request",
              "subagent_start", "subagent_stop"],
}


def _fp(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _artifacts(inst) -> list:
    """按壳模板+实例参数渲染生成物(17.1;安装位置按各壳机制,隔离目录内)。"""
    shell = inst["shell"]
    iso = inst["isolated_dir"]
    if not iso:
        return []
    base = Path(iso)
    arts = []
    # ① 适配器脚本副本(拷贝副本最稳、删了能补、与壳加载零摩擦)
    adapter = Path(__file__).parent / "adapters" / f"{shell}_hook.py"
    if adapter.is_file():
        body = adapter.read_text(encoding="utf-8")
        arts.append({"name": "adapter", "path": base / f"tianji_{shell}_hook.py",
                     "kind": "py", "body": body})
    # ② statusline 脚本(claude 壳,14.1① 装钩子时一并装)
    if shell == "claude":
        arts.append({"name": "statusline", "path": base / "tianji_statusline.py",
                     "kind": "py", "body": _STATUSLINE_PY})
    # ③ 钩子清单(JSON 配置,合并进各壳配置域)
    arts.append({"name": "hooks-manifest",
                 "path": base / "tianji-hooks.json",
                 "kind": "json",
                 "body": json.dumps(_HOOKS_MANIFEST, ensure_ascii=False,
                                    indent=1)})
    return arts


def _render(art: dict) -> str:
    """生成物=指纹头+本体(JSON 用 _tianji 键,脚本用注释头)。"""
    if art["kind"] == "json":
        doc = json.loads(art["body"])
        doc["_tianji"] = {"version": HOOK_TEMPLATE_VERSION}
        # 指纹单独存应然清单,JSON 内只标版本(指纹=内容哈希见 _fp)
        return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    return (f"{_FP_PREFIX}{HOOK_TEMPLATE_VERSION}\n" + art["body"])


def install_instance(conn, name: str) -> dict:
    """安装/重装(17.1): 渲染三脚本写入隔离目录+全部进应然清单。"""
    inst = conn.execute("SELECT * FROM instances WHERE name=?",
                        (name,)).fetchone()
    if inst is None:
        raise KeyError(f"实例 {name} 未注册")
    written = []
    manifest = []
    for art in _artifacts(inst):
        content = _render(art)
        art["path"].parent.mkdir(parents=True, exist_ok=True)
        art["path"].write_text(content, encoding="utf-8")
        written.append(str(art["path"]))
        manifest.append({"name": art["name"], "path": str(art["path"]),
                         "fingerprint": _fp(content),
                         "version": HOOK_TEMPLATE_VERSION})
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"expected:{name}",)).fetchone()
    expected = json.loads(row["value"]) if row else {}
    expected["hooks"] = manifest
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (f"expected:{name}", json.dumps(expected, ensure_ascii=False), now()))
    ops.audit(conn, "hooks_install",
              {"instance": name, "written": written,
               "version": HOOK_TEMPLATE_VERSION})
    return {"instance": name, "written": written}


def reconcile_instance(conn, name: str) -> dict:
    """三态对账(17.2/17.3): 缺失→补装;版本旧(未被手改)→重生成;手改→不碰+升级。"""
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"expected:{name}",)).fetchone()
    expected = json.loads(row["value"]) if row else {}
    manifest = expected.get("hooks") or []
    if not manifest:
        return install_instance(conn, name) | {"status": "installed_fresh"}
    results = []
    for entry in manifest:
        path = Path(entry["path"])
        if not path.exists():
            results.append("missing")
            continue
        current = path.read_text(encoding="utf-8")
        if _fp(current) != entry["fingerprint"]:
            # 非天机生成内容(用户手工改过): 不自动碰,差异报告升级总控
            ops.audit(conn, "hooks_reconcile_diff",
                      {"instance": name, "path": str(path),
                       "expected_fp": entry["fingerprint"],
                       "found_fp": _fp(current)})
            messages.send(conn, "escalation", "hooks",
                          {"worker_id": name,
                           "reason": f"钩子生成物被手工修改,对账不自动碰: {path}"
                                     f"(保留现状或 tianji hooks reinstall {name} 重置官方版)"},
                          "controller")
            results.append("user_modified")
        elif entry["version"] != HOOK_TEMPLATE_VERSION:
            results.append("outdated")
        else:
            results.append("ok")
    if any(r in ("missing", "outdated") for r in results):
        r = install_instance(conn, name)
        return {"instance": name, "status": "regenerated",
                "detail": results, "written": r["written"]}
    if any(r == "user_modified" for r in results):
        return {"instance": name, "status": "user_modified",
                "detail": results}
    return {"instance": name, "status": "ok", "detail": results}


def scan_all(conn, throttle: int = 1800) -> dict:
    """监控器巡检顺带(17.2②,~30min 节流): 差异机械可补自动补。"""
    last = int(ops._config(conn, "hooks_scan_ts") or 0)
    if now() - last < throttle:
        return {"skipped": "window"}
    out = {}
    for r in conn.execute(
            "SELECT name FROM instances WHERE is_active=1").fetchall():
        try:
            out[r["name"]] = reconcile_instance(conn, r["name"])["status"]
        except Exception as e:
            out[r["name"]] = f"error: {e}"
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        ("hooks_scan_ts", str(now()), now()))
    return {"scanned": out}
