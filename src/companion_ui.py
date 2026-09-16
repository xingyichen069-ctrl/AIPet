"""Small desktop controls for companion services."""
from __future__ import annotations

import math
import sys
import time
from PySide6.QtCore import Qt, QTimer, Signal, QUrl, QRectF, QPointF, QByteArray, Slot
from PySide6.QtGui import (QDesktopServices, QKeySequence, QShortcut, QTextCursor,
    QPainter, QColor, QPen, QPainterPath, QLinearGradient, QPalette)
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QScrollArea, QMenu, QFileDialog, QInputDialog, QDialog, QListWidget,
    QMessageBox, QApplication)

import companion as C
import memory as M
from ui_theme import is_daytime, touhou_palette, theme_roles
from theme_widgets import ThemeMenu, add_appearance_menu, draw_motif
from native_glass import NativeGlass
from desktop_state import DesktopState, Appearance

STYLE = '''
QWidget { color:#423936; font-size:13px; font-weight:400; }
QDialog { background:#fbf7ef; }
QLabel#chatTitle { color:#993934; font-size:19px; font-weight:600; background:transparent; }
QLabel#chatSubtitle { color:#8a6156; font-size:11px; background:transparent; }
QLabel#composerHint { color:#806d63; font-size:10px; background:transparent; }
QLabel#userBubble { background:#f6e5df; color:#5a3432; border:1px solid #e9c9be;
    border-radius:13px; border-top-right-radius:3px; padding:11px 14px; }
QLabel#petBubble { background:#fffdf8; border:1px solid #e8ddcd;
    border-left:3px solid #bb5148; border-radius:13px; border-top-left-radius:3px; padding:11px 14px; }
QLabel#sysBubble { color:#967563; padding:6px; background:transparent; }
QPlainTextEdit { background:#fffdf9; color:#423936; border:1px solid #decebd;
    border-radius:12px; padding:8px; selection-background-color:#ecd0c7; selection-color:#442d2a; }
QPlainTextEdit:focus { border:1px solid #b75a4d; }
QPushButton { background:#f0e7da; color:#754c42; border:1px solid #e4d5c4;
    border-radius:9px; padding:8px 11px; }
QPushButton:hover { background:#e9d8c8; border-color:#cfac98; }
QPushButton:pressed { background:#dec6b5; }
QPushButton:disabled { color:#ac9e94; background:#f1ebe2; border-color:#e8ded1; }
QPushButton#sendButton { background:#ad403b; color:#fffaf0; border:1px solid #a13935; font-weight:600; }
QPushButton#sendButton:hover { background:#94332f; }
QPushButton#sendButton:pressed { background:#7f2c29; }
QPushButton#sendButton:disabled { background:#d1aaa1; border-color:#d1aaa1; }
QPushButton#attachmentTag { background:#fff4db; border:1px solid #dfc793; color:#866238; text-align:left; }
QPushButton#moreButton { font-size:19px; padding:0; background:transparent; border-color:#dfcebc; }
QPushButton#attachButton { font-size:20px; padding:0; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:transparent; width:7px; margin:3px 0; }
QScrollBar::handle:vertical { background:#d5bbac; border-radius:3px; min-height:30px; }
QScrollBar::handle:vertical:hover { background:#b98b78; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QMenu { background:#fffaf1; border:1px solid #decbb7; padding:6px; }
QMenu::item { padding:8px 16px; border-radius:5px; }
QMenu::item:selected { background:#f2dfd3; color:#8c3933; }
'''

NIGHT_STYLE = '''
QWidget { color:#eee5dc; }
QDialog { background:#191e2a; }
QLabel#chatTitle { color:#f3e4d6; }
QLabel#chatSubtitle { color:#d5b781; }
QLabel#composerHint { color:#b5a69c; }
QLabel#userBubble { background:#3b2d35; color:#f5ded5; border-color:#62424b; }
QLabel#petBubble { background:#222937; border-color:#39414e; border-left-color:#c86760; }
QLabel#sysBubble { color:#c5ae98; }
QPlainTextEdit { background:#1c2330; color:#eee5dc; border-color:#4e4650;
    selection-background-color:#634650; selection-color:#fff5eb; }
QPlainTextEdit:focus { border-color:#cb7770; }
QPushButton { background:#2d2d38; color:#e5c4ae; border-color:#514650; }
QPushButton:hover { background:#41343e; border-color:#8c625a; }
QPushButton:pressed { background:#4d3a42; }
QPushButton:disabled { color:#a0928e; background:#292b34; border-color:#3d3d47; }
QPushButton#sendButton { background:#a64140; color:#fffaf0; border-color:#b85a55; }
QPushButton#sendButton:hover { background:#b9514c; }
QPushButton#sendButton:pressed { background:#8f3738; }
QPushButton#sendButton:disabled { background:#624044; border-color:#624044; color:#c4aca7; }
QPushButton#attachmentTag { background:#302b29; border-color:#76613d; color:#dfc28c; }
QPushButton#moreButton { border-color:#655048; }
QScrollBar::handle:vertical { background:#64515a; }
QScrollBar::handle:vertical:hover { background:#99736f; }
QMenu { background:#222633; border-color:#59474b; }
QMenu::item:selected { background:#48343d; color:#f5d7c7; }
QListWidget { background:#1c2330; border:1px solid #4e4650; }
'''


