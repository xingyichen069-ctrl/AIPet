# AIPet

本地版本：`0.3.1-beta.2`。更新记录见 [小日和施工日志](CHANGELOG.md)，版本留档与回退方式见 [版本矿坑](docs/版本管理.md)。Windows 便携包请先看 [Windows 使用说明](Windows使用说明.md)。

> 本地增强版：已加入对话接续、约定提醒、安静陪伴、记忆操作、工作状态动作和文本附件。使用方法见 [新增功能说明](新增功能说明.md)。下面保留原版文档，原有界面说明可能与新版不同。

一个住在电脑里的角色。**没有声音**，只有文字。

它分成三块，彼此解耦，你可以只改其中一块：

| 模块 | 在哪 | 谁维护 | 你怎么动它 |
|---|---|---|---|
| **人格** | `persona/SOUL.md`<br>`persona/BOUNDARIES.md` | 你 | 直接编辑，用中文写 |
| **记忆** | `persona/PROFILE.md`<br>`memory/journal.jsonl` | 程序 + agent | 可以手改，也可以只看 |
| **可视化** | `view/memory.html` | 程序生成 | 双击打开 |
| **形象 + 思考强度** | `src/pet.py`<br>`data/thinking.json` | 你 | 双击 `启动桌宠.bat` |

---

## 已经接上的东西

```
agent 名    小日和
模型        deepseek-flash（DeepSeek-V4.1-Flash）
QQ 信道     已配置且 connected
MCP         aipet（已注册，待启用）
独立大脑    brain.py（已配好 API key，实测通过）
```

**两种对话方式，共用同一套人格和记忆：**

| 方式 | 入口 | 谁在思考 |
|---|---|---|
| 图形界面 | 双击角色 → 对话窗口 | `brain.py` 直连 DeepSeek |
| agent / IM | Cherry Studio / QQ | Cherry Studio 的 agent |

两者读写同一批文件，所以**记忆是共享的**。

### 哪些功能需要开着 Cherry Studio

| 功能 | 要开 Cherry Studio 吗 | 为什么 |
|---|---|---|
| **桌宠**（双击桌面图标） | **不用** | `brain.py` 直连 `api.deepseek.com`，全程不经过 Cherry |
| 对话窗口 / 思考面板 / Live2D | **不用** | 纯本地，依赖只有 PySide6 |
| 记忆读写 / 搜索 / 代理检测 | **不用** | 都是本地文件和直连 |
| **QQ 上的小日和** | **要** | QQ 通道是 Cherry 提供的，agent 也跑在里面 |
| **每晚 23:30 的记忆整理** | **要** | 是 Cherry 的定时任务 |
| **MCP 工具** | **要** | MCP 挂在 Cherry 的 agent 上 |
| 右键「打开记忆面板」 | 不用 | 本地生成 HTML |

**一句话：桌宠是完全独立的，Cherry Studio 关了照样用。**
QQ 和定时任务依赖 Cherry Studio，关了就收不到消息、也不会自动整理记忆。

> 实测过：`brain.py` / `pet.py` 的 import 里**没有任何 cherry 相关模块**，
> 第三方依赖只有 `PySide6`（界面）、`ddgs`（搜索）、`live2d`（模型）、`socks`（代理）。
> 全部是本地库，没有一个需要外部服务常驻。

---

## 独立大脑（brain.py）

`brain.py` 让面板上的参数全部真正生效——它直接按档位设置
`model` / `max_tokens` / 思考模式，不再受 Cherry Studio 的配置牵制。

```bash
python src/brain.py setkey sk-xxxx    # 配置 API key（会立刻验证）
python src/brain.py check             # 验证连通性
python src/brain.py ask "在吗"         # 问一句
python src/brain.py ask "深入分析…" --level deep --show-reasoning
python src/brain.py chat              # 命令行对话
```

**实测效果**（同一个问题，五档对比）：

```
档位      thinking  effort  思维链   回复
frugal    关闭       -        0     瑞利散射，短波蓝光更容易被空气分子散开。
max       开启      max      231    阳光里蓝光波长短，被空气分子散射得最凶，
                                    从四面八方晃进你眼睛——所以抬头是蓝的。
```

