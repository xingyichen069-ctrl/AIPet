"""Shared character palettes for chat windows and desktop menus."""
from datetime import datetime

THEME_NAMES = {
    'touhou': '博丽灵梦 · 朱色结界',   # 键名不动：它存在 appearance.json 里，改了旧设置会读不出来
    'marisa': '雾雨魔理沙 · 星屑魔法',
    'koishi': '古明地恋 · 无意识之庭',
    'cirno': '琪露诺 · 冰晶雾湖',
    'satori': '古明地觉 · 觉之瞳',
    'flandre': '芙兰朵露 · 彩翼夜宴',
}
TOUHOU_DAY = {
    'top': '#fffaf1', 'bottom': '#f5eddf',
    'vermilion': '#a9403b', 'gold': '#c39b65',
}
TOUHOU_NIGHT = {
    'top': '#191e2a', 'bottom': '#121723',
    'vermilion': '#c86760', 'gold': '#b99969',
}
# Keep the original four keys compatible with custom appearance files.
PALETTES = {
    'touhou': (TOUHOU_DAY, TOUHOU_NIGHT),
    'marisa': (
        dict(top='#fffdf4', bottom='#f3ecd4', vermilion='#b18a23', gold='#d6b548'),
        dict(top='#25252a', bottom='#17171c', vermilion='#efd16b', gold='#c4a94f')),
    'koishi': (
        dict(top='#f8fbe9', bottom='#e4f0df', vermilion='#3b785f', gold='#c8ac38'),
        dict(top='#192e34', bottom='#12232c', vermilion='#84c7a0', gold='#e3ca69')),
    # 琪露诺 —— 湖上的冰精。取自官方设定的两个锚点：
    #   蓝色无袖连衣裙（主色）+ 背后三对六棱柱翅膀（所以 motif 是六角雪花）。
    #   vermilion/gold 这两个键名是从 touhou 继承的，实际含义是「主色 / 点缀」，
    #   别被名字骗了 —— 这里的 vermilion 是冰蓝，不是朱红。
    'cirno': (
        dict(top='#f6fcff', bottom='#dff1fb', vermilion='#3f9fcf', gold='#8ad4ef'),
        dict(top='#0f1c2b', bottom='#0a1420', vermilion='#7ecfee', gold='#a9e3f8')),
    'satori': (
        dict(top='#fff7f4', bottom='#f2e4ec', vermilion='#a83f55', gold='#d49a9d'),
        dict(top='#271c2b', bottom='#1b1724', vermilion='#ef7b83', gold='#d2a1ad')),
    'flandre': (
        dict(top='#fff5f2', bottom='#f3dfe0', vermilion='#b83e4b', gold='#d6a35b'),
        dict(top='#2a1822', bottom='#1c1420', vermilion='#f06a72', gold='#e0b05f')),
}
# Readable foregrounds and tinted control surfaces, including night variants.
ROLES = {
    'touhou': (
        ('#423936', '#806d63', '#decbb7', '#f2dfd3', '#8c3933', '#a9403b', '#fffaf1', '#ad403b', '#fffaf0', '#fffdf8', '#f6e5df'),
        ('#eee5dc', '#b5a69c', '#59474b', '#48343d', '#f5d7c7', '#c86760', '#222633', '#a64140', '#fffaf0', '#222937', '#3b2d35')),
    'marisa': (
        ('#29282d', '#766b4d', '#d6c58b', '#f5e6a5', '#29282d', '#8a691b', '#fffdf4', '#29282d', '#ffe58b', '#ffffff', '#faf0c5'),
        ('#f7f3e6', '#c2b992', '#625a3d', '#51472a', '#fff1b8', '#efd16b', '#25252a', '#efd16b', '#222127', '#2d2d33', '#3c3626')),
    'koishi': (
        ('#23494c', '#5a7470', '#b4cbb3', '#e6eec0', '#244e64', '#326c85', '#f8fbe9', '#326c85', '#fff7ca', '#f6fcf4', '#e5f1d5'),
        ('#e6f2e8', '#adc6b5', '#476b65', '#34554d', '#fff0a4', '#91cbe0', '#192e34', '#e3ca69', '#203f4b', '#213c41', '#2e4940')),
    # 日间偏「雾之湖的白昼」：白衬衫的白 + 冰和水那种很浅的蓝。
    # 夜间是「湖面结冰的深夜」：整片深蓝，主色提亮成发光的冰。
    # button_text 夜间用深色（#08202e）—— 按钮本身是亮冰蓝，上面压白字看不清。
    'cirno': (
        #                       button ↓ 原本是 #3f9fcf（和 blue 同色）。那个太浅，
        #   白字压上去只有 2.97:1，是四套主题里唯一过不了 4.5:1 的。
        #   换成同一套里的深蓝，白字 5.5:1。
        ('#203a4c', '#5f7d90', '#b9d9e9', '#ddf0fa', '#2b6f97', '#3f9fcf', '#f6fcff', '#2b6f97', '#ffffff', '#f9fdff', '#e3f3fb'),
        ('#dcebf6', '#8ea9bd', '#2f4b60', '#365a71', '#c3e9fb', '#7ecfee', '#0f1c2b', '#7ecfee', '#08202e', '#111f2f', '#1b3549')),
    'satori': (
        ('#4e3038', '#82656b', '#e0c5c7', '#f5dfe0', '#8b3349', '#b64a5d', '#fff8f5', '#ad4052', '#fff8f5', '#fffdfb', '#f6e3e3'),
        ('#f4e5e5', '#c1a4aa', '#5e3e4b', '#51313e', '#ffd8d8', '#ef7b83', '#271c2b', '#d25b67', '#2b1520', '#2b202e', '#4b2935')),
    'flandre': (
        ('#552c32', '#8a6763', '#e4c0b5', '#f6d8cc', '#983743', '#b83e4b', '#fff7f3', '#ad3d48', '#fff7f2', '#fffdfb', '#f7e2dc'),
        ('#f6e4de', '#c5a6a0', '#65414a', '#58303b', '#ffd1c4', '#f06a72', '#2a1822', '#d95762', '#2a1219', '#30212b', '#4c2730')),
}
ROLE_KEYS = ('text', 'muted', 'border', 'hover', 'selected', 'blue', 'surface', 'button', 'button_text', 'bubble', 'user')


def touhou_palette(day):
    return dict(TOUHOU_DAY if day else TOUHOU_NIGHT)


def character_palette(theme, day):
    return dict(PALETTES.get(theme, PALETTES['touhou'])[0 if day else 1])


def theme_roles(theme, day):
    return dict(zip(ROLE_KEYS, ROLES.get(theme, ROLES['touhou'])[0 if day else 1]))


def is_daytime(now: datetime | None = None) -> bool:
    """Use the existing 07:00–18:00 daytime interval."""
    return 7 <= (now or datetime.now()).hour < 18