def chat_style(day):
    return STYLE if day else STYLE + NIGHT_STYLE


def glass_style(day):
    surfaces = '''
    QLabel#userBubble { background:rgba(246,229,223,210); border-radius:17px; }
    QLabel#petBubble { background:rgba(255,253,248,210); border-radius:17px; }
    QPlainTextEdit { background:rgba(255,253,249,205); border-radius:18px; }
    QPushButton { background:rgba(240,231,218,190); border-radius:14px; }
    QPushButton:hover { background:rgba(233,216,200,220); }
    QPushButton#sendButton { border-radius:18px; background:#ad403b; }
    QPushButton#attachmentTag { background:rgba(255,244,219,205); border-radius:12px; }
    QPushButton#moreButton { border-radius:17px; background:rgba(255,250,241,100); }
    ''' if day else '''
    QLabel#userBubble { background:rgba(59,45,53,210); border-radius:17px; }
    QLabel#petBubble { background:rgba(34,41,55,210); border-radius:17px; }
    QPlainTextEdit { background:rgba(28,35,48,205); border-radius:18px; }
    QPushButton { background:rgba(45,45,56,190); border-radius:14px; }
    QPushButton:hover { background:rgba(65,52,62,220); }
    QPushButton#sendButton { border-radius:18px; background:#a64140; }
    QPushButton#attachmentTag { background:rgba(48,43,41,205); border-radius:12px; }
    QPushButton#moreButton { border-radius:17px; background:rgba(25,30,42,100); }
    '''
    return chat_style(day) + surfaces


class ShrineEmblem(QWidget):
    """Slow waiting rotation and a single quiet completion pulse."""
    def __init__(self):
        super().__init__()
        self.day = True
        self.theme = 'touhou'
        self.colours = touhou_palette(True)
        self.roles = theme_roles('touhou', True)
        self._spinning = False
        self._quiet = False
        self._angle = 0.0
        self._pulse_started = None
        self._last_frame = time.monotonic()
        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(33)
        self.animation_timer.timeout.connect(self._animate)
        self.setFixedSize(52, 52)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def start_waiting(self):
        self._spinning = True
        self._pulse_started = None
        self._sync_animation()

    def finish(self, success=True):
        self._spinning = False
        self._pulse_started = time.monotonic() if success and not self._quiet and self.isVisible() else None
        self._sync_animation()

    def set_quiet(self, quiet):
        if quiet == self._quiet:
            return
        self._quiet = quiet
        if quiet:
            self._pulse_started = None
        self._sync_animation()

    def _sync_animation(self):
        self._last_frame = time.monotonic()
        if self.isVisible() and not self._quiet and (self._spinning or self._pulse_started is not None):
            self.animation_timer.start()
        else:
            self.animation_timer.stop()
        self.update()

    def _animate(self):
        now = time.monotonic()
        if self._spinning:
            self._angle = (self._angle + min(now - self._last_frame, .1) * 36) % 360
        self._last_frame = now
        if self._pulse_started is not None and now - self._pulse_started >= 1.2:
            self._pulse_started = None
        if not self._spinning and self._pulse_started is None:
            self.animation_timer.stop()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_animation()

    def hideEvent(self, event):
        self.animation_timer.stop()
        self._pulse_started = None
        super().hideEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.save()
        p.translate(26, 26)
        p.rotate(self._angle)
        p.translate(-26, -26)
        draw_motif(p, self.theme, QRectF(0, 0, 52, 52), self.colours, self.roles)
        p.restore()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.colours['gold']))
        for x, y in ((26, 1), (51, 26), (26, 51), (1, 26)):
            p.drawEllipse(QPointF(x, y), 1.4, 1.4)
        if self._pulse_started is not None and not self._quiet:
            progress = min(1., max(0., (time.monotonic() - self._pulse_started) / 1.2))
            alpha = round(math.sin(progress * math.pi) * (90 if self.day else 105))
            for i in range(18):
                colour = QColor(self.colours['vermilion'] if i % 2 == 0 else self.colours['gold'])
                colour.setAlpha(alpha)
                p.setBrush(colour)
                p.save()
                p.translate(26, 26)
                p.rotate(i * 20 + progress * 12)
                p.translate(19 + progress * 5, 0)
                p.drawEllipse(QRectF(-1.8, -.8, 3.6, 1.6))
                p.restore()


