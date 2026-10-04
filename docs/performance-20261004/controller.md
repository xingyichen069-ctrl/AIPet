# 工作包 1：表演控制层与现有桌宠接线

2026-10-04。起点为本地 `codex/stability-20261004` 的 `f3b0d8d`；它以公开 main `c0da0943e795f5682d715315d3d97a17f7a967d3` 为基线，并保留上一轮更新来源与 Live2D 故障恢复修复。没有改脑模型请求、人格、边界、记忆算法或 VERSION。

## 解决的问题

此前，聊天状态、鼠标反馈和 Live2D 待机各自播放动作。新一轮回复开始后，上一轮的结束计时器可能把它切回待机；动作被拖拽打断后也没有统一的恢复依据。现在由一个控制器决定当前状态，渲染器只执行该状态。

例如：基础心情为开心，正在思考，用户播放喝茶示意；回复开始输出时仍可继续喝茶，但下面的工作状态已经变为回复中。拖拽立即取消喝茶，松手后显示“回复中 + 开心”，不会从半截茶杯动作继续。现有 Hiyori 没有喝茶动作，会明确拒绝请求；示例喝茶只在模拟角色中绘制。

## 文件与依赖

| 层 | 文件 | 责任与依赖 |
| --- | --- | --- |
| 能力映射 | `src/performance_profile.py`、`assets/performance/*.json` | 白名单语义到模型参数/动作组；只用标准库 |
| 控制核心 | `src/performance.py` | 状态、优先级、期限、轮次、拒绝理由；只用标准库 |
| Qt 时钟桥 | `src/performance_runtime.py` | GUI 线程内发送事件、每 50 ms 检查期限、状态变化时发信号 |
| 原生适配 | `src/performance_live2d.py` | 核对实际模型参数/动作能力，在 GL 回调内执行 |
| 桌宠接线 | `src/pet.py`、`src/live2d_widget.py`、`src/companion_ui.py` | 已有回复生命周期、点击/拖拽、专注安静、模型恢复与退出 |

核心不导入 Qt、brain、memory、人格或私人配置。可独立使用前两层；使用原生适配仍需要现有 Live2DWidget、PySide6 和 live2d-py。测试台是另一个工作包，详见 [testbench.md](testbench.md)。

## 三类状态与显示顺序

| 类别 | 取值 | 何时改变 |
| --- | --- | --- |
| 工作状态 activity | idle / thinking / searching / replying / done / error | 程序提供真实任务生命周期；测试台提供合成事件 |
| 基础心情 mood | calm / happy / sad / curious / worried | 显式心情意图；不会因为短动作结束而被覆盖 |
| 临时动作 action | greet / acknowledge / react / drink_tea | 能力支持且当前规则允许时播放一次有期限的动作 |

显示优先级为：关闭 → 渲染不可用 → 暂停表演 → 安静 → 拖拽 → 临时动作 → 工作状态 → 基础心情 → 待机。关闭、渲染不可用、暂停、安静与拖拽会阻止短动作。基础心情与工作状态仍独立保存，因此解除阻止后恢复的是最新状态。

参数合成顺序为基础心情、工作姿态、思考档位；后者覆盖同名参数。思考档位只在 `thinking` 时生效，使用现有 frugal / daily / serious / deep / max 名称。动作期间不再叠加基础姿态。安静/暂停会停止待机和临时 motion，保留模型自己的呼吸、眨眼；不是冻结每个原生参数或销毁渲染器。

## 调度规则

