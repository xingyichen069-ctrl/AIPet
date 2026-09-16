"""Character ornaments and shared, keyboard-accessible desktop menus."""
import math
from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QColor, QPen, QActionGroup
from PySide6.QtWidgets import QMenu, QWidget, QWidgetAction, QMessageBox
from ui_theme import THEME_NAMES, is_daytime


def draw_motif(p, theme, rect, colours, roles):
    """Draw a vector motif in a normalized 52-pixel square."""
    p.save()
    p.translate(rect.x(), rect.y())
    p.scale(rect.width() / 52, rect.height() / 52)
    p.setPen(QPen(QColor(colours['gold']), 1.2))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(4, 4, 44, 44))
    if theme == 'marisa':
        p.setBrush(QColor('#25252a'))
        p.drawEllipse(QRectF(8, 8, 36, 36))
        star = QPainterPath()
        for i in range(10):
            a, r = i * math.pi / 5 - math.pi / 2, 17 if i % 2 == 0 else 7.5
            point = QPointF(26 + math.cos(a) * r, 26 + math.sin(a) * r)
            star.moveTo(point) if i == 0 else star.lineTo(point)
        star.closeSubpath()
        p.setBrush(QColor(colours['gold']))
        p.drawPath(star)
    elif theme == 'koishi':
        p.setPen(QPen(QColor(roles['blue']), 1.6))
        cord = QPainterPath(QPointF(26, 41))
        cord.cubicTo(55, 50, 51, 8, 37, 13)
        cord.cubicTo(33, 15, 45, 22, 48, 18)
        p.drawPath(cord)
        heart = QPainterPath(QPointF(26, 40))
        heart.cubicTo(21, 34, 9, 26, 11, 19)
        heart.cubicTo(13, 9, 23, 11, 26, 18)
        heart.cubicTo(31, 9, 41, 12, 41, 21)
        heart.cubicTo(41, 28, 32, 36, 26, 40)
        p.setBrush(QColor(colours['gold']))
        p.drawPath(heart)
        p.setPen(QPen(QColor(colours['vermilion']), 2))
        lid = QPainterPath(QPointF(18, 24))
        lid.quadTo(26, 30, 34, 24)
        p.drawPath(lid)
    elif theme == 'cirno':
        # 六角雪花。不是随便挑的 —— 官方设定写她背后长着「三對六棱柱狀翅膀」，
        # 六棱柱就是六角，所以六角雪花的来历是有据的。
        #
        # ★ 按尺寸分两档画。踩过：主枝加侧枝一共 18 条线，在 52px 的徽章上
        #   很好看，到了 16px 的分隔线上就糊成一团 —— 同尺寸下另外三个主题
        #   （太极/星星/心）都还认得出，只有这个成了蓝疙瘩。
        #   小尺寸砍掉一半侧枝、把线加粗，形状反而更清楚。
        small = rect.width() < 24
        segs = ((0.66, 9.0),) if small else ((0.52, 7.0), (0.80, 5.0))
        p.setPen(QPen(QColor(colours['vermilion']),
                      2.8 if small else 2.0, Qt.SolidLine, Qt.RoundCap))
        for i in range(6):
            a = i * math.pi / 3 - math.pi / 2
            p.drawLine(QPointF(26, 26),
                       QPointF(26 + math.cos(a) * 17, 26 + math.sin(a) * 17))
            for frac, seg in segs:
                bx = 26 + math.cos(a) * 17 * frac
                by = 26 + math.sin(a) * 17 * frac
                for s in (-1, 1):
                    sa = a + s * math.pi / 4
                    p.drawLine(QPointF(bx, by),
                               QPointF(bx + math.cos(sa) * seg,
                                       by + math.sin(sa) * seg))
        # 小尺寸不点花心：那一点在 16px 里只会把六条线糊在一起
        if not small:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(colours['gold']))
            p.drawEllipse(QPointF(26, 26), 3.4, 3.4)
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(colours['vermilion']))
        p.drawEllipse(QRectF(8, 8, 36, 36))
        light = QPainterPath(QPointF(26, 8))
        light.arcTo(QRectF(8, 8, 36, 36), 90, 180)
        light.cubicTo(14, 44, 14, 26, 26, 26)
        light.cubicTo(38, 26, 38, 8, 26, 8)
        p.setBrush(QColor(colours['top']))
        p.drawPath(light)
        p.setBrush(QColor(colours['vermilion']))
        p.drawEllipse(QPointF(26, 17), 2.7, 2.7)
        p.setBrush(QColor(colours['top']))
        p.drawEllipse(QPointF(26, 35), 2.7, 2.7)
    p.restore()


