# AIPet

一个住在电脑桌面上的角色。没有声音，只有文字。

当前版本 `0.4.0-beta.6`。改动见 [CHANGELOG.md](CHANGELOG.md)，版本与回退见 [版本管理](docs/版本管理.md)。对话接续、约定提醒、安静陪伴等增强功能见 [新增功能说明](新增功能说明.md)。

## 先跑起来

Windows：

1. 双击 **准备环境.bat**。第一次用要装依赖，下载约 300 MB，装完占磁盘约 800 MB
2. 双击 **启动桌宠.bat**

macOS 直接双击 `启动桌宠.command`。

前置只有一样：64 位 Python 3.10–3.14，或者 uv。两个都没装的话，准备环境.bat 会告诉你装哪个。Releases 里的便携版自带运行环境，跳过第 1 步。

## 仓库里没有的东西

仓库只放代码、模型、主题和文档。下面这些不在里面，首次运行时生成：

| 不在仓库 | 是什么 |
|---|---|
| `persona/` | 人格：`SOUL.md`、`BOUNDARIES.md`、`PROFILE.md`。要自己写 |
| `memory/` | 记忆库、关系状态、人物卡 |
| `data/` 除 `thinking.json` 外 | 密钥、配置、聊天记录 |
| `view/` | 记忆面板，程序生成 |

不放进来是因为里面有密钥和私人对话。`.gitignore` 已经拦住它们，别为了图方便改。

## 三块，互不干扰

| 模块 | 在哪 | 怎么改 |
|---|---|---|
| 人格 | `persona/SOUL.md`、`persona/BOUNDARIES.md` | 直接编辑，中文写 |
| 记忆 | `persona/PROFILE.md`、`memory/journal.jsonl` | 程序维护，也可以手改 |
| 可视化 | `view/memory.html` | 双击打开 |
| 形象与思考强度 | `src/pet.py`、`data/thinking.json` | 双击启动 |

桌宠完全本地运行，不依赖任何外部服务常驻。第三方依赖只有 PySide6（界面）、live2d（模型）、ddgs（搜索）、socks（代理）。

## 对话与主题

在「右键桌宠 → 外观 → 主题」或「对话窗 ··· → 外观 → 主题」里选：

- **东方 · 朱色结界**：朱红、米白、金色，阴阳玉和御币装饰
- **雾雨魔理沙 · 星屑魔法**：黄黑白，星形徽章与星屑
- **古明地恋 · 无意识之庭**：黄绿蓝，闭眼爱心与蓝色连线
- **琪露诺 · 冰晶雾湖**：冰蓝与白，六角雪花 —— 呼应她背后那三对六棱柱翅膀

对话窗、右键主菜单、各层子菜单与托盘菜单同步切换，保留日夜自动配色（07:00–18:00 为日间）。选择自动保存，草稿、附件和对话不受影响。字号和玻璃质感也能单独调，详见 [主题使用说明](docs/主题使用说明.md)。

## 大脑

`brain.py` 直连 DeepSeek，面板上的档位参数真正生效，不受外部框架配置牵制。

```bash
python src/brain.py setkey sk-xxxx    # 配置 key，会立刻验证
python src/brain.py check             # 验证连通性
python src/brain.py ask "在吗"
python src/brain.py ask "深入分析…" --level deep --show-reasoning
python src/brain.py chat              # 命令行对话
```

两个 DeepSeek 的限制，详见 [思考强度配置手册](docs/思考强度配置手册.md)：

1. 思考模式下 `temperature` 无效。官方说明是「不会报错，但也不会生效」
2. 思考强度实际只有三档，`medium` 和 `high` 都映射到 `high`

它能调用的工具在 `src/local_tools.py`：

| 工具 | 作用 |
|---|---|
| `get_time` | 当前日期时间、星期 |
| `get_system` | 电量、开机时长、系统 |
| `recall` | 检索记忆库 |
| `remember` | 写入记忆库 |
| `web_search` | 联网搜索（省电档关闭） |
| `mood` | 心理点：`get` / `list` / `set` / `clear` / `log` |
| `fs_list` `fs_read` | 列目录、读文本文件 |
| `fs_write` `fs_mkdir` | 写文件（可追加）、建目录 |
| `see_image` | 看图片 —— 走你自己配的视觉接口，因为她自己的脑子是纯文本的 |
| `keep_image` | 把 QQ 发来的图存进沙箱，免得被定期清理 |

