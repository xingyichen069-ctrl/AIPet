"""AppKit glass underneath Qt's own content view, without intercepting input."""
import ctypes as C
import platform
import sys
from ui_theme import touhou_palette


class Point(C.Structure):
    _fields_ = [('x', C.c_double), ('y', C.c_double)]


class Size(C.Structure):
    _fields_ = [('width', C.c_double), ('height', C.c_double)]


class Rect(C.Structure):
    _fields_ = [('origin', Point), ('size', Size)]


class NativeGlass:
    def __init__(self, widget):
        self.widget = widget
        self.view = self.window = self.effect = None
        self.titlebar = None
        self.kind = 'fallback'
        from PySide6.QtWidgets import QApplication
        if sys.platform != 'darwin' or QApplication.platformName() != 'cocoa':
            return
        self.objc = C.CDLL('/usr/lib/libobjc.A.dylib')
        self.objc.objc_getClass.argtypes = [C.c_char_p]
        self.objc.objc_getClass.restype = C.c_void_p
        self.objc.sel_registerName.argtypes = [C.c_char_p]
        self.objc.sel_registerName.restype = C.c_void_p
        self._calls = {}

    def send(self, receiver, selector, result=C.c_void_p, *args):
        types = tuple(t for t, _ in args)
        key = (result, types)
        if key not in self._calls:
            self._calls[key] = C.CFUNCTYPE(result, C.c_void_p, C.c_void_p, *types)(('objc_msgSend', self.objc))
        return self._calls[key](receiver, self.objc.sel_registerName(selector.encode()), *(v for _, v in args))

    def cls(self, name):
        return self.objc.objc_getClass(name.encode())

    def frame(self, view=None, selector_name='frame'):
        target = view or self.view
        selector = self.objc.sel_registerName(selector_name.encode())
        if platform.machine() == 'x86_64':
            value = Rect()
            call = C.CFUNCTYPE(None, C.POINTER(Rect), C.c_void_p, C.c_void_p)(('objc_msgSend_stret', self.objc))
            call(C.byref(value), target, selector)
            return value
        return self.send(target, selector_name, Rect)

    def install(self, day):
        if not hasattr(self, 'objc'):
            return False
        if self.effect:
            self.update(day)
            return True
        self.view = int(self.widget.winId())
        self.window = self.send(self.view, 'window')
        parent = self.send(self.view, 'superview')
        if not parent or not self.window:
            return False
        glass_class = self.cls('NSGlassEffectView')
        self.kind = 'liquid-glass' if glass_class else 'frosted-glass'
        self.effect = self.send(self.send(glass_class or self.cls('NSVisualEffectView'), 'alloc'),
                                'initWithFrame:', C.c_void_p, (Rect, self.frame()))
        if glass_class:
            self.send(self.effect, 'setStyle:', None, (C.c_long, 0))  # Regular glass.
            self.send(self.effect, 'setCornerRadius:', None, (C.c_double, 18))
        else:
            self.send(self.effect, 'setMaterial:', None, (C.c_long, 21))
            self.send(self.effect, 'setBlendingMode:', None, (C.c_long, 0))
        # A sibling underlay leaves Qt's contentView, focus, and hit testing intact.
        self.send(parent, 'addSubview:positioned:relativeTo:', None,
                  (C.c_void_p, self.effect), (C.c_long, -1), (C.c_void_p, self.view))
        self.send(self.effect, 'setAutoresizingMask:', None, (C.c_ulong, 2 | 16))
        self.send(self.window, 'setOpaque:', None, (C.c_bool, False))
        clear = self.send(self.cls('NSColor'), 'clearColor')
        self.send(self.window, 'setBackgroundColor:', None, (C.c_void_p, clear))
        # A solid backing colours the native title bar while its controls stay above it.
        self.titlebar = self.send(self.send(self.cls('NSView'), 'alloc'), 'initWithFrame:',
                                  C.c_void_p, (Rect, Rect()))
        self.send(self.titlebar, 'setWantsLayer:', None, (C.c_bool, True))
        self.send(parent, 'addSubview:positioned:relativeTo:', None,
                  (C.c_void_p, self.titlebar), (C.c_long, -1), (C.c_void_p, self.view))
        self.send(self.window, 'setTitlebarAppearsTransparent:', None, (C.c_bool, True))
        self.update(day)
        return True

    def update(self, day):
        if not self.effect:
            return
        name = self.send(self.cls('NSString'), 'stringWithUTF8String:', C.c_void_p,
                         (C.c_char_p, b'NSAppearanceNameAqua' if day else b'NSAppearanceNameDarkAqua'))
        appearance = self.send(self.cls('NSAppearance'), 'appearanceNamed:', C.c_void_p, (C.c_void_p, name))
        self.send(self.window, 'setAppearance:', None, (C.c_void_p, appearance))
        self.send(self.effect, 'setAppearance:', None, (C.c_void_p, appearance))
        colour = touhou_palette(day)['top'].lstrip('#')
        components = [int(colour[i:i+2], 16) / 255 for i in (0, 2, 4)]
        solid = self.send(self.cls('NSColor'), 'colorWithSRGBRed:green:blue:alpha:', C.c_void_p,
                          *((C.c_double, v) for v in (*components, 1.0)))
        layer = self.send(self.titlebar, 'layer')
        self.send(layer, 'setBackgroundColor:', None, (C.c_void_p, self.send(solid, 'CGColor')))
        if self.kind == 'liquid-glass':
            tint = self.send(self.cls('NSColor'), 'colorWithSRGBRed:green:blue:alpha:', C.c_void_p,
                             *((C.c_double, v) for v in (*components, .12)))
            self.send(self.effect, 'setTintColor:', None, (C.c_void_p, tint))
        self.resize()

    def resize(self):
        if self.effect:
            content = self.frame()
            self.send(self.effect, 'setFrame:', None, (Rect, content))
            if self.titlebar:
                parent = self.send(self.view, 'superview')
                bounds = self.frame(parent, 'bounds')
                y = content.origin.y + content.size.height
                title = Rect(Point(bounds.origin.x, y),
                             Size(bounds.size.width, max(0, bounds.origin.y + bounds.size.height - y)))
                self.send(self.titlebar, 'setFrame:', None, (Rect, title))

    def dispose(self):
        if self.titlebar:
            self.send(self.titlebar, 'removeFromSuperview', None)
            self.send(self.titlebar, 'release', None)
            self.titlebar = None
        if self.effect:
            self.send(self.effect, 'removeFromSuperview', None)
            self.send(self.effect, 'release', None)
            self.effect = None
