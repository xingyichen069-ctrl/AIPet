# QQ 输入框指令菜单

群聊中的指令面板由 QQ 客户端显示。当前客户端在实际选中 @机器人时也会弹出，不必一定先输入 `/`；触发时机和面板样式由 QQ 控制，接口没有“仅输入 `/` 时弹出”的设置。点击候选项后填入输入框，发送后才由 AIPet 处理。

面板通过 QQ 官方 `/v2/panels` 接口登记，配置保存在 QQ 平台，本机关闭后仍可使用；无需在开发者网页寻找设置入口。当前配置为 `/help` 加 5 个功能入口，避免将同一功能的多个操作重复铺在面板上。仅更新菜单不需要重启网关，新增命令处理逻辑则需要部署后端。

## 菜单内容

| 主入口 | 用途与可追加参数 |
| --- | --- |
| `/help` | 发送 Markdown 指令手册文件，所有人可用；兼容 `/帮助` |
| `/符卡` | 默认查看本群状态；可追加 `状态`、`预览`、`开启`、`关闭` |
| `/定时任务` | 默认查看本群符卡状态；追加 `列表` 查看模板，`开启 任务名`、`关闭 任务名`、`状态 任务名`、`预览 任务名` 管理指定模板 |
| `/档位` | 默认查看档位与选项；可追加 `自动`、`省电`、`日常`、`认真`、`深究`、`极限`、`雷霆`，或 `跟随` 恢复跟随默认档位 |
| `/tavily` | 查看联网检索用量和额度，不执行搜索 |
| `/版本` | 查看版本并检查更新，不安装更新 |

`/help` 直接发送现成的 [QQ 使用帮助](QQ使用帮助.md)，聊天里只保留一个文件卡片，不再贴整页正文。文件上传与发送不调用模型或搜索；同一会话、同一版本的上传凭据在有效期内复用。QQ 客户端若不能预览 `.md`，下载后用 Markdown 阅读器打开。其余管理、用量和版本命令仍仅限已经绑定的主人执行。菜单本身允许所有群成员点击，避免把“QQ 群管理员”误当作 AIPet 的主人；群友尝试执行管理命令仍会被后端拒绝。登记菜单不会开启符卡推送，开关仍由主人在目标群决定。主人认领口令不列入菜单。

帮助源文件统一维护在 `docs/QQ使用帮助.md`。通过 QQ 官方分片接口上传为文件类型，再使用当前消息编号被动回复，不依赖公网文档网站或桌面电脑。上传失败只回一句简短提示；发送结果未知时不自动重发。普通聊天的链接和 Markdown 过滤不因此放宽。档位列表按当前运行端的实际预设生成，未配置的档位会明确拒绝切换。

命令由程序直接处理：说明使用现成文本，状态和用量查询读取实际数据。带 `/` 的指令输错时直接提示用法，不交给模型猜测；普通聊天问题仍正常处理。命令附带的图片或文件不自动读取，命令及其回复也不加入后续模型聊天上下文，避免再次消耗 token。Tavily 用量和版本查询仍可能产生必要的接口流量，但不调用语言模型。

官方群指令面板只支持一层条目，没有子菜单或未发送状态下的点击回调。选择 `/档位` 后，手动追加参数即可，例如 `/档位 日常`；不会再弹出第二层选择器。带折叠子菜单的 `/v2/menu` 仅适用于单聊窗口底部，不适用于群面板。

符卡时间、开关和模板扩展见 [QQ 定时任务](QQ定时任务.md)。

## 更新菜单

配置文件是 [`templates/qq_group_panel.json`](../templates/qq_group_panel.json)，登记工具是 [`tools/qq_panel.py`](../tools/qq_panel.py)。在项目目录运行：

```console
python tools/qq_panel.py preview
python tools/qq_panel.py list
python tools/qq_panel.py apply
```

`preview` 只检查和显示本地配置；`list` 查询群面板；`apply` 使用当前安装的 QQ 凭据登记并回读，既不会启动第二个网关，也不会发送群消息。配置应用于该机器人全部群聊，不修改单聊、频道或其他机器人的菜单。

以后添加新命令，先实现其处理逻辑与身份检查，再更新配置并运行 `apply`。菜单不会自动生成后端功能，也不会给参数自动生成第二级菜单。模板保留熟悉的 `/` 写法；QQ 当前接口回读会去掉命令名的前导 `/` 并省略值为 false 的权限字段，工具会按相同含义核对。

工具用固定备注识别本项目面板，只修改它；遇到其他全局群面板或多个同标记面板时停止，保留已有内容。写入前的配置与操作状态保存在私人 `data/qq_panel_admin/` 中，不提交到 Git。若创建超时且尚未查到结果，会阻止重复创建；先用 `list` 核对平台。已经取得面板编号时会按编号回读，不能仅因列表暂未显示就再创建一份。

## 核对效果

先在 QQ 中实际选中 @机器人，查看候选菜单；也可继续输入半角 `/`。如果仍显示旧条目，重新进入群聊后再检查；接口回读成功与客户端显示是两个不同的验证步骤。需要确认指令送达时，可以由主人自行发送 `/符卡 状态`，它不会开启推送。

官方依据：[自定义菜单与指令面板](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/)、[创建指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.post.html)、[修改指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.put.html)、[仅适用于单聊的自定义菜单](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_menu.put.html)。

文件接口：[群聊预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)、[分片完成](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_part_finish.post.html)、[文件合并](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)、[发送群消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)。
