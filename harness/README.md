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
python -m harness --root . guide "创建一个离线 hello 插件" --apply
```

`auto` 是默认投入流程：测试通过后生成带 `release-manifest.json` 的压缩包，安装到 `data/harness/plugins/<name>/<version>`，通过健康检查后才更新 `data/harness/registry.json`。旧版本保留在磁盘，失败不会覆盖当前启用版本；`data/harness/` 属于本机运行状态，已被 `.gitignore` 排除。

`guide` 使用 OpenAI 兼容的 `/chat/completions` 做对话引导。模型只能返回结构化插件草案，宿主在 `work/harness-guided/` 生成临时源目录，再复用 `auto` 的测试、打包、健康检查和原子启用流程。模型凭据只从当前进程的 `AIPET_HARNESS_API_KEY` 读取，不写入仓库或 AIPet 私人配置；base URL 和模型分别使用 `AIPET_HARNESS_BASE_URL`、`AIPET_HARNESS_MODEL`，也可以传 `--base-url`、`--model`。

不带 `--apply` 时只生成并校验草案；`--apply` 才会自动启用。多轮编辑可用 `--interactive`，输入 `/apply` 才投入使用：

```text
$env:AIPET_HARNESS_API_KEY="你的临时 key"
$env:AIPET_HARNESS_BASE_URL="https://你的服务/api/v1"
$env:AIPET_HARNESS_MODEL="qwen"
python -m harness --root . guide --interactive
```
