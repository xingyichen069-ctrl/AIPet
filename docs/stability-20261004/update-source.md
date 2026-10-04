# 更新来源识别

工作包日期：2026-10-04。独立基线与授权范围见 [README](README.md)。本项可单独合入，不依赖 Live2D 修复，不改变 VERSION 或标签。

## 解决的问题

原检查器从全仓标签中选择最高版本，可能把其他开发分支的标签当成 main 更新；本机版本号相同或更高时，还会直接显示“已是最新”，无法反映同版本号下的提交差异。

现在默认先核对 `xingyichen069-ctrl/AIPet` 的 `main` 提交，再读取该提交的 VERSION。源码链接和 ZIP 地址都固定到这个提交，避免检查期间分支前移导致版本和下载来源错配。成功结果始终展示仓库、分支和短提交号。

只比较有效语义版本号，包括数字预发布段、正式版与预发布版、构建元数据。版本相同明确提示“未比较本机文件”；本机更高不推断兼容性。来源不可核对时报告错误，不改查标签。本机 VERSION 缺失或无效时报告未知，不伪装成 0.0.0。

## 改动与接口

- `src/update.py`：`fetch_source()` 固定来源；`target()` 验证配置；`check()` 保留原结果字段，增加 `repo`、`branch`、`commit`、`download_url`、`comparison`。`comparison` 的成功状态为 `remote_newer`、`same_version`、`local_ahead`、`local_unknown`。
- `describe()` 供命令行、桌面与 QQ 共用，不含 URL；桌面单独提供固定提交页面按钮。
- `src/pet.py`：检查结果使用共同文案，各种成功状态都能查看已核对的源码。
- `fetch_tags()` 仅保留兼容诊断用途，检查与推荐路径不调用它。
- README、迁移指南、命令行参考、版本管理说明同步行为，并保留对旧检查工具的提醒。

可用 `update.repo`、`update.branch` 改变检查目标；不自动猜测本机分支。不改动文件、下载、安装或迁移资料。两次请求复用既有代理客户端，第二次使用剩余请求时间；代理探测有独立开销，不承诺严格的总墙钟时限。

## 验证记录

Windows Python 3.14.5，隔离公开模板环境执行：

```powershell
& 'D:\CXY\AIPet-0.5.0\AIPet-0.5.0\.venv\Scripts\python.exe' -X utf8 -B tools/test_core.py test_update_source test_search_proxy
```

32 项通过。新增用例覆盖默认 main、fork 与多层分支名、固定提交读取、禁止标签推荐、版本相同/本机更高/本机未知、非法配置、异常 ref/SHA/版本响应、HTTP/JSON 失败、剩余超时和语义版本排序。原有代理行为回归同时通过。

另用只有公开模块与配置模板的临时目录，通过产品实际请求路径只读核对 GitHub：main 提交为 `c0da0943e795f5682d715315d3d97a17f7a967d3`，VERSION 为 `0.5.0-beta.2`，返回 `same_version`，源码与 ZIP 链接均固定到该提交。结果保存在本地 `work/verification/update-public-read.json`，不含私人配置或密钥。

## 限制与拆分

该检查不是发行认证、文件完整性检查或自动更新器。它不能判断同版本号下本机是否缺少提交，也不能证明用户当前源码来自哪个分支。GitHub 请求成功不代表下载、安装或迁移成功；这些操作仍按独立目录迁移流程执行。

合入时选择本说明所在的 `fix: pin update checks to the configured branch commit` 提交即可。若目标分支同时修改了 `PetWindow.on_update_done`，保留共同结果文案和固定提交链接即可，其余 Live2D 代码无依赖。用户要求拆分记录，因此本轮施工和验证写在此处；发行方合入时再按该分支约定整理版本日志。