### 看图要自己配一个接口

`see_image` 和 `keep_image` 走的是 `src/vision.py`，它**不绑定任何一家服务**。她自己的脑子（DeepSeek）是纯文本的，图进去只能干看着，所以需要一个带视觉的模型把图读成文字。

只要对方是 **OpenAI 兼容的 `/chat/completions`，并且支持 `image_url` 这种消息格式**就能接：

| 场景 | 例子 |
|---|---|
| 本机跑 | Ollama、vLLM、LM Studio —— 图不出本机 |
| 云上 | 各家多模态 API |
| 学校 / 公司部署 | 自己的内网 endpoint |

在 `data/secrets.json` 里填三个值：

```json
{
  "vision_base_url": "https://你的服务/v1",
  "vision_api_key": "对方的 key",
  "vision_model": "qwen-vl / gpt-4o / llava"
}
```

本地部署通常不校验 key，随便填个非空串就行。**没配就是不会看图** —— 她会照实说看不了，不会编一张图出来。

```bash
python src/vision.py            # 自检：现场造一张有字的图，真调一次
python src/vision.py 看图 <路径>
```

**配之前先想清楚图会发到哪里。** 读图是把整份文件 base64 编码后 POST 出去的：本地模型不出机器，云服务就是出门了。群聊里别人发的图走的是同一条路。

### 文件读写是沙箱的，看图不是

`fs_*` 四个工具**只能动 `D:\CXY` 里面的东西**。这是代码层拦的，不是靠她自觉：

```
读取/../../../AIPet/data/secrets.json   → 被拒
D:/CXY/../AIPet/data/secrets.json       → 被拒（合法根 + 用 .. 钻出去）
//?/C:/Windows                          → 被拒（设备路径）
```

带盘符的绝对路径如果不在沙箱里会**明确拒绝**，而不是剥成相对路径——否则「存到 D:\其他目录」会悄悄变成「存到 D:\CXY\其他目录」，你按自己说的路径去找什么都找不到。

想换沙箱根，在 `data/config.json` 里加 `tools.fs_root`。

**`see_image` 不受沙箱限制**（图可以在电脑任何地方，因为从对话窗拖进来的文件本来就在各处）。它靠另外两道闸门把关：

- **后缀白名单** —— 不是图片一律拒
- **文件头魔数** —— 后缀能改，内容改不了。文本改名成 `.png` 也过不去

这两条是必须的：读图会**把整份文件 base64 发到 vision.py 配的那个服务**（可能是外部的），没有它们，`see_image("secrets.json")` 读不出内容但密钥已经出门了。

**QQ 那边，非主人用不了 `see_image`** —— 工具在调模型之前就被摘掉了。主人的记忆里有这条规矩，但记忆是说服，这里是拦。

带 `tools` 的请求，后续所有请求必须完整回传 `reasoning_content`，即使那轮没实际调用工具。不回传会 400，`brain.py` 已处理。

`ddgs` 的 news 后端在国内基本不通，实测英文超时、中文无结果。`tools.py` 会自动退回 text 并加一周内的时间过滤。

## 桌面角色

双击 **启动桌宠.bat**。

| 操作 | 效果 |
|---|---|
| 左键单击角色 | 显示 / 隐藏思考强度面板 |
| 左键拖动 | 移动，面板跟着走，位置会记住 |
| 悬停档位 | 显示该档详情 |
| 右键 | 菜单：鼠标穿透、总在最前、换形象、退出 |
| `Esc` | 收起面板 |

形象是 Live2D 模型（桃濑日和 PRO），实时渲染、自动眨眼呼吸、视线跟着鼠标走、点身体有反应。换档时表情会变。

```bash
python src/make_icon.py --live2d     # 从模型重新生成图标
```

改大小或换模型改 `data/config.json` 的 `live2d` 段。关掉 `enabled` 就退回静态图片模式，用 `assets/character.png`。

Live2D 有个坑：Qt 默认给 Core Profile 上下文，而 Live2D 的着色器用 `gl_FragColor`，只有兼容模式才有。结果是着色器编译通过但什么都不画，而且不报错。`live2d_widget.py` 已强制用 `CompatibilityProfile`，别改回去。

思考强度五档真实改变记忆深度（350 → 9000 tokens）、推理投入、是否联网、是否自检。默认「自动」，按问题难度自己挑。完整说明见 [思考强度配置手册](docs/思考强度配置手册.md)。

