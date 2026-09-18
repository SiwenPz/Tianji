# Codex 初始化入口说明

这不是第二套初始化流程。所有宿主统一执行同目录 `BOOTSTRAP.md`。

Codex 的已验证约束是：原生子代理能覆盖 `model`，但不能依赖角色文件单独切换 provider；因此主会话和 Tianji 子代理共用一个支持 `/v1/responses` 的路由入口。晨检只有在 provider、菜单、全部角色绑定和原生 parent→`tianji-worker` 证明都匹配当前配置时才返回 `READY`。

`NEED_HOST_ADAPTER`、`NEED_ROLE_CONFIG` 或 `NEED_ROUTE_PROOF` 均回到共享引导对应步骤。切换 Codex 主 provider 必须先获得用户明确同意，不能默认绑定角色，也不能跳过重启和实证。