> **⚠️ 两个 DeepSeek 的限制**（详见 [配置手册](docs/思考强度配置手册.md)）：
> 1. **思考模式下 `temperature` 无效** —— 官方明确说"不会报错，但也不会生效"
> 2. **思考强度会塌成三档** —— `medium` 和 `high` 都映射到 `high`，
>    所以「认真」和「深究」在推理强度上是同一档

### 它能调用的工具

`brain.py` 有工具调用能力，工具在 `src/local_tools.py`：

| 工具 | 作用 | 哪些档位可用 |
|---|---|---|
| `get_time` | 当前日期时间、星期 | 全部 |
| `get_system` | 电量、开机时长、系统 | 全部 |
| `recall` | 检索记忆库 | 全部 |
| `remember` | 写入记忆库 | 全部 |
| `web_search` | 联网搜索 | 除「省电」外（省电档 search=off） |

界面里工具调用是**显示出来**的：

```
你：现在几点？我电脑还有电吗？
小日和：
⚙ get_time()    → 2026年09月14日 周一 20:23（晚上）
⚙ get_system()  → 系统：Windows 11

八点二十三，周一。

电量 76%，插着电，不慌。

就是电脑已经开了 23 小时——你也是？
```

显出来是故意的——你能看见它**真的去查了**，而不是凭空编一个时间。

> **⚠️ 两个工具调用的坑**（官方文档写的）：
> 1. 带 `tools` 的请求，**后续所有请求必须完整回传 `reasoning_content`**，
>    即使那轮没实际调用工具。不回传会 400。
>    `brain.py` 已处理。
> 2. `ddgs` 的 **news 后端在国内基本不通**（实测英文超时、中文无结果）。
>    `tools.py` 会自动退回 text + 一周内的时间过滤。

---

## 桌面角色

双击 **`启动桌宠.bat`** 就能看到它。

```
┌──────────┐   ┌─────────────────────┐
│          │   │  思考强度            │
│   角色   │   │ ⟳ ○ ◔ ◑ ◕ ●        │
│  图片    │   │  ─────────────────  │
│          │   │  深究 · 完整推理+自检 │
└──────────┘   │  记忆 5000 · 推理 high│
               └─────────────────────┘
  左键拖动         点角色切换显隐
```

| 操作 | 效果 |
|---|---|
| 左键单击角色 | 显示 / 隐藏思考强度面板 |
| 左键拖动 | 移动（面板跟着走，位置会记住） |
| 悬停档位 | 显示该档详情 |
| 右键 | 菜单：鼠标穿透、总在最前、换形象、退出 |
| `Esc` | 收起面板 |

**形象是 Live2D 模型**（桃濑日和 PRO），实时渲染、自动眨眼呼吸、
视线跟着鼠标走、点身体有反应。换档时表情会变——调到「深究」会眯眼歪头。

```bash
python src/make_icon.py --live2d     # 从模型重新生成图标
```

改大小 / 换模型 → `data/config.json` 的 `live2d` 段。
关掉 `enabled` 就退回静态图片模式（用 `assets/character.png`）。

> ⚠️ **Live2D 有个必须知道的坑**：Qt 默认给 Core Profile 上下文，
> 而 Live2D 的着色器用 `gl_FragColor`（兼容模式才有）。
> 结果是**着色器编译通过但什么都不画**，且没有任何报错。
> `live2d_widget.py` 已强制用 `CompatibilityProfile`，别改回去。

**思考强度控制什么、怎么调** → 见 **[docs/思考强度配置手册.md](docs/思考强度配置手册.md)**

一句话版：五档强度真实改变**记忆深度**（350 → 9000 tokens）、
推理投入、是否联网、是否自检。默认「自动」，按问题难度自己挑。

```bash
python src/thinking.py list                  # 档位总览
python src/thinking.py set deep              # 命令行换档
python src/thinking.py auto "帮我分析这个设计"  # 看自动会判成哪档
```

---

## 快速开始

```bash
cd D:\CXY\AIPet

# 1. 灌入演示数据，看看效果（正式使用时删掉 journal.jsonl 重来）
python src/memory.py demo

# 2. 生成可视化面板，会自动打开浏览器
python src/render.py --open

# 3. 看看一次对话会注入什么上下文
python src/memory.py context "报告 进度"
```

