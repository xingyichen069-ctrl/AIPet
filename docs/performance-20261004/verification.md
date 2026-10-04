# 验证与问题修正记录

日期：2026-10-04。所有改动、测试输出和临时副本都在独立 main 候选内。借用原安装的 Windows Python 解释器与现有依赖，没有安装新包、覆盖原安装、美术工程或私人运行资料。

最终功能提交为 `bdf752b`（控制器与接线）、`99bb8ca`（测试台与回放）；本文件单独记录两包的联合验证，后续拆分时可按对应测试重新执行。

## 最终结果

| 验证 | 实际结果 | 本地证据（相对候选根目录） |
| --- | --- | --- |
| 完整 Windows 隔离回归 | 执行 300 项；298 通过、2 跳过；23.112 秒 | `work/verification/performance-final-tests.log` |
| 带 Qt 诊断输出的完整回归 | 同样 300 项，298 通过、2 跳过；23.382 秒 | `work/verification/performance-full-cleanup-check.log` |
| Linux 纯核心/适配契约 | 26 项通过；不导入 Qt | `work/verification/performance-core-linux.log` |
| Linux 记录/回放 | 10 项通过，包括标准库独立子进程入口 | `work/verification/performance-trace-linux.log` |
| 六个无界面示例 | 全部重算并核对成功 | `work/verification/performance-scenarios-result.json` |
| 实际 GUI 导出 → Linux 无界面核对 | 3 个输入事件、1400 ms；最终状态与摘要一致 | `work/verification/performance-lab-recording.json`、`performance-lab-replay-result.json` |
| 新控制器 + 原生 Hiyori | 8 个检查节点通过，2 次渲染器释放，0 个失败 | `work/verification/performance-native-result.json`、`performance-native.log` |
| 上一轮的原生故障恢复 | 11 个检查节点通过，5 次渲染器释放，0 个失败 | `work/verification/live2d-recovery-result.json`、`performance-recovery-native.log` |
| 原独立 Live2D 接口 | 8 种活动/安静/恢复显示检查通过 | `work/verification/performance-legacy-native.log` |
| 可见测试台 | 实时示意、回放、原生 Hiyori 3 个节点通过 | `work/verification/performance-lab-result.json`、`performance-lab-smoke.log` |

Windows 为 Python 3.14.5、PySide6 6.11.2、live2d-py 0.7.0.4。两项跳过均为既有备份符号链接测试，原因是 Windows 进程没有创建链接权限；本次新增测试及 Live2D 检查没有因此跳过。

相对上一轮的 247 项回归，本次新增 53 项：核心/映射/模拟原生适配 26 项、Qt 接线 8 项、记录回放 10 项、测试台 8 项，以及渲染器第一帧代数保护 1 项。实际 OpenGL 和可见界面检查另行执行，不混入上述 300 项。

## 覆盖边界

- 独立状态：工作进度不覆盖基础心情，动作结束/打断恢复最新底层状态，取消不会播放错误反馈。
- 排队：优先级、同级先后、容量 2、等待 2 秒、重复合并拒绝、同刻过期先于动作完成、每轮回复动作额度，以及固定种子的 1000 次事件不变量检查。
- 迟到保护：新轮次、重复结束、旧 worker 的真实 Qt 排队信号、取消后迟到内容、旧模型代数、旧动作令牌、旧完成提示计时器。
- 期限：动作结束、拖拽释放丢失、120 秒无进度、持续 reasoning/content/tool 刷新期限；粗细时间推进一致。
- 映射：不支持动作、缺少心情、无效参数/范围/时长、实际 SDK 范围、恢复原生默认值、缺组/编号拒绝、同一动作只启动一次。
- 接线：专注安静、用户点击优先于回复动作、悬停反馈排队、runtime 只发变化、退出后拒绝新动作、受限语言意图整份校验。
- 回放：文件往返、所有示例和所有倍速、暂停/定位/重启、早期事件篡改而最终状态相同、历史滚出后摘要仍完整、布尔/数字类型不同也不算相同结果。
- 文件：版本、未知字段、事件倒序、超限、巨型数值、NaN、重复 JSON 字段、深层嵌套、无效编码、原子替换失败保留旧文件、文件末尾换行也纳入大小上限。
- 测试台：无 expected 时不伪称已对照核验；坏文件不覆盖未保存记录；旧原生预览回调不能进入新记录；清理失败仍回到示意绘制；新建和退出停止旧活动。
- 隔离：单独子进程阻止导入 brain/memory/thinking/companion/pet/人格运行入口并阻止 Python 连接调用，仍能建立测试台、开始模拟轮次、关闭窗口；无界面入口在 `-S` 禁用 site 包后仍可运行。

## 原生与画面证据

新原生脚本 `tests/performance_native_smoke.py` 打开自己的 Hiyori 窗口，核对实际参数值；检查同一 motion 只启动一次，并等待真实时钟超过 1900 ms 后确认循环动作由控制器停止。还检查拖拽时停止 motion、旧回调拒绝、安静、取消、模型重载、窄参数范围恢复 SDK 默认值，以及实际不存在的动作组被拒绝。

脚本包装本次模型的 StartMotion、StopAllMotions、SetParameterValue 和 DestroyRenderer，断言调用时拥有正确的活动 GL 上下文。它记录了 5 次 motion 启动、7 次停止和 2 次显式渲染资源释放。缺少 `MissingSmokeMotion` 的 notice 是有意注入的检查，不是交付模型缺件。