```bash
python src/thinking.py list                  # 档位总览
python src/thinking.py set deep              # 换档
python src/thinking.py auto "帮我分析这个设计"  # 看自动会判成哪档
```

## 记忆面板

```bash
python src/memory.py demo       # 灌演示数据看效果（正式用之前删掉 journal.jsonl 重来）
python src/render.py --open     # 生成面板并打开浏览器
python src/memory.py context "报告 进度"   # 看一次对话会注入什么上下文
```

`view/memory.html` 是自包含的，CSS/JS 全内联，不联网、不加载 CDN。

面板上每项左边那个竖直色条是记忆热度，也就是时间衰减后的残余强度。永久类永远是满的，日常记忆会慢慢变暗。低于阈值就不再进对话上下文，但不会被删除，你还能在面板上看到它。

## 记忆系统的设计

朴素做法是把对话历史全塞进 prompt。那样跑一周就会崩：prompt 无限膨胀、同一个事实被记 20 遍、模型在噪音里找不到重点。这里用了六个手段。

### 一、事实与事件分开

| | `PROFILE.md` | `journal.jsonl` |
|---|---|---|
| 内容 | 当前状态快照 | 历史时间线 |
| 写入 | 覆盖 / 合并去重 | 只追加 |
| 上限 | 有，会去重 | 无，靠压缩控制 |

「用户在准备考研」是事实，进 PROFILE；「9 月 14 日用户说下周三要交报告」是事件，进 journal。两者生命周期完全不同，混在一起必然膨胀。

### 二、每条记忆带三个元数据

- `importance` 1–5。生日和「今天吃了什么」不该一个待遇
- `emotion` 情绪标签，影响它回话的语气
- `decay` 决定遗忘速度：`permanent` / `slow` / `normal`

### 三、按分数检索，不全量注入

```
score = 关键词匹配 × 重要度 × 时间衰减 × 标签加成
```

时间衰减用 `exp(-λ·天数)`：

| 类别 | λ | 半衰期 | 用于 |
|---|---|---|---|
| `permanent` | 0 | 永不 | 生日、核心事实 |
| `slow` | 0.004 | ~173 天 | 长期目标、稳定偏好 |
| `normal` | 0.02 | ~35 天 | 日常事件 |

中文用字和相邻二字做特征，两千条以内比 embedding 更快更省。上万条再考虑向量检索。

### 四、Token 预算硬约束

记忆注入有上限，默认 1200 tokens，按分数截断。另外有近期兜底：最近 4 条无论分数高低都注入，保证短期连贯。

### 五、分层压缩

```
30 天内   → 原样保留
30 天以上 → 压成周摘要，原始条目进 archive/
180 天以上 → 再压成月摘要
```

老记忆是降采样，不是删除。原始数据永远在 `archive/` 里，随时能翻。

### 六、隐私过滤

`data/config.json` 里的 `blocked_patterns` 命中时，整句拒绝入库。默认拦密码、密钥、token、身份证、银行卡。

## 睡前整理

Python 和 LLM 分工明确。机制层由 Python 做：存、检索、评分、衰减、压缩、归档。语义层由 LLM 做：理解、抽取事实、合并去重、判断什么重要。

Python 做不了「这句话说明了用户什么偏好」这种判断，LLM 做不了「三年后这条记忆该不该还在」这种机械计算。

```bash
python src/consolidate.py brief     # Python 准备好素材
# agent 读完素材，直接改写 persona/PROFILE.md
python src/consolidate.py done      # 打时间戳，标记本轮整理完成
```

挂成定时任务就能每天睡前自动跑一次。合并优先于新增，因为 PROFILE 膨胀的唯一原因就是只加不减。

## 网络搜索

```bash
python src/tools.py search "查询词" -n 5
python src/tools.py search "今日新闻" --news
python src/tools.py fetch https://example.com
python src/tools.py cache --clear
```

| 后端 | 免费额度 | 要 key 吗 | 说明 |
|---|---|---|---|
| `ddgs`（默认） | 无限 | 不要 | 聚合 bing/brave/google/duckduckgo |
| `tavily` | 1000 次/月 | 不要 | 返回 LLM 就绪的摘要 |
| `searxng` | 自建无限 | — | 完全自主，需自己部署 |

在 `data/config.json` 的 `tools` 段切换后端、填 key、设代理。国内连不上 ddgs 就在 `tools.proxy` 里填 `http://127.0.0.1:7890`。