零依赖，纯 Python 标准库，Python 3.10+ 就能跑。

---

## 目录结构

```
AIPet/
├── persona/                    ← 人格（你改这里）
│   ├── SOUL.md                 它是谁：性格、说话方式、脾气
│   ├── BOUNDARIES.md           禁区：隐私红线、不假装、情绪边界
│   └── PROFILE.md              关于你的事实（agent 自动维护，也可手改）
│
├── memory/                     ← 记忆
│   ├── journal.jsonl           事件时间线，只追加
│   ├── state.json              关系状态：亲密度、情绪、未兑现的承诺
│   ├── summaries/              压缩后的周/月摘要
│   └── archive/                降采样的原始条目（不是删除）
│
├── view/
│   └── memory.html             可视化面板（程序生成，别手改）
│
├── knowledge/                  ← 本地资料（默认不启用，见下）
├── assets/character.png        角色形象（占位石头，可替换）
├── docs/
│   ├── 个性化设置总览.md        ★ 所有能改的东西都在这
│   ├── 思考强度配置手册.md      面板参数详解
│   └── QQ机器人接入提示词.md    让 QQ 上也有人格
├── 启动桌宠.bat                 双击运行
│
├── src/
│   ├── memory.py               记忆引擎：写入 / 检索 / 评分 / 压缩
│   ├── consolidate.py          「睡前整理」：连接 Python 和 LLM 的接口
│   ├── render.py               生成可视化面板
│   ├── tools.py                网络搜索 / 网页抓取（带缓存）
│   ├── knowledge.py            本地资料检索（轻量知识库）
│   ├── thinking.py             思考强度引擎：档位解析 / 自动判定
│   ├── pet.py                  桌面窗口 + 思考面板 + 对话窗口（PySide6）
│   ├── brain.py                独立大脑：直连 DeepSeek API（插槽 B）
│   ├── local_tools.py          它能调用的本地工具（时间/电量/搜索/记忆）
│   ├── live2d_widget.py        Live2D 渲染组件（QOpenGLWidget）
│   ├── proxy.py                代理自动检测
│   ├── make_icon.py            生成 .ico 图标
│   ├── backup.py               记忆备份 / 回滚
│   └── mcp_server.py           MCP server：把上面这些暴露给 agent（零依赖）
│
└── data/
    ├── config.json             参数：token 预算、衰减率、隐私词表、搜索后端
    ├── thinking.json           思考强度：五档预设 + 自动判定规则
    ├── pet_state.json          窗口位置等运行时状态
    └── cache/                  搜索结果缓存
```

---

## 它是"原生"还是跑在 agent 框架上？

**都不是——AIPet 本身没有大脑。** 它是一套**纯本地的数据层 + 工具层**：

```
memory.py       纯文件读写，没有一行在调 LLM
consolidate.py  Python 备料 → LLM 改写 → 打时间戳
render.py       生成 HTML
tools.py        网络请求（唯一会联网的模块）
```

大脑是插槽式的，两个插槽任选：

| | 插槽 A：Cherry Studio agent | 插槽 B：独立 Python 进程 |
|---|---|---|
| 模型 | 现成的 | 自己调 API |
| 网络搜索 | 已内置 | 用 `tools.py` |
| 定时 / 信道 | 已内置 | 要自己写 |
| 代码量 | 几乎为零 | 全套自己来 |
| 自主性 | 受框架约束 | 完全自主 |

**把 AIPet 接到 agent 上 → 用 `src/mcp_server.py`（已在设置里注册，需手动启用）。**

```
设置 → MCP → 找到 aipet → 启用
```

挂上后 agent 每轮可调 `aipet_context(问题)`，一次拿到思考档位 +
关系状态 + 相关回忆 + 用户档案。**面板上的滑块至此才真正接进对话流程。**

> MCP 和信道是两件事：MCP 解决「它思考时读面板」，信道解决「它主动找你」。
> 想让面板生效只需要 MCP，不需要信道。

**建议：先用 A 跑起来**（把 `SOUL.md` / `PROFILE.md` 接到 agent 上，
配上定时任务和 IM 通道），**等确有需要再迁到 B**。
数据格式两边完全一样，迁移不用改任何文件。