`tests/live2d_recovery_smoke.py` 在仅含公开模板的临时安装中实例化实际 PetWindow，因此也覆盖了本次 runtime 接线后的旧故障恢复流程。原静态回退、重试、坏候选保留旧模型、窗口标志变化、运行中 Draw 故障和启动超时均通过。

画面取自真实 Qt 窗口或模型帧缓冲；检查非透明像素并查看了截图：

- `performance-native-idle.png`、`performance-native-thinking.png`、`performance-native-greet.png`、`performance-native-quiet.png`。
- `performance-lab-live.png`：1120×780 逻辑像素，当前工作/心情/动作与操作区可同时查看。
- `performance-lab-replay.png`：同样 1120×780，手动输入禁用，旧回调拒绝记录可见。
- `performance-lab-native.png`：960×680，实际 Hiyori 显示，右侧可滚动；没有被布局强制撑到屏幕以外。

截图和原始日志留在本地 `work/verification/`，不作为发行资产提交。这里验证的是本机显示、控制调用和显式释放顺序，没有测量 GPU 显存泄漏，也没有完成多设备、长时运行或美术观感验收。

## 开发中发现并处理的问题

1. **原模型动作会循环。** Hiyori 的现有 motion 都标有 `Meta.Loop=true`；按已核对时长设置应用期限，原生完成回调只允许提前结束，不作为唯一恢复途径。
2. **布尔值会和整数令牌相等。** Python 中 True 等于 1。原生 motion 缓存键增加 action/base/stopped 类型标记，避免首个动作被错当成待机缓存而跳过。
3. **只限幅目标再混合仍可能超范围。** 改为把姿态按交集范围完整写入；释放参数使用 SDK 默认值，不能把较窄映射边界误当默认值。
4. **鼠标反馈来源应区分。** 明确点击使用 user，悬停使用 system，使用户动作能按规则打断回复动作。
5. **新模型第一帧可能吃到旧动作。** 初始化/重载后等待新 epoch；就绪事件尚在 Qt 队列时不使用旧动作快照。同代数的普通进度更新不能误解锁。
6. **摘要不能只覆盖可见历史。** 采用流式 SHA-256 覆盖全部决策，512 条可见历史滚出后仍能核对完整行为。
7. **文件容量和成功文案要一致。** 校验与录制共用输入预算，保留输出空间，换行计入文件上限；缺 expected 明确标为重新计算，坏导入不替换当前记录。
8. **回放倍速切换需要先结算旧速度。** 切换速度、暂停先处理此前经过的时间，再更新时钟锚点；定位从初始状态重算。
9. **较矮窗口会被最小布局撑高。** 初次实测最小高度为 817，后调整状态区、记录表及右侧滚动容器，实测可缩至 960×680。映射选择框也补足宽度。
10. **全套回归出现 Qt 对象回收阶段异常退出。** 初次全套及带故障诊断的复测在既有并发人格导入测试期间 Aborted，堆栈标示 Garbage-collecting；仅 42 项关联子集不能复现。新增界面/桥接测试和相关渲染测试原先只 `deleteLater + processEvents`，未在没有主事件循环的测试环境中交付 DeferredDelete。补上明确的延迟删除处理后，完整诊断入口与常规入口分别完成 300 项。没有修改人格导入算法、跳过该测试或放宽断言；该证据不等于已定位 PySide/CPython 的全部底层原因。

上述失败日志保留为 `performance-full-tests.log`、`performance-full-diagnostic.log`、`performance-full-qt-diagnostic.log`；通过后的日志独立保留，没有把失败文件覆盖为成功。诊断期间遗留的任务自有测试进程单独核对处理，没有按名称停止用户的普通 AIPet。

早期在无 PySide6 的 Linux Python 下使用过宽的测试通配符，包含了两个 Qt 模块并导致导入失败；之后明确分为 Linux 纯逻辑测试与已有 Windows Qt 环境测试。记录为 `performance-linux-first.log`，不把缺依赖误记作已完成 GUI 验证。

## 复核命令

在候选根目录，以 PowerShell 为例：

```powershell
$py = 'D:\CXY\AIPet-0.5.0\AIPet-0.5.0\.venv\Scripts\python.exe'
& $py -X utf8 -B tools/test_core.py
& $py -X utf8 -B tests/performance_native_smoke.py
& $py -X utf8 -B tests/performance_lab_smoke.py
& $py -X utf8 -B tests/live2d_recovery_smoke.py
& $py -X utf8 -B tests/live2d_smoke.py
```

`tools/test_core.py` 复制公开代码、模板和测试到候选内部的临时目录，并阻止通常的 Python 网络连接。原生脚本需要活动 Windows 桌面及 OpenGL 驱动，会短暂打开自己的窗口并关闭。它们不是操作系统级网络沙箱；本轮没有启动真实脑模型请求。

Linux 或任意标准库 Python 可复核纯部分：

```text
python -B -m unittest discover -s tests -p test_performance.py
python -B -m unittest discover -s tests -p test_performance_trace.py
python -B tools/performance_lab.py --scenario late_reply
```

使用 discovery，不能把仅运行测试文件但没有执行测试视为成功。源码还进行了 AST 解析、两份 JSON 映射解析、Git 差异空白检查和工作包内文档链接核对。

## 交付限制

没有调用真实 AI/API 来推断情绪；没有导入灵梦运行资产；没有变更人格、记忆算法或原安装的设置。未来语言意图的质量和频率、真实皮套动作的美术验收、口型同步及长时间运行仍需分别验证。当前 beta 版本号保持不变，本轮完成的是可以独立测试和拆分接入的基础设施。
