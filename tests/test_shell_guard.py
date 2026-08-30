"""架构守卫(票 46 签约): 通用控制流无壳名字面量分支——防回归机械扫描。

豁免: 适配器模板数据文件 (tianji/adapters/template.py) 正常存壳名数据;
注册表定义行 (RENDERERS / BACKENDS 等) 合法。
"""

import importlib
import inspect

import pytest


# ---------------------------------------------------------------------------
# 通用控制流模块(适配器模板数据文件/注册表定义行 豁免,不在此扫描)
# ---------------------------------------------------------------------------
_GENERIC_MODULES = [
    "tianji.monitor",
    "tianji.ops",
    "tianji.render",
    "tianji.wizard",
    "tianji.permission",
    "tianji.hooks",
    "tianji.events",
    "tianji.ctrlprotocols",
    "tianji.shellrender",
    "tianji.integrations",
    "tianji.quota",
    "tianji.pool",
    "tianji.ctrlsession",
    "tianji.daemon",
    "tianji.cockpit",
    "tianji.proxy",
]

_SHELL_NAME_KEYWORDS: set[str] = set()  # 动态填充


def _collect_shell_names() -> set[str]:
    """动态收集已知壳名(从出厂模板注册表)。"""
    from tianji.adapters.template import list_templates
    return set(list_templates())


_SHELL_NAME_KEYWORDS = _collect_shell_names()


_BRANCH_PREFIXES = (
    "if shell ==", "elif shell ==",
    # adapter 是 dict(模板/壳条目数据),adapter == "壳名" 是恒 False 死分支
    # (monitor.py:166 踩坑实证),一并守卫
    "if adapter ==", "elif adapter ==",
)


def _is_branch_line(stripped: str) -> bool:
    return stripped.startswith(_BRANCH_PREFIXES)


def _scan_shell_name_branches(module_name: str, known_shells: set[str]) -> list[str]:
    """扫描模块源码,返回发现的壳名字面量分支行 (if/elif shell|adapter == "壳名")。

    仅检测控制流分支: 以 if/elif shell == 或 if/elif adapter == 开头的行。
    """
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    violations: list[str] = []
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if not _is_branch_line(stripped):
            continue
        for sn in known_shells:
            if f'"{sn}"' in line or f"'{sn}'" in line:
                violations.append(f"  {module_name}:{lineno}: {line.strip()}")
                break
    return violations


# ---------------------------------------------------------------------------
# 守护测试
# ---------------------------------------------------------------------------

def test_no_shell_name_literal_branches_in_generic_modules():
    """通用控制流零壳名字面量分支——防回归扫描(PO-46 验收第 2 条)。

    扫描策略:
    - 目标 = tianji/ 通用控制流模块
    - 豁免 = 适配器模板数据文件 + 注册表定义行
    - 检测模式 = if/elif shell|adapter == "壳名" (已知壳名)
    """
    known_shells = _collect_shell_names()
    assert known_shells, "至少需要一个已知壳名才能跑守卫"

    all_violations: list[str] = []
    for mod_name in _GENERIC_MODULES:
        try:
            violations = _scan_shell_name_branches(mod_name, known_shells)
            all_violations.extend(violations)
        except (ImportError, OSError):
            pass

    assert all_violations == [], (
        "检测到壳名字面量分支(应改为数据驱动读取壳条目/注册表):\n"
        + "\n".join(all_violations)
    )


def test_guard_catches_deliberate_injection():
    """自证测试: 故意植入的壳名分支能被守卫捕获,证明防回归有效。"""
    known_shells = _collect_shell_names()
    assert known_shells, "至少需要一个已知壳名才能跑自证"
    fake_shell = next(iter(known_shells))

    fake_src = (
        "def bad_func(shell):\n"
        f'    if shell == "{fake_shell}":\n'
        '        return "special"\n'
        '    return "generic"\n'
    )
    hits = _scan_shell_name_branches_from_src(fake_src, known_shells)
    assert hits, (
        f"守卫应捕获故意植入的壳 '{fake_shell}' 分支但未捕获——守卫测试不生效"
    )


def test_guard_catches_adapter_dict_branch():
    """自证: adapter(dict) == "壳名" 死分支模式也能被捕获(monitor.py:166 踩坑)。"""
    known_shells = _collect_shell_names()
    fake_shell = next(iter(known_shells))
    fake_src = (
        "def check(adapter):\n"
        f'    if adapter == "{fake_shell}":\n'
        "        return True\n"
        "    return False\n"
    )
    hits = _scan_shell_name_branches_from_src(fake_src, known_shells)
    assert hits, (
        f"守卫应捕获 adapter == '{fake_shell}' 死分支但未捕获——守卫测试不生效"
    )


# ---------------------------------------------------------------------------
# 扫描辅助(供自证 + 主守卫复用)
# ---------------------------------------------------------------------------

def _scan_shell_name_branches_from_src(src: str, known_shells: set[str]) -> list[str]:
    """从原始源码文本中扫描壳名字面量分支。"""
    violations: list[str] = []
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if not _is_branch_line(stripped):
            continue
        for sn in known_shells:
            if f'"{sn}"' in line or f"'{sn}'" in line:
                violations.append(f"L{lineno}: {line.strip()}")
                break
    return violations
