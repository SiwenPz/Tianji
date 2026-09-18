#!/usr/bin/env bash
# detect.sh — 检查本机是否已配置 kimi-code 子代理模型池
#
# 用法:
#   bash detect.sh
#
# 退出码:
#   0 — 已配置 [secondary_model] 段,并打印池内模型清单
#   1 — 未配置,打印提示信息

set -euo pipefail

# 配置文件路径 - 支持 KIMI_CODE_HOME 环境变量
if [[ -n "${KIMI_CODE_HOME:-}" ]]; then
  CONFIG_FILE="${KIMI_CODE_HOME}/config.toml"
else
  CONFIG_FILE="${HOME}/.kimi-code/config.toml"
fi

# 检查配置文件是否存在
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "未配置子代理模型池"
  echo "指引: 请创建 ${CONFIG_FILE}, 并添加 [secondary_model] 与 [secondary_model.models] 配置段。"
  exit 1
fi

# 检查是否存在 [secondary_model] 或 [secondary_model.models] 配置段
if ! grep -qE '^(\[secondary_model\]|\[secondary_model\.models\])' "${CONFIG_FILE}"; then
  echo "未配置子代理模型池"
  echo "指引: 请在 ${CONFIG_FILE} 中添加 [secondary_model] 段及其 models 子表。"
  exit 1
fi

# 已配置,打印池内模型清单
echo "已配置子代理模型池,清单如下:"
echo "----------------------------------------"

# 提取 [secondary_model.models] 段下的键名(等号左侧),排除空行与注释
awk '
  /^\[secondary_model\.models\]/ { in_models=1; next }
  /^\[/ { in_models=0 }
  in_models && /=/ {
    key=$0
    sub(/[[:space:]]*=.*/, "", key)
    sub(/[[:space:]]+$/, "", key)
    if (key !~ /^#/ && key != "") print key
  }
' "${CONFIG_FILE}"

echo "----------------------------------------"
exit 0