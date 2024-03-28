import os
from ctypes import *
import mmap
import weakref

__all__ = [
    'FLAG_ALIGN_RIGHT',
    'FLAG_ALIGN_CENTER',
    'ON_TOP',
    'ON_BOTTOM',
    'AT_TOP',
    'AT_BOTTOM',
    'ON_LEFT',
    'ON_RIGHT',
    'AT_LEFT',
    'AT_RIGHT',
    'CENTER_OF',
    'Widget',
    'widget_decorator',
    'WidgetConfig',
]

FLAG_ALIGN_RIGHT = 1
FLAG_ALIGN_CENTER = 2

ON_TOP = 0
ON_BOTTOM = 1
AT_TOP = 2
AT_BOTTOM = 3

ON_LEFT = 0
ON_RIGHT = 1
AT_LEFT = 2
AT_RIGHT = 3

CENTER_OF = 4

txtbuf_types = {}

PREV = object()

def char_array(buf, ofs, size):
    typ = txtbuf_types.get(size)
    if not typ:
        typ = txtbuf_types[size] = (c_char * size)
    return typ.from_buffer(buf, ofs)

class Widget(Structure):
    _fields_ = [
        ('version', c_uint32),
        ('cvisgroup', c_uint32),
        ('cvismask', c_uint32),
        ('cflags', c_uint32),
        ('cxscale', c_double),
        ('cfg', c_uint32),
        ('cbg', c_uint32),
        ('cstrike', c_uint32),
        ('cx', c_int16),
        ('cy', c_int16),
        ('cw', c_uint16),
        ('ch', c_uint16),
        ('cxo', c_int16),
        ('cyo', c_int16),
        ('ctextsize', c_uint8),
        ('ctextptr', c_uint16),
        ('ctype', c_uint8),
        ('cnchar', c_uint8),
        ('cfont', c_uint8),
    ]

    w = h = 0
    bg = 0xFF000000
    fg = 0xFFFFFFFF

    xo = yo = 0
    font = 0
    textsize = 24
    xscale = 1
    nchar = 0
    strike = 0
    wtype = 0


    visgroup = 0
    vismask = 0

    def __init__(self):
        self.last_rawval = None
        self.textbuf = None

    def get_rawval(self, hud, cd):
        return getattr(cd, self.field)

    def update_rawval(self, rv):
        self.text = self.fmt % rv

    def set_text(self, text):
        self.textbuf.value = text.encode('utf8')[:self.nchar]

    def bump_version(self):
        self.version += 1

    def getkey(self):
        return type(self).__name__

    def post_build(self):
        pass

    def check(self, cd):
        rv = self.get_rawval(hud, cd)
        if rv != self.last_rawval:
            self.last_rawval = rv
            self.update_rawval(rv)


class MemHeader(Structure):
    _fields_ = [
        ('version', c_uint32),
        ('numwidgets', c_uint32),
        ('visibility', c_uint32),
    ]

def widget_decorator(widget_list):
    def decorator(cls):
        widget_list.append(cls)
        return cls
    return decorator



def set_pos(wjt, pos, pf, sf, by_key):
    if isinstance(pos, tuple):
        where = pos[0]
        who = by_key.get(pos[1], pos[1])

        margin = pos[2] if len(pos) > 2 else 0

        p1 = getattr(who, pf)
        p2 = p1 + getattr(who, sf)

        if where == ON_RIGHT:
            pos = p2 + margin
        elif where == ON_LEFT:
            pos = p1 - getattr(wjt, sf) - margin
        elif where == AT_RIGHT:
            pos = p2 - getattr(wjt, sf) - margin
        elif where == AT_LEFT:
            pos = p1 + margin
        elif where == CENTER_OF:
            pos = (p1 + p2 - getattr(wjt, sf)) // 2 + margin

    setattr(wjt, pf, pos)