class ShrineDivider(QWidget):
    def __init__(self):
        super().__init__()
        self.day = True
        self.theme = 'touhou'
        self.colours = touhou_palette(True)
        self.roles = theme_roles('touhou', True)
        self.setFixedHeight(17)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(self.colours['gold']), 1))
        p.drawLine(0, 3, self.width(), 3)
        if self.theme != 'touhou':
            for x in (self.width() - 69, self.width() - 43, self.width() - 17):
                draw_motif(p, self.theme, QRectF(x - 7, 1, 16, 16), self.colours, self.roles)
            return
        # Folded paper streamers, like the shide on a shrine rope.
        for x in (self.width() - 69, self.width() - 43, self.width() - 17):
            paper = QPainterPath(QPointF(x, 2))
            for dx, y in ((7, 2), (3, 7), (7, 7), (1, 15), (-3, 15), (1, 10), (-3, 10)):
                paper.lineTo(x + dx, y)
            paper.closeSubpath()
            p.setBrush(QColor(self.colours['top']))
            p.drawPath(paper)


class Composer(QPlainTextEdit):
    submitted = Signal()
    fileDropped = Signal(str)

    def __init__(self):
        super().__init__()
        self._composing = False
        self.textChanged.connect(self._refresh_placeholder)
        self._refresh_placeholder()
        self.setFixedHeight(68)
        self.setAcceptDrops(True)

    def _refresh_placeholder(self):
        # IME preedit is visible before it becomes part of QTextDocument.
        hint = '' if self._composing or not self.document().isEmpty() else '说点什么…'
        if self.placeholderText() != hint:
            self.setPlaceholderText(hint)
            self.viewport().update()

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)
        self._refresh_placeholder()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._composing = False
        self._refresh_placeholder()

    def clear(self):
        self._composing = False
        super().clear()
        self._refresh_placeholder()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & Qt.ShiftModifier and not self._composing):
            self.submitted.emit()
        else:
            super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        if source.hasUrls():
            urls = source.urls()
            if len(urls) == 1 and urls[0].isLocalFile():
                self.fileDropped.emit(urls[0].toLocalFile())
            else:
                QMessageBox.information(self, '材料', '一次拖一个本地文件：文本或图片。')
            return
        self.insertPlainText(source.text())

    def canInsertFromMimeData(self, source):
        return source.hasText() or source.hasUrls()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        self.insertFromMimeData(event.mimeData())
        event.acceptProposedAction()