| 条件 | 处理 |
| --- | --- |
| 动作来源 | 用户 60 > 回复意图 50 > 自动反馈 20；分值由程序确定，语言侧不能指定 |
| 更高优先级动作 | 立即取代当前动作；被打断动作不恢复 |
| 相同/较低优先级 | 最多等待 2 个，每项等待 2 秒；优先级相同按先后顺序 |
| 重复动作名 | 当前动作或队列已有同名项时拒绝，不延长、不叠加、不提高原项优先级 |
| 回复动作过量 | 每轮最多接受 4 次；不支持、重复、阻止、队满等拒绝不消耗额度 |
| 心情实际变化 | 清空当前及等待动作；同一心情重复设置不会清空 |
| 新回复开始 | 清除回复/自动动作，保留明确的用户动作；清除上一轮完成提示期限 |
| 回复取消 | 立即关轮次，清除回复/自动动作，不播放失败反馈；用户动作可继续 |
| 回复完成/失败 | 显示 done/error 1.8 秒，并尝试普通自动反馈；受同一队列和阻止规则约束 |
| 拖拽、安静、暂停 | 清空临时动作；恢复后不会补播已取消动作 |
| 拖拽释放通知丢失 | 15 秒后恢复；重复 drag.begin 不延长同一次拖拽 |
| 工作长时间无进度 | 120 秒后关闭表演轮次，短暂显示 error 后回待机 |
| 模型不可用/重绑定/换皮套 | 增加 epoch，清空临时动作及拖拽状态；保留工作和心情 |
| 退出 | 关闭状态不可重新打开；停止 Qt 调度计时器，再释放原生渲染 |

**工作超时只保护表演状态，不取消网络请求。** 真实请求仍由原来的 worker 管理，界面仍可接收和保存正常结果。超过期限后，该轮的迟到状态/意图不再重新激活表演；下一轮才能重新开始。持续到达的 reasoning/content/tool 都会刷新期限，即使工作状态名称没有变化。当前没有为耗时工具另造心跳。

时间由调用方提供整数毫秒，不读系统墙钟。`advance()` 逐个处理已经到期的边界；粗跳、细步进、回放倍速得到相同决策。相同时间先处理队列过期，再处理工作/完成提示/拖拽/动作期限，最后接收该时刻输入；队列过期与动作结束同时发生时，过期项不会被启动。

## 事件协议 v1

所有输入是只含指定字段的对象，必须有 `kind`。未知字段、未知枚举、布尔值冒充整数、越界数据会在改变状态前拒绝。以下为程序侧接口，不能原样开放给语言模型：

| kind | 其他字段 | 含义 |
| --- | --- | --- |
| turn.begin | turn_id | 正整数且大于已见轮次，开始思考 |
| turn.phase | turn_id, phase | thinking / searching / replying；只接受当前开放轮次 |
| turn.end | turn_id, outcome | complete / error / cancelled；同轮只能结束一次 |
| mood.set | mood，可选 turn_id | 有轮次时必须是当前开放轮次；无轮次为本地设置 |
| action.request | action, source，可选 turn_id | source=user/reply/system；reply 必须绑定轮次，user 不允许绑定 |
| action.end | epoch, token, success | 原生回调；代数和令牌都匹配当前动作才有效 |
| quiet.set / suspend.set / renderer.set | value | 严格布尔值；renderer 每次表示新的绑定状态并递增 epoch |
| skin.change | profile | 完整且已校验的能力映射对象；不接收文件路径 |
| level.set | level | 现有五档思考强度 |
| drag.begin / drag.end / close | 无 | 交互和生命周期 |

输出快照包括工作状态、语义心情、`visible_mood`、当前显示层、当前动作、队列、轮次和 epoch。每条处理结果都有 accepted 与 reason，例如 `stale_turn`、`stale_action`、`unsupported_action`。这使“没动作”可以区分为没能力、正在安静、动作过期或消息迟到。

`turn_id` 防止旧回复影响新回复；`epoch + token` 防止旧皮套/旧动作回调误结束新动作。原生完成信号和 QWidget 身份检查同时存在。模型刚加载或重载后的帧还会等待新 epoch，避免就绪信号排队期间消费旧动作。

## 将来的语言侧入口

目前提供 `Engine.reply_intent(turn_id, payload, at_ms)` 和 `PerformanceRuntime.reply_intent(turn_id, payload)`，还没有把自然语言自动分类或接入真实模型生成的情绪结果。