缓存是省额度最有效的手段，同一 query 在 TTL 内不重复请求。默认 general 1 小时 / news 15 分钟 / 网页 24 小时，都可在 config 里调。

## 本地知识库

默认关闭。它解决的是「模型不知道的私有资料」：关于你的事有 `PROFILE.md`，世界上的事有搜索，只有一批要反复查的私有资料才需要它。

```bash
# 1. 把资料丢进 knowledge/（.md .txt；PDF/Word/PPT 先转成 Markdown）
python src/knowledge.py build
python src/knowledge.py search "梯度下降的学习率怎么选"
# 2. 改 data/config.json → knowledge.enabled = true
```

用的同样是关键词检索，不是向量。两千块以内的资料，关键词比 embedding 更快、更准，还不用装依赖。

## 接到 agent 上

`src/mcp_server.py` 把上面这些暴露给 agent。挂上之后 agent 每轮可以调 `aipet_context(问题)`，一次拿到思考档位、关系状态、相关回忆和用户档案。

MCP 和信道是两件事：MCP 解决「它思考时读面板」，信道解决「它主动找你」。只想让面板生效，挂 MCP 就够了。

QQ 也是接入方式之一，见下面。

## 常用命令

```bash
python src/memory.py add "用户说下周三要交报告" -i 5 -t 工作,承诺 -e 压力
python src/memory.py search "报告"
python src/memory.py context "报告"        # 看注入了什么，附带 token 数
python src/memory.py stats
python src/memory.py compress              # 手动触发压缩
python src/consolidate.py brief|done|status
python src/render.py --open
python src/tools.py search "查询词" -n 5
python src/knowledge.py build|search|stats
python src/thinking.py list|set|auto|context|params
python src/pet.py                          # 需要 PySide6
```

参数：`-i` 重要度 1–5 · `-t` 标签逗号分隔 · `-e` 情绪 · `-d` 衰减类别

## 调参

改 `data/config.json`，保存即生效，不用重启：

- `retrieval.token_budget` 记忆注入上限。嫌它记不住事就调大，嫌贵就调小
- `scoring.decay_lambda` 遗忘速度。想让某些事记更久就调小
- `compression.daily_to_weekly_after_days` 多久开始压缩
- `privacy.blocked_patterns` 加你自己的隐私词

## QQ

自建的网关客户端，不依赖第三方框架。凭据放在 `data/secrets.json`，用 `tools/qq_ctl.py` 管理：

```bash
python tools/qq_ctl.py status     # 在跑没、连上没、自启装没装
python tools/qq_ctl.py start
python tools/qq_ctl.py stop
python tools/qq_ctl.py restart    # 改完代码重启
```

同一个 bot app 只能有一条网关连接。别处还连着的话要先停掉，两个同时跑事件会随机分配，表现为有时回有时不回。

## 目录结构

```
AIPet/
├── persona/                人格（不在仓库，自己建）
├── memory/                 记忆库（不在仓库）
├── view/                   记忆面板（程序生成）
├── knowledge/              本地资料（默认关闭）
├── assets/character.png    角色形象，可替换
├── themes/                 主题配色
├── hiyori_zh-Hans/         Live2D 模型
├── runtime/                便携版自带的运行环境（不在仓库）
├── docs/                   个性化设置总览、思考强度手册、QQ 接入、主题说明
├── 准备环境.bat             第一次用之前跑这个
├── 启动桌宠.bat / 启动诊断.bat
├── 启动QQ.bat / 停止QQ.bat
│
├── src/
│   ├── memory.py           记忆引擎：写入 / 检索 / 评分 / 压缩
│   ├── brain.py            独立大脑：直连 DeepSeek
│   ├── pet.py              桌面窗口 + 思考面板 + 对话窗口
│   ├── companion.py        对话接续、约定、陪伴
│   ├── mood.py             心理点
│   ├── qq_bot.py           QQ 网关客户端
│   ├── qq_bridge.py        QQ 事件 → 身份 → 回复
│   ├── people.py           群成员档案
│   ├── thinking.py         思考强度引擎
│   ├── live2d_widget.py    Live2D 渲染组件
│   ├── local_tools.py      它能调用的本地工具
│   ├── mcp_server.py       MCP server
│   └── ...
│
└── data/
    └── thinking.json       五档预设 + 自动判定规则
```
