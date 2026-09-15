"""Shared character palettes for chat windows and desktop menus."""
from datetime import datetime

THEME_NAMES = {
    'touhou': '东方 · 朱色结界',
    'marisa': '雾雨魔理沙 · 星屑魔法',
    'koishi': '古明地恋 · 无意识之庭',
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