---

## 网络搜索

```bash
python src/tools.py search "查询词" -n 5
python src/tools.py search "今日新闻" --news
python src/tools.py fetch https://example.com
python src/tools.py cache --clear
```

需要装一次依赖：

```bash
pip install ddgs
# 或临时用：uv run --with ddgs --no-project python src/tools.py search "…"
```

**后端对比（2026 年现状）：**

| 后端 | 免费额度 | 要卡吗 | 说明 |
|---|---|---|---|
| `ddgs`（默认） | 无限 | 不要 | 聚合 bing/brave/google/duckduckgo，无需 key |
| `tavily` | 1000 次/月 | 不要 | 返回 LLM 就绪的摘要，质量最好 |
| `searxng` | 自建无限 | — | 完全自主，需自己部署 |

在 `data/config.json` 的 `tools` 段切换后端、填 key、设代理。

**缓存是省额度最有效的手段**——同一 query 在 TTL 内不重复请求。
默认 general 1 小时 / news 15 分钟 / 网页 24 小时，都可在 config 里调。

> 国内如果 ddgs 连不上，在 `tools.proxy` 填 `http://127.0.0.1:7890`
> 或 `socks5h://127.0.0.1:9150`。

---

## 本地知识库 —— 默认关闭，因为多数时候你不需要

RAG / 向量库解决的是「模型不知道的私有知识」。但桌宠场景下：

| 它需要知道的 | 从哪来 | 需要知识库吗 |
|---|---|---|
| 关于你的事 | `PROFILE.md` + `journal.jsonl` | **不需要**，已有检索够用 |
| 世界上的事 | 搜索接口 | **不需要**，现查就行 |
| 一批固定的私有资料 | ← 只有这个才需要 | 需要 |

真 RAG 的成本是：文档解析 + 分块 + embedding + 向量库 + 重排。
单人本机小规模，这是**过度工程**。

**什么时候才值得开：**
1. 你有一批要反复查的私有资料（500 页教材、你的笔记库、长规范文档）
2. 你想让它"读过"某个世界观设定，用来角色扮演
   ← **这才是桌宠场景里真正有意思的用法**，比查资料有价值得多

**用起来：**

```bash
# 1. 把资料丢进 knowledge/（支持 .md .txt；PDF/Word/PPT 先转成 Markdown）
# 2. 建索引
python src/knowledge.py build
# 3. 检索
python src/knowledge.py search "梯度下降的学习率怎么选"
# 4. 改 data/config.json → knowledge.enabled = true
```

即使开了，用的也是**关键词检索，不是向量检索**。
两千块以内的资料，关键词比 embedding 更快、更准、还不用装依赖。
上万块再考虑向量库。

检索逻辑复用了记忆引擎的评分思路（覆盖率 + 精确度 + 标题加成），
所以两套系统的行为是一致的，你只需要理解一次。

---

## 记忆系统怎么设计的

朴素做法是"把对话历史全塞进 prompt"。那样跑一周就会崩：prompt 无限膨胀、
同一个事实被记 20 遍、模型在噪音里找不到重点。

这里用了六个手段：

### 1. 事实与事件分离

| | `PROFILE.md` | `journal.jsonl` |
|---|---|---|
| 内容 | 当前状态快照 | 历史时间线 |
| 写入 | 覆盖 / 合并去重 | 只追加 |
| 上限 | 有（会去重） | 无（靠压缩控制） |

"用户在准备考研"是**事实**，进 PROFILE；"9月14日用户说下周三要交报告"是**事件**，进 journal。
两者生命周期完全不同，混在一起必然膨胀。

### 2. 每条记忆带三个元数据

- `importance` 1–5 —— 生日和"今天吃了什么"不该一个待遇
- `emotion` —— 情绪标签，影响它回话的语气
- `decay` —— `permanent` / `slow` / `normal`，决定遗忘速度

### 3. 检索评分，而不是全量注入

```
score = 关键词匹配 × 重要度 × 时间衰减 × 标签加成
```

时间衰减用 `exp(-λ·天数)`：

