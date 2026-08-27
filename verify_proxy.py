"""快速验证 proxy 核心逻辑(不依赖 pytest)。"""

import sys, os, json, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tianji.db import connect, tianji_home
from tianji import ops
from tianji.proxy import CircuitBreaker, PoolRouter, _verify_token, _pool_json
from tianji.pool import pool_create, pool_add_member, pool_rotate_token
from tianji.integrations import register_custom_provider, register_credential, model_entry

# Setup temp home
tmp = tempfile.mkdtemp()
os.environ["TIANJI_HOME"] = tmp
conn = connect()
ops.ensure_defaults(conn)

# 1. Register controller
r = ops.instance_register(conn, "总控", "claude", "deepseek-v4-flash", controller=True)
controller = {"worker_id": "总控", "secret": r["secret"]}

# 2. Register provider
from tianji import integrations
prov = "test-prov"
register_custom_provider(conn, controller, prov,
    base_url="http://127.0.0.1:19999", protocol="openai_chat",
    auth_style="bearer", request_id="rp-1")
entry = integrations._config(conn, "integration_provider:" + prov)
entry["models"] = [model_entry({"id": "test-model"})]
conn.execute("UPDATE configs SET value=? WHERE key=?",
    (json.dumps(entry, ensure_ascii=False), "integration_provider:" + prov))

# 3. Register credential + key file
key_dir = os.path.join(tmp, "keys")
os.makedirs(key_dir, exist_ok=True)
key_file = os.path.join(key_dir, "test.key")
with open(key_file, "w") as f: f.write("test-key-12345")

register_credential(conn, controller, "testcred", provider=prov,
    key_ref=key_file, request_id="rc-1")

# 4. Create pool
pool_create(conn, controller, "testpool", members=["testcred"], request_id="cp-1")

# ---- Tests ----

# CB test
print("=== CircuitBreaker ===")
cb = CircuitBreaker(min_samples=3)
assert cb.state == "closed"
assert cb.allow() == True
for _ in range(3): cb.record_failure()
assert cb.state == "open"
assert cb.allow() == False
print("PASS: CB opens after threshold")

cb2 = CircuitBreaker(min_samples=15, error_threshold=0.7)
for _ in range(14): cb2.record_failure()
assert cb2.state == "closed"
print("PASS: CB no-trip below min_samples")

# PoolRouter test
print("=== PoolRouter ===")
router = PoolRouter(conn)
m, cred, prov_entry = router.pick("testpool", "test-model", "openai_chat")
assert m == "testcred"
assert cred is not None
assert prov_entry is not None
print("PASS: PoolRouter picks member")

# Verify no match for wrong model
m2, _, _ = router.pick("testpool", "nonexistent-model", "openai_chat")
assert m2 is None
print("PASS: PoolRouter filters by model")

# CB skip
# Force open by setting opened_at in the future
router3 = PoolRouter(conn)
router3._load_breakers("testpool", ["testcred"])
cb3 = router3._breakers["testcred"]
cb3._state = "open"
cb3._opened_at = time.monotonic() + 9999  # far future
m3, _, _ = router3.pick("testpool", "test-model", "openai_chat")
assert m3 is None
print("PASS: PoolRouter skips circuit-broken member")

# Persist - set state to open with recent opened_at
router._breakers["testcred"]._state = "open"
router._breakers["testcred"]._opened_at = time.monotonic()
router._breakers["testcred"]._window = [False, False]
router._breakers["testcred"]._half_ok = 0
router._persist_breakers("testpool")
pool = _pool_json(conn, "testpool")
print(f"  Persisted circuit: {pool['circuit']['testcred']['state']}")
assert pool["circuit"]["testcred"]["state"] == "open"
print("PASS: Circuit state persisted")

# Token test
print("=== Token Verify ===")
assert _verify_token(conn, "testpool", "xxx") == False  # no token yet, but empty returned ...
# After rotate
pool_rotate_token(conn, controller, "testpool", request_id="rt-1")
stored = conn.execute("SELECT value FROM configs WHERE key='pool:token:testpool'").fetchone()["value"]
assert _verify_token(conn, "testpool", stored) == True
assert _verify_token(conn, "testpool", "wrong") == False
assert _verify_token(conn, "testpool", "") == False
print("PASS: Token verification")

# Daemon proxy lifecycle
print("=== Daemon Proxy Lifecycle ===")
from tianji.daemon import daemon_start, daemon_stop, daemon_status
r = daemon_start(interval=1, web_port=8821)
assert r["ok"] == True
assert r["proxy_port"] > 0
print("PASS: daemon_start includes proxy_port", r)

deadline = time.time() + 12
while time.time() < deadline:
    st = daemon_status()
    if st.get("proxy_alive"):
        break
    time.sleep(0.3)

st = daemon_status()
assert st["proxy_alive"] == True, f"proxy not alive: {st}"
assert st["proxy_pid"] > 0
print("PASS: proxy is alive after daemon_start")

# Kill proxy → should restart
from tianji.daemon import _kill_pid
old_pid = st["proxy_pid"]
_kill_pid(old_pid)

deadline2 = time.time() + 15
while time.time() < deadline2:
    st = daemon_status()
    if st.get("proxy_alive") and st["proxy_pid"] != old_pid:
        break
    time.sleep(0.3)

st = daemon_status()
assert st["proxy_pid"] != old_pid
assert st["proxy_pid"] > 0
print("PASS: proxy auto-relaunched after kill")

daemon_stop()
st = daemon_status()
assert st.get("proxy_alive") == False
assert st.get("proxy_pid") == 0
print("PASS: proxy stopped")

conn.close()
print("\n=== ALL QUICK TESTS PASSED ===")