允许的意图只有 mood 和 action，可只给其中一项：

```json
{"mood": "curious", "action": "acknowledge"}
```

轮次由程序传入，整份意图先校验再处理。不能混入原始回复文本、参数编号、路径、代码、窗口命令、来源或优先级。语法错误会整份拒绝；语法有效但皮套缺少动作时，心情仍可以应用，动作单独报告拒绝。这是“语法校验原子性”，不是所有语义结果必须一起成功。

使用纯核心的最小例子，在项目根运行并把 `src` 放入 Python 搜索路径：

```python
from performance import Engine
from performance_profile import load_profile

engine = Engine(load_profile("simulator"))
engine.dispatch({"kind": "turn.begin", "turn_id": 1}, 0)
engine.reply_intent(1, {"mood": "curious", "action": "drink_tea"}, 200)
engine.dispatch({"kind": "turn.phase", "turn_id": 1, "phase": "replying"}, 800)
state = engine.advance(10000)  # 短动作结束；工作状态与基础心情仍在
```

Qt 中通过 `PerformanceRuntime` 在其所属 GUI 线程发送事件；后台生产者应使用排队的 Qt 信号。`changed(state, profile)` 收到的是数据；模型调用留给 `paintGL`。`begin_turn()` 分配递增编号；不要由模型返回编号。

后续自然语言分类器只需生产这些有限意图。它的低置信度策略、情绪迟滞、频率控制、上下文归因和离线语料评估应单独立项；本次没有用关键词匹配冒充已经完成这些算法。

## 皮套映射与原生边界

映射 schema v1 包含 id、label、renderer、parameters、moods、activities、levels、actions，可选 idle_motion。参数最多 64 个，范围有限且有默认值；姿态只能引用已声明参数，动作时长在 100–30000 ms。Live2D 动作必须给出实际 motion group/index。映射不包含脚本、网络地址或加载模型的路径。

Hiyori 当前映射只声明已核对的能力：

| 语义 | 原生动作 | 控制器时限 |
| --- | --- | --- |
| greet | Tap / 0，hiyori_m07 | 1900 ms |
| acknowledge | Tap@Body / 0，hiyori_m09 | 1600 ms |
| react | Flick / 0，hiyori_m03 | 4200 ms |
| 待机 | Idle / 0，hiyori_m01 | 待机循环 |
| drink_tea | 未提供 | 拒绝 |

现有 motion 文件的 `Meta.Loop` 都为 true，所以上述短动作由应用期限终止，不能只等 SDK 的结束通知。文件中的时长用于本轮初始配置；不自动探测任何新皮套的动作语义。

原生适配读取实际模型的参数范围和动作组。姿态值限制在映射与模型范围交集中，完整权重写入，避免与原动作混合后超出范围。退出该姿态时，将失去控制的参数恢复为模型自己的默认值一次，随后交回原生 motion/眨眼；该默认值可以位于映射的较窄范围之外。缺少参数会报告并略过；缺少动作会报告并结束该请求，不改播一个无关动作。

缺少某种心情时，语义心情仍被保存，`visible_mood` 标示 calm，画面使用平静映射。这个区别便于以后换到支持该心情的模型。

当前普通桌宠使用 Hiyori 映射，既有静态回退不额外绘制表情。安静、失败回退、重试和菜单入口仍沿用已有流程。未增加皮套商店、任意资产载入或全局换装菜单。

## 与美术交接的关系

08_handoff 是未来资产接入依据；计划书用于理解目标。这一轮没有修改或导入灵梦资产，也没有把 V13 的肩手问题视为已验收。待美术完成后，需要核对实际参数/动作组、补专属能力映射，再分别验收点击区域、动作过渡、表情、遮挡和显示尺寸。

口型同步、语音时间轴、模型长期情绪、逐帧回放、任意皮套自动映射都不在本工作包内。原生验证的具体证据与限制见 [verification.md](verification.md)。
