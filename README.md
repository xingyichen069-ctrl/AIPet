# AIPet

一个住在桌面上的角色。她记得你说过的话，会主动找你，也能在 QQ 上等你回消息。

完全本地运行，不依赖任何外部服务常驻。她需要一个「大脑」—— 任何 OpenAI 兼容的对话 API 都行，默认接 DeepSeek。

当前版本 `0.4.0-beta.7` ｜ [更新日志](CHANGELOG.md) ｜ [文档](#文档)

---

## 她的特质

**住在桌面上。** Live2D 形象，实时渲染。

**记得住事。** 事实和事件分开存，每条带重要度、情绪和遗忘速度，按分数检索、按 token 预算注入。半年后她还记得你三月说过什么，但不会把上周的闲聊也算进来。→ [记忆系统](docs/记忆系统.md)

**有思考强度档位。** 五档，「省电」到「极限」，改变记忆检索深度（350 → 9000 tokens）、推理投入、是否联网、是否自检。默认「自动」。→ [思考强度配置手册](docs/思考强度配置手册.md)

**能动手。** 查时间、看电量、联网搜索、读写你指定的文件夹、看图片。文件读写限制在一个沙箱根里 —— **你想让她管哪儿，就把沙箱根设成哪儿**。→ [工具与沙箱](docs/工具与沙箱.md)

**能接 QQ。** 自建的网关客户端，不依赖第三方框架。能认出群成员，记得住群友说过的话，同时不让别人的事混进关于你的记忆。→ [QQ 接入](docs/QQ机器人接入提示词.md)

**换肤。** 四套主题，日夜不同。

## 跑起来

Windows：

1. 双击 **准备环境.bat**。首次会装依赖，下载约 300 MB
2. 双击 **启动桌宠.bat**

macOS 直接双击 `启动桌宠.command`。

前置只有一样：64 位 Python 3.10–3.14，或者 uv。两个都没装的话，`准备环境.bat` 会告诉你装哪个。

首次运行会自动生成 `data/config.json`（带注释的完整配置）以及 `persona/` `memory/` `view/` 这些运行目录。

**这时她还不会说话** —— 需要配一个 API key，见下面。

## 关于人格

仓库里**不含人格文件**。`persona/SOUL.md`、`BOUNDARIES.md`、`PROFILE.md` 要你自己写。

这是有意的：人格是私人的东西，别人的 SOUL.md 对你没有意义。仓库提供的是位置和读取它的代码。

直接编辑 `persona/SOUL.md`，中文写，保存即生效，不用重启。

## 配置

### 大脑

`src/brain.py` 直连 API，中间不隔任何框架 —— 所以思考强度面板上的参数是真正生效的。

只要对方是 **OpenAI 兼容的 `/chat/completions`** 就能当脑子用：

```bash
python src/brain.py setkey sk-xxxx    # 配置 key，会立刻验证
python src/brain.py check             # 验证连通性
python src/brain.py chat              # 命令行对话
```

默认连 DeepSeek 官方。换别家改 `data/secrets.json`：

```json
{
  "deepseek_api_key": "你的 key",
  "deepseek_base_url": "https://你的服务/v1"
}
```

（键名里的 `deepseek` 是历史包袱 —— 改掉会让老配置读不出来，所以留着。）模型名在 `data/thinking.json` 的档位里改。

> 文档里提到的两个限制（思考模式下 `temperature` 无效、思考强度实际只有三档）**是 DeepSeek 的行为**，换别家不一定成立。

### 她能碰哪些文件

沙箱根默认是用户主目录下的 `AIPet` 文件夹。想改，编辑 `data/config.json`：

```json
{ "tools": { "fs_root": "D:\\我的资料" } }
```

留空就是默认值。设成哪里，她就能读写到哪里 —— 这是最该按自己习惯设的一项。→ [工具与沙箱](docs/工具与沙箱.md)

### 看图

需要另外配一个带视觉的模型。同样支持任何 OpenAI 兼容接口：本机跑的（Ollama、vLLM、LM Studio）、云上的、内网部署的都能接。→ [工具与沙箱](docs/工具与沙箱.md)

### 其它

搜索后端、代理、隐私词、记忆参数、Live2D 尺寸 —— 都在 `data/config.json` 里，**文件本身带注释**，改完保存即生效，不用重启。总览见 [个性化设置总览](docs/个性化设置总览.md)。

## 文档

| 文档 | 讲什么 |
|---|---|
| [个性化设置总览](docs/个性化设置总览.md) | 你能改的东西，按重要性排 |
| [记忆系统](docs/记忆系统.md) | 她怎么记事、怎么忘事 |
| [工具与沙箱](docs/工具与沙箱.md) | 她能做什么、边界在哪、开哪些开关 |
| [思考强度配置手册](docs/思考强度配置手册.md) | 五档参数与自动判定规则 |
| [主题使用说明](docs/主题使用说明.md) | 四套主题、字号、玻璃质感 |
| [QQ 接入](docs/QQ机器人接入提示词.md) | 自建网关、群身份识别 |
| [命令行参考](docs/命令行参考.md) | 所有脚本的用法与自检 |
| [新增功能说明](新增功能说明.md) | 对话接续、约定提醒、安静陪伴 |
| [Windows 使用说明](Windows使用说明.md) | Windows 上的差异 |
| [版本管理](docs/版本管理.md) | 版本号、标签、回退 |

## 更新

当前版本号在根目录 `VERSION` —— 程序读的是它，`CHANGELOG.md` 和这份 README 里的只是给人看的。

查有没有新版本：桌宠右键 → 高级 → 检查更新；或者在 QQ 上给主人发 `/版本`。

**它只查，不下载也不覆盖任何文件。** 有新版会给出下载页，装不装是你的事。不做自动更新是有意的 —— 覆盖安装一旦中途失败，整个装好的环境就坏了，而更新这件事并不值得冒这个风险。

把新版 zip 解压覆盖到旧目录**不会丢任何东西**：仓库的 zip 根本不含 `persona/` `memory/` `data/`，解压也不会删掉压缩包里没有的文件。

如果你习惯解压到新目录再切换，用迁移脚本把人格、记忆、密钥和配置搬过去：

```bash
python tools/migrate.py <旧目录> --dry-run   # 先看清单，不动
python tools/migrate.py <旧目录>             # 看清单并确认
```

或者把旧目录拖到 `迁移私人内容.bat` 上。

## 目录结构

```
AIPet/
├── src/                    全部代码
│   ├── pet.py              桌面窗口 + 思考面板 + 对话窗
│   ├── brain.py            大脑：直连 API，工具循环
│   ├── memory.py           记忆引擎：写入 / 检索 / 评分 / 压缩
│   ├── thinking.py         思考强度引擎
│   ├── local_tools.py      她能调用的工具（含沙箱）
│   ├── vision.py           看图（接任意视觉接口）
│   ├── mood.py             心理点
│   ├── companion.py        对话接续、约定、安静陪伴
│   ├── qq_bot.py           QQ 网关客户端
│   ├── qq_bridge.py        QQ 事件 → 身份 → 回复
│   ├── people.py           群成员档案
│   ├── knowledge.py        本地知识库
│   ├── tools.py            联网搜索
│   ├── update.py           检查更新
│   ├── mcp_server.py       MCP server
│   └── ...
├── persona/                人格（不在仓库，自己建）
├── memory/                 记忆库（不在仓库）
├── data/
│   ├── config.example.json 配置模板，首次运行会复制成 config.json
│   ├── thinking.json       五档预设
│   └── secrets.json        API key（不在仓库）
├── view/                   记忆面板（程序生成）
├── knowledge/              本地资料（默认关闭）
├── assets/                 角色形象，可替换
├── themes/                 主题配色
├── hiyori_zh-Hans/         Live2D 模型
├── docs/                   文档
├── tests/                  界面测试
├── tools/                  qq_ctl / migrate 等辅助脚本
├── 准备环境.bat             第一次用之前跑这个
├── 启动桌宠.bat / 启动诊断.bat
├── 启动QQ.bat / 停止QQ.bat
├── 查看状态.bat             看桌宠和 QQ 桥活着没
├── 停止桌宠.bat             退不掉的时候从外面停
└── 迁移私人内容.bat          换新装时搬私人内容
```

## 依赖

Python 包（`requirements.txt`）：

| 包 | 用途 |
|---|---|
| PySide6 | 界面、窗口、WebSocket |
| live2d-py | 桌面形象 |
| ddgs | 联网搜索 |
| PySocks | SOCKS 代理 |

都是纯 Python 或带预编译轮子，覆盖 CPython 3.10–3.14，不需要现场编译。

## 状态

还在 beta。Windows 上的实机启动、原生托盘显示尚未完整验证。

`tests/` 里的用例必须走 `unittest discover` 才会跑（单独执行文件不会报错，但什么都不执行）：

```bash
python -m unittest discover -s tests        # 67 项
```

`tests/test_desktop.py` 里两个滚动条测试在 offscreen 平台下会随机失败，与改动无关。
