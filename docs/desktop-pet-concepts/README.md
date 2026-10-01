# 七位小住客 · 视觉样例 01

2026-10-01。七位默认角色，每位一张基础稿和一张活动稿，共 14 张。当前为 **待选静态原画**，还没有拆层、绑定或接入 AIPet。

![七人总览](review/seven-personas-overview.png)

## 先看哪张

| 角色 | 活动姿态 | 两张对照 |
| --- | --- | --- |
| 小日和 · 桃濑日和 | 坐着抱杯 | [查看](review/hiyori-comparison.png) |
| 博丽灵梦 | 侧坐喝茶 | [查看](review/reimu-comparison.png) |
| 雾雨魔理沙 | 扫帚上招手 | [查看](review/marisa-comparison.png) |
| 琪露诺 | 托雪花小跳 | [查看](review/cirno-comparison.png) |
| 古明地觉 | 坐着抱书阅读 | [查看](review/satori-comparison.png) |
| 古明地恋 | 扶帽看小花 | [查看](review/koishi-comparison.png) |
| 芙兰朵露 | 坐着搭积木 | [查看](review/flandre-comparison.png) |

## 互动预览怎么开

在本分支下载并完整解压后，双击本目录的 **打开预览.bat**，或用浏览器打开 [review/index.html](review/index.html)。这是离线静态页面，不需要 Python、网络或 API 密钥。

页面可切换基础 / 活动姿态，调节 120–260 像素高度、切换深浅背景、点击角色放大。GitHub 的 HTML 文件页只展示源码；请下载后在浏览器里打开。

也可以直接查看 [120 / 180 / 260 像素对照](review/size-comparison.png)。图片查看器处于 100% 缩放时，才对应图内标注的实际像素。

## 文件对应关系

| 文件或目录 | 内容 |
| --- | --- |
| [originals](originals/) | 14 张接口返回的原始透明 PNG，未改写原图 |
| [prompts](prompts/) | 每张图对应的完整提示词 |
| [characters.json](characters.json) | 角色辨识点、活动意图和配色 |
| [image-inventory.json](image-inventory.json) | 相对路径、实际尺寸、请求参数与 SHA256 |
| [visual-review.json](visual-review.json) | 逐图视觉检查记录 |
| [review](review/) | 原画排版、尺寸对照与离线预览 |
| [DESIGN.md](DESIGN.md) | 人格 / 皮套分离、小体型与动态模式纲领 |
| [制作记录](review/制作记录.md) | 生成方式、参考来源及当前限制 |

清单中的 `selected` 仅表示选入本轮预览，不代表角色已经由用户最终定稿。

## 制作与出处

通过 imagegen 技能自带的 API/CLI 流程调用经授权的图像接口，请求模型名 `gpt-image-2`。先确立小日和的画风，再用它作为其他角色的比例与画法参考；每张活动图均从对应角色自己的基础图编辑。当前没有对服务上游实现作独立确认。

- 桃濑日和身份来自仓库中的 Live2D 示例模型，原插画 Kani Biimu，模型 Live2D。参见 [模型说明与素材使用许可](../../hiyori_zh-Hans/hiyori_pro/ReadMe.txt)。
- 手部、眼睛、头发与道具互动参考 [大肥鱼！丨免费 Live2d 模型](https://www.bilibili.com/video/BV16yYi69EQT/)，发布者氵六青，画面标注模型制作茶坤不接了。
- 紧凑小全身与姿态变化参考 [【全动态Live2D】大肥鱼桌宠！？](https://www.bilibili.com/video/BV1E6aq6pEpz/)，作者 PalAILab。
- 东方角色的身份和气质结合仓库跟踪的 [persona_defaults](../../persona_defaults/)。这些图片是同人桌宠视觉探索，不是官方立绘。

原视频截图只作本机参考，本分支保留来源链接，不打包视频截图、模型下载权重、运行环境或接口凭据。角色和参考素材的原有使用条件仍适用，本次整理不替原权利方授予新的授权。

## 下一步

先选择喜欢的比例、面容和服装，再统一细节、清理透明边缘、拆层补画、绑定 Live2D 参数。整组眼型还有相似之处；觉和恋的线管、芙兰的棱晶翼为小尺寸做了简化，可在定稿时继续调整。

两张姿态图用于讨论外形和动作意图，不是可以直接插值的动画帧。四种动态模式和皮套切换设置仍属于 [后续设计](DESIGN.md)。