class MenuHeading(QWidget):
    def __init__(self, appearance, parent):
        super().__init__(parent)
        self.appearance = appearance
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.refresh()

    def refresh(self):
        theme = self.appearance.settings.get('theme', 'touhou')
        font = self.font()
        font.setPixelSize(self.appearance.settings.get('font_size', 13))
        font.setBold(True)
        self.setFont(font)
        self.setFixedHeight(70)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(THEME_NAMES[theme]) + 82)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        day = is_daytime()
        colours, roles = self.appearance.palette(day), self.appearance.roles(day)
        theme = self.appearance.settings.get('theme', 'touhou')
        draw_motif(p, theme, QRectF(9, 10, 43, 43), colours, roles)
        p.setPen(QColor(roles['text']))
        p.drawText(QRectF(64, 9, self.width() - 72, 25), Qt.AlignVCenter, THEME_NAMES[theme])
        font = p.font()
        font.setPixelSize(11)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QColor(roles['muted']))
        p.drawText(QRectF(64, 35, self.width() - 72, 19), Qt.AlignVCenter, '小日和  /  日间' if day else '小日和  /  夜间')
        p.setPen(QPen(QColor(roles['border']), 1))
        p.drawLine(10, 64, self.width() - 10, 64)


class ThemeMenu(QMenu):
    def __init__(self, appearance, parent=None, title='', heading=False):
        super().__init__(title, parent)
        self.appearance = appearance
        self.heading = None
        if heading:
            action = QWidgetAction(self)
            self.heading = MenuHeading(appearance, self)
            action.setDefaultWidget(self.heading)
            self.addAction(action)
        self.aboutToShow.connect(self.refresh_theme)
        appearance.changed.connect(self.refresh_theme)
        self._theme_timer = QTimer(self)
        self._theme_timer.setInterval(60_000)
        self._theme_timer.timeout.connect(self.refresh_theme)
        self.aboutToShow.connect(self._theme_timer.start)
        self.aboutToHide.connect(self._theme_timer.stop)
        self.refresh_theme()

    def refresh_theme(self):
        self.setStyleSheet(self.appearance.menu_stylesheet(is_daytime()))
        if self.heading:
            self.heading.refresh()
        for action in self.actions():
            key = action.property('appearanceKey')
            if key:
                action.setChecked(self.appearance.settings.get(key) == action.data())

    def addMenu(self, title):
        menu = ThemeMenu(self.appearance, self, title)
        super().addMenu(menu)
        return menu


def add_appearance_menu(menu, appearance, choose=None):
    def save(key, value):
        try:
            appearance.choose(key, value)
        except OSError:
            QMessageBox.information(menu.parentWidget(), '外观', '外观设置暂时无法保存，请稍后再试。')
    choose = choose or save
    target = menu.addMenu('外观')
    for title, key, choices in (
        ('主题', 'theme', [(label, key) for key, label in THEME_NAMES.items()]),
        ('字号', 'font_size', [('标准', 13), ('大一点', 15), ('更大', 17)]),
        ('玻璃质感', 'glass_opacity', [('清晰', .85), ('柔和', .65), ('通透', .4)]),
    ):
        submenu = target.addMenu(title)
        group = QActionGroup(submenu)
        group.setExclusive(True)
        for label, value in choices:
            action = submenu.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.setProperty('appearanceKey', key)
            action.setChecked(appearance.settings.get(key) == value)
            group.addAction(action)
            action.triggered.connect(lambda _=False, k=key, v=value: choose(k, v))
    return target
