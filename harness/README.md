# AIPet 轻量插件 harness

这个目录是独立的插件生命周期应用，不参与桌面窗口、QQ 网关或人格运行时。它参考 Codex 插件的清单和隔离边界，把插件操作固定为：

```text
发现 → 清单校验 → 隔离测试 → 打包哈希 → 暂存 → 健康检查 → 原子启用
```

插件根目录必须包含 `.aipet-plugin/plugin.json`。清单必须列出全部文件、入口、测试命令和健康命令；路径越界、符号链接、重复文件、哈希不符和不完整包都会在执行代码前拒绝。

常用命令：

```text
python -m harness --root . list
python -m harness --root . validate plugins/aipet-mcp
python -m harness --root . test plugins/aipet-mcp
python -m harness --root . package plugins/aipet-mcp
python -m harness --root . auto plugins/aipet-mcp
python -m harness --root . run aipet-mcp -- --serve
```

`auto` 是默认投入流程：测试通过后生成带 `release-manifest.json` 的压缩包，安装到 `data/harness/plugins/<name>/<version>`，通过健康检查后才更新 `data/harness/registry.json`。旧版本保留在磁盘，失败不会覆盖当前启用版本；`data/harness/` 属于本机运行状态，已被 `.gitignore` 排除。