class WidgetConfig:
    def __init__(self, buf):
        self.buf = buf
        self.hdr = MemHeader.from_buffer(buf)
        self.widgets = []
        self.version = None

    @classmethod
    def from_mmap(cls, path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fsize = os.lseek(fd, 0, 2)
        size = 32768
        if fsize < size:
            os.ftruncate(fd, size)
        mm = mmap.mmap(fd, size)
        return cls(mm)

    def parse(self, wclass):
        while True:
            self.version = vers_start = self.hdr.version
            buf = self.buf
            widgets = self.widgets = []
            nw = self.hdr.numwidgets
            pos = sizeof(MemHeader)
            size_spec = sizeof(Widget)

            for j in range(nw):
                w = wclass.from_buffer(buf, pos)
                pos += size_spec
                widgets.append(w)
                nc = w.cnchar
                if nc:
                    w.textbuf = char_array(self.buf, w.ctextptr, nc + 1)
                w.lasttext = w.lastfg = w.lastbg = w.laststrike = None

            if vers_start == self.hdr.version:
                return

    def check_parse(self, wclass):
        if self.hdr.version != self.version:
            self.parse(wclass)
            return True
        return False

    def set_visgroup(self, mask, group):
        vis = self.hdr.visibility
        vis &= ~mask
        vis |= group
        self.hdr.visibility = vis

    def build(self, widgetlist, sw, sh):
        self.hdr.visibility = 0
        self.hdr.numwidgets = 0
        self.hdr.version += 1
        self.widgets = []
        scr = Widget()

        buf = self.buf

        scr.w = sw
        scr.h = sh
        by_key = self.by_key = {'screen': scr}

        pos = sizeof(MemHeader)
        size_spec = sizeof(Widget)

        textptr = pos + size_spec * len(widgetlist)

        for cls in widgetlist:
            if isinstance(cls, type):
                widget = cls.from_buffer(buf, pos)
            else:
                widget = cls(buf, pos)

            pos += size_spec
            by_key[widget.getkey()] = widget
            self.widgets.append(widget)

        for wjt in self.widgets:
            wjt.cw = wjt.w
            wjt.ch = wjt.h
            wjt.cbg = wjt.bg
            wjt.cfg = wjt.fg
            wjt.cxo = wjt.xo
            wjt.cyo = wjt.yo
            wjt.cfont = wjt.font
            wjt.ctextsize = wjt.textsize
            wjt.cxscale = wjt.xscale
            wjt.cflags = wjt.flags
            wjt.cvisgroup = wjt.visgroup
            wjt.cvismask = wjt.vismask
            wjt.cstrike = wjt.strike
            wjt.ctype = wjt.wtype

            if wjt.nchar:
                wjt.ctextptr = textptr
                wjt.textbuf = char_array(self.buf, textptr, wjt.nchar + 1)
                wjt.textbuf[0] = 0
                wjt.textbuf[wjt.nchar] = 0
                textptr += wjt.nchar + 1
            else:
                wjt.textptr = 0
                wjt.textbuf = None

            wjt.cnchar = wjt.nchar + 1

            set_pos(wjt, wjt.xpos, 'cx', 'w', by_key)
            set_pos(wjt, wjt.ypos, 'cy', 'h', by_key)
            by_key['prev'] = wjt

        for wjt in self.widgets:
            wjt.post_build()

        self.hdr.numwidgets = len(self.widgets)
        self.hdr.version += 1

def test():
    buf = bytearray(128)
    wc = WidgetConfig.from_mmap('/dev/shm/hud')

    lst = []
    wjt = widget_decorator(lst)

    @wjt
    class TestWidget1(Widget):
        w = 100
        h = 50
        xpos = CENTER_OF, 'screen'
        ypos = AT_TOP, 'screen'
        nchar = 10
        fg = 0x0099FF
        bg = 0xFF9900FF
        xscale = .5
        flags = FLAG_ALIGNR
        yo = 38
        textsize = 40

    @wjt
    class TestWidget2(Widget):
        w = 80
        h = 50
        xpos = ON_LEFT, 'TestWidget1', 5
        ypos = AT_TOP, 'screen', 15
        nchar = 10
        fg = 0x0099FF
        bg = 0xFF9900FF
        xscale = .5
        flags = FLAG_ALIGNR
        yo = 38
        textsize = 40

    wc.build(lst, 800, 480)
    w1, w2 = wc.widgets[:2]
    w1.set_text('12384')
    w2.set_text('dfajsdlkjfsalkfhsadlghasdlfjaeglkjag')
    print(wc.widgets)
    for w in wc.widgets:
        print(w.cx, w.cy, w.cw, w.ch, w.ctextptr, w.textbuf.value)

    wc.parse(Widget)
    for w in wc.widgets:
        print(w.cx, w.cy, w.cw, w.ch, w.ctextptr, w.textbuf.value)

    print(buf)
    x = 42
    import time
    while True:
        x += 1
        print(x)
        w1.set_text('%04x' % x)
        w1.bump_version()
        time.sleep(1)
        w2.set_text('%04d' % x)
        w2.bump_version()
        time.sleep(1)
#def parse_spec(buf):
if __name__ == '__main__':
    test()
