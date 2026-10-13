<picture>
  <source media="(max-width: 640px)" srcset="docs/assets/readme-cover-mobile.png">
  <img src="docs/assets/readme-cover.png" alt="AIPet 小日和，桌面上的 Live2D 伙伴" width="1200">
</picture>

# AIPet · 小日和

一个住在桌面上的 AI 角色：陪你聊天、记住话题，也能按需要看图片、读文件和提醒你。

**0.6.0-beta.3** · Windows 10/11 x64 · 推荐 Python 3.12

[下载最新版](https://github.com/xingyichen069-ctrl/AIPet/archive/refs/heads/main.zip) · [使用指南](docs/使用指南.md) · [更新日志](CHANGELOG.md) · [开发者文档](docs/developer_开发者/README.md)

## 开始使用

先安装 [Python 3.12 Windows 64 位版](https://www.python.org/downloads/windows/)，安装时勾选 **Add python.exe to PATH**。已有支持的 Python 或 uv 可以跳过。

1. **下载并完整解压**，例如放在 `D:\AIPet`。
2. 双击 **准备环境.bat**，等依赖安装完成。首次需要联网下载。
3. 双击 **配置API.bat**，在「模型与接口」填写地址和 API key，获取或验证模型 ID 后保存。
4. 双击 **启动桌宠.bat**，发一句“你好”。

日常只需启动桌宠。换接口、模型或外观，从桌宠右键菜单进入 **配置**；角色性格在独立的 **人格管理** 中调整。

## 怎么使用

- **聊天**：双击角色身体打开对话窗；右键可找到配置和退出入口。
- **个性化**：切换人格、六套昼夜主题、字号和通透度。
- **按需开启**：图片识别、文件读写、提醒和 QQ。具体用法见[使用指南](docs/使用指南.md)。

新装默认使用「极限」思考档位，普通聊天可切换到「日常」或「自动」。

聊天记录和记忆保存在本机；对话及选中的回忆会发送到你配置的模型服务，API 费用由服务商收取。

## 更新旧版

把新版解压到**独立新目录**，保留旧目录，再按[更新步骤](docs/使用指南.md#更新)迁移自己的配置与记忆。

## 需要帮助

[常见问题](docs/使用指南.md#常见问题) · [QQ 指令帮助](docs/QQ使用帮助.md) · [开发与维护](docs/developer_开发者/README.md)

macOS 目前为实验性，安装方法见[开发者说明](docs/developer_开发者/README.md#macos实验性)。当前版本仍为 beta。

<sub>封面角色为 Live2D 示例模型「桃濑日和」；插画 Kani Biimu，模型 Live2D。<a href="hiyori_zh-Hans/hiyori_pro/ReadMe.txt">模型说明与使用许可</a>。</sub>
