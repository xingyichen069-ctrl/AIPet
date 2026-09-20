# AIPet MCP 插件

这是 AIPet 的第一个独立插件适配。它不复制人格、记忆或 API 凭据，只把现有 `src/mcp_server.py` 作为可校验的 MCP 入口交给 harness 管理。

插件生命周期由仓库根目录的 `python -m harness` 负责：

```text
python -m harness --root . list
python -m harness --root . auto plugins/aipet-mcp
python -m harness --root . run aipet-mcp -- --serve
```

`--selftest` 只走 MCP 握手、工具清单、未知工具拒绝和通知行为；`--health` 只验证 AIPet 根目录和工具清单。真正服务模式使用 stdio，不会自动连接网络或启动桌面窗口。