| 类别 | λ | 半衰期 | 用于 |
|---|---|---|---|
| `permanent` | 0 | 永不 | 生日、核心事实 |
| `slow` | 0.004 | ~173 天 | 长期目标、稳定偏好 |
| `normal` | 0.02 | ~35 天 | 日常事件 |

**不需要向量库。** 中文用字 + 相邻二字做特征，两千条以内比 embedding 更快更省。
等 journal 上万条再考虑上向量检索。

### 4. Token 预算硬约束

记忆注入有上限（默认 1200 tokens），按分数截断。这是防 prompt 膨胀的闸门。
另外有"近期兜底"——最近 4 条无论分数高低都注入，保证短期连贯性。

### 5. 分层压缩

```
30 天内   → 原样保留
30 天以上 → 压成周摘要，原始条目进 archive/
180 天以上→ 再压成月摘要
```

老记忆是**降采样**，不是删除。原始数据永远在 `archive/` 里，你随时能翻。

### 6. 隐私过滤

`data/config.json` 里的 `blocked_patterns` 命中时，整句拒绝入库。
默认拦密码、密钥、token、身份证、银行卡等。

---

## 「睡前整理」怎么运作

这是整个设计里最关键的一环。**Python 和 LLM 分工明确：**

```
Python（机制层）              Agent / LLM（语义层）
─────────────────            ──────────────────────
存、检索、评分、衰减           理解、抽取事实、合并去重
压缩、归档、可视化             判断什么重要、怎么表述
```

Python 做不了"这句话说明了用户什么偏好"这种判断；
LLM 做不了"三年后这条记忆该不该还在"这种机械计算。所以分开。

**流程：**

```bash
# 1. Python 准备好素材
python src/consolidate.py brief

# 2. agent 读完素材，直接改写 persona/PROFILE.md
#    （合并优先于新增、写事实不写日志、过期的要删）

# 3. 打时间戳，标记本轮整理完成
python src/consolidate.py done
```

如果你把它接到了 Cherry Studio 的 agent 上，这个流程可以挂成定时任务，
每天睡前自动跑一次。

**为什么要合并而不是追加？** 因为 PROFILE 膨胀的唯一原因就是"只加不减"。
"用户喜欢咖啡"出现 20 次，prompt 就废了。

---

## 常用命令

```bash
python src/memory.py add "用户说下周三要交报告" -i 5 -t 工作,承诺 -e 压力
python src/memory.py search "报告"
python src/memory.py context "报告"          # 看注入了什么，附带 token 数
python src/memory.py stats
python src/memory.py compress                # 手动触发压缩
python src/consolidate.py brief|done|status
python src/render.py --open

# 网络搜索（需 pip install ddgs）
python src/tools.py search "查询词" -n 5
python src/tools.py fetch https://example.com
python src/tools.py cache --clear

# 本地知识库（默认关闭）
python src/knowledge.py build|search|stats

# 思考强度
python src/thinking.py list|set|auto|context|params

# 桌面角色（需要 PySide6）
python src/pet.py
```

参数：`-i` 重要度 1–5 · `-t` 标签逗号分隔 · `-e` 情绪 · `-d` 衰减类别

---

## 调参

改 `data/config.json`，保存即生效，不用重启：

- `retrieval.token_budget` —— 记忆注入上限。嫌它记不住事就调大，嫌贵就调小
- `scoring.decay_lambda` —— 遗忘速度。想让某些事记更久就调小
- `compression.daily_to_weekly_after_days` —— 多久开始压缩
- `privacy.blocked_patterns` —— 加你自己的隐私词

---

## 关于那个 HTML 面板

`view/memory.html` 是自包含的——CSS/JS 全内联，不联网、不加载 CDN，
用浏览器直接打开就行。

面板上每一项左边那个竖直色条是**记忆热度**：时间衰减后的残余强度。
永久类永远 100，日常记忆会慢慢变暗。低于阈值就不再进对话上下文，
但**不会被删除**——你还能在面板上看到它。

---

## 下一步

这套东西现在只有"记忆和人格"。要变成桌宠还需要两步：

1. **表现层** —— 一个透明置顶窗口。见 `SOUL.md` 里设定的形象
2. **信道** —— 让它能主动找你（Cherry Studio 的 IM 通道 / 定时任务）

信道的接法见下节。