class ChatWindow(QWidget):
    def __init__(self, pet, worker_cls):
        super().__init__()
        self.pet, self.worker_cls = pet, worker_cls
        self.store = pet.companion.store
        self._ui_ready = False
        self._closing_application = False
        self._ui_open = False
        self._save_error = False
        root = self.store.path.parent.parent
        self.desktop_state = DesktopState(root)
        if not hasattr(pet, 'appearance'):
            pet.appearance = Appearance(root, pet)
        self.appearance = pet.appearance
        self._draft_session = self.store.session()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._flush_ui)
        self.worker = None
        self.attachment = None
        self.cur_reply = []
        self.cur_bubble = None
        self.current_id = None
        self.had_error = False
        self.stopping = False
        self.ephemeral = False
        self.follow_bottom = True
        self.setObjectName('chatRoot')
        self.setWindowTitle('小日和')
        # The character floats above apps; the chat follows normal window order.
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._glass = NativeGlass(self)
        self._glass_ready = False
        # Native ownership is released while the Qt window is still valid.
        self.destroyed.connect(lambda *_: self._glass.dispose())
        self.resize(490, 650)
        self.setMinimumSize(390, 460)
        self._day = is_daytime()
        self.setStyleSheet(self.appearance.stylesheet(glass_style(self._day), self._day))
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 17, 18, 12)
        v.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.emblem = ShrineEmblem()
        self.emblem.set_quiet(bool(self.store.focus()))
        top.addWidget(self.emblem)
        title = QVBoxLayout()
        title.setSpacing(2)
        self.head = QLabel('小日和')
        self.head.setObjectName('chatTitle')
        title.addWidget(self.head)
        subtitle = QLabel('幻想乡 · 对话札记')
        subtitle.setObjectName('chatSubtitle')
        title.addWidget(subtitle)
        top.addLayout(title, 1)
        more = QPushButton('···')
        more.setObjectName('moreButton')
        more.setFixedSize(34, 34)
        more.setToolTip('对话与约定')
        more.clicked.connect(self.menu)
        top.addWidget(more)
        v.addLayout(top)
        self.divider = ShrineDivider()
        v.addWidget(self.divider)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('messageCanvas')
        self.msgs = QVBoxLayout(holder)
        self.msgs.setContentsMargins(0, 8, 5, 12)
        self.msgs.setSpacing(14)
        self.msgs.addStretch(1)
        self.scroll.setWidget(holder)
        holder.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        v.addWidget(self.scroll, 1)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._apply_scroll_bottom)
        self.scroll.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._scroll_range_changed)
        self.attachment_btn = QPushButton()
        self.attachment_btn.setObjectName('attachmentTag')
        self.attachment_btn.setToolTip('点击移除材料；材料会随下一条消息发送')
        self.attachment_btn.clicked.connect(self.remove_attachment)
        self.attachment_btn.hide()
        v.addWidget(self.attachment_btn)
        bottom = QHBoxLayout()
        self.attach_btn = QPushButton('＋')
        self.attach_btn.setObjectName('attachButton')
        self.attach_btn.setFixedSize(36, 36)
        self.attach_btn.setToolTip('添加材料（文本或图片），也可以直接拖入。图片会自动读成文字')
        self.attach_btn.clicked.connect(self.choose_attachment)
        bottom.addWidget(self.attach_btn)
        self.input = Composer()
        self.input.submitted.connect(self.send)
        self.input.fileDropped.connect(self.load_attachment)
        bottom.addWidget(self.input, 1)
        self.btn = QPushButton('发送')
        self.btn.setObjectName('sendButton')
        self.btn.setFixedSize(60, 40)
        self.btn.clicked.connect(self.send_or_stop)
        bottom.addWidget(self.btn)
        v.addLayout(bottom)
        hint = QLabel('Enter 发送  ·  Shift+Enter 换行')
        hint.setObjectName('composerHint')
        hint.setAlignment(Qt.AlignRight)
        v.addWidget(hint)
        self.retry = QPushButton('重试上一条')
        self.retry.clicked.connect(self.retry_last)
        self.retry.hide()
        v.addWidget(self.retry)
        QShortcut(QKeySequence('Ctrl+W'), self, self.hide)
        self.restore()
        self.refresh_head()
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._refresh_theme)
        self._theme_timer.start(60_000)
        self._refresh_theme(force=True)
        self._restore_layout()
        self._restore_draft()
        self._ui_ready = True
        self.input.textChanged.connect(self._schedule_save)
        self.input.cursorPositionChanged.connect(self._schedule_save)
        self.appearance.changed.connect(self._appearance_changed)
        QApplication.instance().aboutToQuit.connect(self.prepare_quit)

    def _schedule_save(self):
        if self._ui_ready:
            self._save_timer.start()

    def _flush_ui(self):
        if not self._ui_ready:
            return
        self._save_timer.stop()
        data = self.desktop_state.data
        drafts = data.setdefault('drafts', {})
        if not isinstance(drafts, dict):
            drafts = data['drafts'] = {}
        cursor = self.input.textCursor()
        text = self.input.toPlainText()
        if text or self.attachment:
            drafts[self._draft_session] = {'text': text, 'attachment': self.attachment,
                                           'cursor': cursor.position(), 'anchor': cursor.anchor()}
        else:
            drafts.pop(self._draft_session, None)
        data['layout'] = {'geometry': bytes(self.saveGeometry().toBase64()).decode('ascii'),
                          'open': self._ui_open}
        try:
            self.desktop_state.save()
            self._save_error = False
            self.input.setToolTip('草稿和附件自动保存在本机')
        except OSError:
            if not self._save_error:
                print('[desktop] Unable to save draft/layout', flush=True)
            self._save_error = True
            self.input.setToolTip('草稿暂时无法保存，请先保留文字再退出')

    def _restore_draft(self):
        self._draft_session = self.store.session()
        drafts = self.desktop_state.data.get('drafts', {})
        draft = drafts.get(self._draft_session, {}) if isinstance(drafts, dict) else {}
        if not isinstance(draft, dict):
            draft = {}
        self.input.setPlainText(draft.get('text', '') if isinstance(draft.get('text', ''), str) else '')
        self.attachment = draft.get('attachment')
        if not (isinstance(self.attachment, dict)
                and all(isinstance(self.attachment.get(k), str) for k in ('name', 'text'))):
            self.attachment = None
        self.attachment_btn.setVisible(bool(self.attachment))
        if self.attachment:
            self.attachment_btn.setText('材料：' + self.attachment['name'] + '  ×')
        cursor = self.input.textCursor()
        for key, mode in (('anchor', QTextCursor.MoveAnchor), ('cursor', QTextCursor.KeepAnchor)):
            value = draft.get(key, 0)
            if isinstance(value, int):
                cursor.setPosition(max(0, min(value, self.input.document().characterCount()-1)), mode)
        self.input.setTextCursor(cursor)

    def _restore_layout(self):
        layout = self.desktop_state.data.get('layout', {})
        encoded = layout.get('geometry') if isinstance(layout, dict) else None
        if isinstance(encoded, str) and self.restoreGeometry(QByteArray.fromBase64(encoded.encode('ascii', errors='ignore'))):
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            return
        area = (self.pet.screen() or QApplication.primaryScreen()).availableGeometry()
        self.move(max(area.left(), min(self.pet.x()+self.pet.width()+20, area.right()-self.width()+1)),
                  max(area.top(), min(self.pet.y(), area.bottom()-self.height()+1)))

    def prepare_quit(self):
        self._closing_application = True
        self._flush_ui()

    def hideEvent(self, event):
        if self._ui_ready and not self._closing_application and not self.isMinimized():
            self._ui_open = False
            self._flush_ui()
        super().hideEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_save()

    @Slot()
    def _appearance_changed(self):
        self._refresh_theme(force=True)

    def _refresh_theme(self, force=False):
        day = is_daytime()
        if not force and day == self._day:
            return
        self._day = day
        self.setStyleSheet(self.appearance.stylesheet(glass_style(day), day))
        self._glass.update(day)
        palette = self.input.palette()
        palette.setColor(QPalette.PlaceholderText, QColor(self.appearance.roles(day)['muted']))
        self.input.setPalette(palette)
        for ornament in (self.emblem, self.divider):
            ornament.day = day
            ornament.theme = self.appearance.settings.get('theme', 'touhou')
            ornament.colours = self.appearance.palette(day)
            ornament.roles = self.appearance.roles(day)
            ornament.update()
        self.update()

    def showEvent(self, event):
        self._refresh_theme()
        super().showEvent(event)
        QTimer.singleShot(0, self._install_glass)
        self._ui_open = True
        self._schedule_save()

    def _install_glass(self):
        try:
            self._glass_ready = self._glass.install(self._day)
        except (AttributeError, OSError, RuntimeError) as error:
            print('[glass] Native effect unavailable:', type(error).__name__, flush=True)
            self._glass_ready = False
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_glass'):
            self._glass.resize()
        self._schedule_save()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        paper = QLinearGradient(0, 0, self.width(), self.height())
        colours = self.appearance.palette(self._day)
        def tint(key, alpha):
            colour = QColor(colours[key])
            colour.setAlpha(self.appearance.alpha(alpha))
            return colour
        if self._glass_ready:
            paper.setColorAt(0, tint('top', 175))
            paper.setColorAt(.45, tint('top', 130))
            paper.setColorAt(1, tint('bottom', 160))
        else:
            paper.setColorAt(0, QColor(colours['top']))
            paper.setColorAt(1, QColor(colours['bottom']))
        p.fillRect(self.rect(), paper)
        # Keep the heading readable and solid above the glass conversation.
        p.fillRect(0, 0, self.width(), self.divider.geometry().bottom() + 1,
                   QColor(colours['top']))
        p.fillRect(0, 0, self.width(), 3, QColor(colours['vermilion']))
        edge = QLinearGradient(0, 0, self.width(), self.height())
        edge.setColorAt(0, QColor(255, 255, 255, 210 if self._day else 90))
        edge.setColorAt(.5, QColor(255, 255, 255, 30))
        edge.setColorAt(1, QColor(255, 255, 255, 125 if self._day else 55))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(edge, 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 18, 18)
        # Faint concentric danmaku patterns stay behind the conversation.
        cx, cy = self.width() - 25, self.height() * .60
        pattern = QColor(colours['gold'])
        pattern.setAlpha(20 if self._day else 25)
        p.setPen(QPen(pattern, 1))
        p.setBrush(Qt.NoBrush)
        for radius in (75, 108, 142):
            p.drawEllipse(QPointF(cx, cy), radius, radius)
            for i in range(20):
                angle = i * math.pi / 10
                x, y = cx + math.cos(angle) * radius, cy + math.sin(angle) * radius
                p.save()
                p.translate(x, y)
                p.rotate(math.degrees(angle))
                p.setBrush(pattern)
                p.drawEllipse(QRectF(-5, -2, 10, 4))
                p.restore()

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def restore(self):
        while self.msgs.count() > 1:
            w = self.msgs.takeAt(0).widget()
            if w:
                w.deleteLater()
        rows = self.store.messages()
        for m in rows:
            label = m['text']
            if m['status'] in ('pending', 'failed', 'cancelled'):
                label += '（未完成，可重试）'
            self.add_bubble(label, m['role'], m['id'])
        last = self.store.last_user()
        self.retry.setVisible(bool(last and last['status'] in ('pending', 'failed', 'cancelled')))
        self._scroll_bottom(force=True)

    def add_bubble(self, text, role='assistant', mid=None):
        b = QLabel(text)
        b.setTextFormat(Qt.PlainText)
        b.setWordWrap(True)
        b.setTextInteractionFlags(Qt.TextSelectableByMouse)
        b.setMaximumWidth(max(180, int(self.width() * .76)))
        b.setObjectName('userBubble' if role == 'user' else 'petBubble' if role == 'assistant' else 'sysBubble')
        if mid and role == 'user':
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos: self.message_menu(mid, b, pos))
        row = QWidget()
        row.setObjectName('bubbleRow')
        row.setStyleSheet('#bubbleRow {background:transparent;}')
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        if role == 'user':
            h.addStretch()
        h.addWidget(b)
        if role != 'user':
            h.addStretch()
        self.msgs.insertWidget(self.msgs.count()-1, row)
        self._scroll_bottom()
        return b

    def _scroll_changed(self, value):
        bar = self.scroll.verticalScrollBar()
        self.follow_bottom = bar.maximum() - value < 45

    def _scroll_bottom(self, force=False):
        if force:
            self.follow_bottom = True
        if self.follow_bottom:
            self._scroll_timer.start(0)

    def _scroll_range_changed(self, minimum, maximum):
        # Wrapped bubbles may grow after the first queued layout pass.
        self._scroll_bottom()

    def _apply_scroll_bottom(self):
        if self.follow_bottom:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    def refresh_head(self):
        if self.busy():
            return
        f = self.store.focus()
        self.head.setText('安静陪伴中' if f else '小日和')

    def message_menu(self, mid, bubble, pos):
        menu = ThemeMenu(self.appearance, self)
        menu.addAction('复制', lambda: QApplication.clipboard().setText(bubble.text()))
        if self.store.message(mid)['status'] != 'forgotten':
            menu.addSeparator()
            for label, action in [('以后记住', 'remember'), ('更正记忆', 'correct'), ('撤回并忘记', 'forget')]:
                a = menu.addAction(label, lambda _=False, a=action: self.memory_action(mid, a))
                a.setEnabled(not self.busy())
        menu.exec(bubble.mapToGlobal(pos))
        menu.deleteLater()

    def memory_action(self, mid, action):
        try:
            if action == 'remember':
                result = self.store.remember_message(mid)
            elif action == 'correct':
                text, ok = QInputDialog.getMultiLineText(self, '更正这条记忆', '正确内容：', self.store.message(mid)['text'])
                if not ok:
                    return
                result = self.store.edit_message(mid, text)
            else:
                result = self.store.edit_message(mid)
            self.restore()
            self.add_bubble(result, 'sys')
        except (ValueError, OSError) as e:
            QMessageBox.information(self, '记忆', str(e))

    def menu(self):
        menu = ThemeMenu(self.appearance, self, heading=True)
        a = menu.addAction('另起话题', self.new_topic)
        a.setEnabled(not self.busy())
        a = menu.addAction('以前的话题', self.old_topics)
        a.setEnabled(not self.busy())
        menu.addAction('查看约定', self.pet.companion.show_tasks)
        add_appearance_menu(menu, self.appearance, self.change_appearance)
        if self.store.focus():
            menu.addAction('结束陪伴', self.pet.companion.stop_focus)
        menu.exec(self.mapToGlobal(self.rect().topRight()))
        menu.deleteLater()

    def change_appearance(self, key, value):
        try:
            self.appearance.choose(key, value)
        except OSError:
            QMessageBox.information(self, '外观', '外观设置暂时无法保存，请稍后再试。')

    def new_topic(self):
        if self.busy():
            return
        self._flush_ui()
        self.store.session(new=True)
        self.restore()
        self._restore_draft()
        self.add_bubble('新话题开始了。以前的对话仍可在菜单中找回。', 'sys')

    def old_topics(self):
        with self.store.db() as db:
            sessions = db.execute('SELECT s.id,s.created,(SELECT text FROM messages WHERE session=s.id AND role=\'user\' AND status!=\'forgotten\' ORDER BY created LIMIT 1) AS title FROM sessions s ORDER BY created DESC LIMIT 50').fetchall()
        labels = [time.strftime('%m-%d %H:%M', time.localtime(s['created'])) + '  ' + (s['title'] or '空话题')[:36] for s in sessions]
        if not labels:
            return
        # Prefix with an index so equal titles/timestamps still select the correct session.
        labels = [f'{i+1}. {label}' for i, label in enumerate(labels)]
        choice, ok = QInputDialog.getItem(self, '以前的话题', '选择要继续的话题：', labels, 0, False)
        if ok:
            self._flush_ui()
            with self.store.db() as db:
                db.execute('UPDATE sessions SET active=0')
                db.execute('UPDATE sessions SET active=1 WHERE id=?', (sessions[labels.index(choice)]['id'],))
            self.restore()
            self._restore_draft()

    def choose_attachment(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择材料', '',
            '文本与图片 (*.txt *.text *.md *.markdown *.png *.jpg *.jpeg *.webp *.gif *.bmp)'
            ';;文本材料 (*.txt *.text *.md *.markdown)'
            ';;图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp)')
        if path:
            self.load_attachment(path)

    def load_attachment(self, path):
        if self.busy():
            return
        try:
            self.attachment = C.read_attachment(path)
            self.attachment_btn.setText('材料：' + self.attachment['name'] + '  ×')
            self.attachment_btn.show()
            self._flush_ui()
        except (ValueError, OSError) as e:
            QMessageBox.information(self, '材料', str(e))

    def remove_attachment(self):
        self.attachment = None
        self.attachment_btn.hide()
        self._flush_ui()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.input.insertFromMimeData(event.mimeData())
        event.acceptProposedAction()

    def send_or_stop(self):
        if self.busy():
            self.stopping = True
            self.emblem.finish(success=False)
            self.worker.requestInterruption()
            self.btn.setEnabled(False)
            self.head.setText('正在停止…')
        else:
            self.send()

    def send(self):
        q = self.input.toPlainText().strip()
        if self.busy() or (not q and not self.attachment):
            return
        q = q or '请帮我解释这份材料。'
        if len(q) > 24000:
            QMessageBox.information(self, '消息较长', '请把消息缩短到 24,000 字以内。')
            return
        self._scroll_bottom(force=True)
        try:
            result = C.local_command(q, self.store) if not self.attachment else None
        except (ValueError, OSError) as e:
            result = str(e)
        if result is not None:
            self.input.clear()
            # Memory-control commands are not stored as fresh autobiographical facts.
            self.restore()
            if not any(word in q for word in ('别保存', '不要保存', '忘掉', '撤回', '记错', '记住')):
                mid = self.store.add_message('user', q)
                self.store.add_message('assistant', result)
                self.add_bubble(q, 'user', mid)
            else:
                self.add_bubble('记忆操作', 'sys')
            self.add_bubble(result)
            self.pet.companion.tick()
            self._flush_ui()
            return
        history = self.store.history(q)
        context = C.material_query(q, self.attachment)
        label = q + ('\n〔材料：' + self.attachment['name'] + '〕' if self.attachment else '')
        self.ephemeral = bool(M.is_sensitive(context))
        self.current_id = None if self.ephemeral else self.store.add_message('user', label, context, 'pending')
        self.add_bubble(label, 'user', self.current_id)
        self.input.clear()
        self.remove_attachment()
        self._start(context, history)

    def retry_last(self):
        if self.busy():
            return
        m = self.store.last_user()
        if not m or m['status'] not in ('failed', 'cancelled', 'pending'):
            return
        self.current_id = m['id']
        self.ephemeral = False
        self.store.set_status(m['id'], 'pending')
        self._start(m['context'], self.store.history(m['text']))

    def _start(self, query, history):
        self.had_error = self.stopping = False
        self.cur_reply = []
        self.cur_bubble = None
        self.retry.hide()
        self.btn.setText('停止')
        self.btn.setEnabled(True)
        self.input.setEnabled(False)
        self.attach_btn.setEnabled(False)
        self._scroll_bottom(force=True)
        self.head.setText('正在想…')
        self.emblem.start_waiting()
        self.pet.companion.activity('thinking')
        self.worker = self.worker_cls(query, history)
        self.worker.memory_message_id = "__ephemeral__" if self.ephemeral else self.current_id
        self.worker.chunk.connect(self.on_chunk)
        self.worker.finished.connect(self.on_done)
        self.worker.start()

    def on_chunk(self, kind, text):
        if self.stopping:
            return
        if kind == 'content':
            if self.cur_bubble is None:
                self.cur_bubble = self.add_bubble('')
                self.pet.companion.activity('replying')
            self.cur_reply.append(text)
            self.cur_bubble.setText(''.join(self.cur_reply))
            self._scroll_bottom()
            self.head.setText('正在说…')
        elif kind == 'tool':
            self.head.setText('正在处理…')
            self.pet.companion.activity('searching')
        elif kind == 'error':
            self.had_error = True
            self.emblem.finish(success=False)
            self.add_bubble(text, 'sys')

    def on_done(self):
        reply = ''.join(self.cur_reply)
        state = 'cancelled' if self.stopping else 'failed' if self.had_error or not reply else 'complete'
        if self.current_id:
            self.store.set_status(self.current_id, state)
            if reply:
                self.store.add_message('assistant', reply, status=state)
            if state == 'complete':
                try:
                    self.store.record_memory(self.current_id)
                except (ValueError, OSError) as e:
                    self.add_bubble('对话已保存，但长期记忆未写入：' + str(e), 'sys')
        elif self.ephemeral:
            self.add_bubble('本轮触发现有隐私过滤，未保存对话或材料。', 'sys')
        if self.stopping:
            self.add_bubble('已停止。', 'sys')
        elif not reply and not self.had_error:
            self.add_bubble('没有收到回复，可以重试。', 'sys')
        self.btn.setText('发送')
        self.btn.setEnabled(True)
        self.input.setEnabled(True)
        self.attach_btn.setEnabled(True)
        self.retry.setVisible(state != 'complete' and self.current_id is not None)
        self.pet.companion.activity('done' if state == 'complete' else 'error')
        self.emblem.finish(success=state == 'complete')
        self.pet.companion.tick()
        self.refresh_head()
        self.input.setFocus()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class ReminderBubble(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.task = None
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setStyleSheet(STYLE + 'ReminderBubble {background:#f6fbf8; border:1px solid #dce8e1;}')
        self.setFixedWidth(290)
        v = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        v.addWidget(self.label)
        h = QHBoxLayout()
        self.done = QPushButton('完成')
        self.done.clicked.connect(lambda: self.act('complete'))
        h.addWidget(self.done)
        later = QPushButton('稍后 10 分钟')
        later.clicked.connect(lambda: self.act('snooze'))
        h.addWidget(later)
        v.addLayout(h)

    def show_task(self, task):
        self.task = task
        title = '这一段陪伴到时间了。' if task['kind'] == 'focus' else task['title']
        if task['due'] and time.time() - task['due'] > 60:
            title = '补一条错过的提醒：\n' + title
        self.label.setText(title)
        self.adjustSize()
        self.controller.position(self)
        self.show()

    def act(self, action):
        if self.task:
            self.controller.store.task_action(self.task['id'], action)
        self.task = None
        self.hide()
        self.controller.tick()


class CompanionController:
    def __init__(self, pet):
        self.pet = pet
        self.store = C.Store()
        self.popup = ReminderBubble(self)
        self.counter = QLabel()
        self.counter.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.counter.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.counter.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.counter.setStyleSheet('background:#e9f3ef;color:#466458;border-radius:8px;padding:6px 10px;font-size:12px;')
        self._quiet = False
        self._activity = 'idle'
        self.timer = QTimer(pet)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

    def position(self, widget):
        screen = self.pet.screen().availableGeometry()
        x = max(screen.left(), min(self.pet.x() + self.pet.width()//2-widget.width()//2, screen.right()-widget.width()+1))
        y = self.pet.y()-widget.height()-8
        if y < screen.top():
            y = min(screen.bottom()-widget.height()+1, self.pet.y()+self.pet.height()+8)
        widget.move(x, max(screen.top(), y))

    def tick(self, visit=False):
        due = self.store.due_tasks(visit=visit)
        if self.popup.task and self.popup.task['id'] not in {t['id'] for t in due}:
            self.popup.task = None
            self.popup.hide()
        if not self.popup.task and due:
            self.popup.show_task(due[0])
        if self.popup.isVisible():
            self.position(self.popup)
        f = self.store.focus()
        quiet = bool(f)
        if quiet != self._quiet:
            self._quiet = quiet
            if quiet:
                self.pet.panel.fade_out()
                if self.pet.bubble_win:
                    self.pet.bubble_win.hide()
            if self.pet.gl:
                self.pet.gl.set_quiet(quiet)
        if f and self.pet.state.get('show_focus_timer', True) and not self.popup.isVisible():
            seconds = max(0, math.ceil(f['due']-time.time()))
            self.counter.setText(f'陪伴中  {seconds//60:02d}:{seconds%60:02d}')
            self.counter.adjustSize()
            self.position(self.counter)
            self.counter.show()
        else:
            self.counter.hide()
        if self.pet.chat:
            self.pet.chat.emblem.set_quiet(quiet)
            self.pet.chat.refresh_head()

    def activity(self, state):
        if state == self._activity:
            return
        self._activity = state
        if self.pet.gl and not self._quiet:
            self.pet.gl.set_activity(state)
        if state in ('done', 'error'):
            QTimer.singleShot(1800, lambda: self.activity('idle') if self._activity == state else None)

    def stop_focus(self):
        f = self.store.focus()
        if f:
            self.store.task_action(f['id'], 'cancel')
        self.tick()

    def show_tasks(self):
        dialog = QDialog(self.pet)
        dialog.setWindowTitle('约定')
        dialog.resize(430, 300)
        dialog.setStyleSheet(chat_style(is_daytime()))
        v = QVBoxLayout(dialog)
        v.addWidget(QLabel('应用关闭时不会弹出；错过的提醒会在重启后补发。'))
        listing = QListWidget()
        v.addWidget(listing)
        def refresh():
            listing.clear()
            for task in self.store.tasks():
                listing.addItem(C.task_description(task))
                listing.item(listing.count()-1).setData(Qt.UserRole, task['id'])
            if not listing.count():
                listing.addItem('目前没有未完成的约定。')
        def action(kind):
            item = listing.currentItem()
            if item and item.data(Qt.UserRole):
                try:
                    self.store.task_action(item.data(Qt.UserRole), kind)
                except ValueError:
                    pass
                refresh()
                self.tick()
        buttons = QHBoxLayout()
        for label, kind in [('完成', 'complete'), ('稍后 10 分钟', 'snooze'), ('取消约定', 'cancel')]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, k=kind: action(k))
            buttons.addWidget(button)
        v.addLayout(buttons)
        refresh()
        dialog.exec()


def open_local(path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
