#!/usr/bin/env python3
"""EXE//HUB - console-style game launcher for Windows.

Keyboard : arrows browse, Enter play, Tab switch style, Del remove, F2 edit,
           F11 windowed/fullscreen, Esc back, just type to search, Ctrl+O add games
Mouse    : click select, double-click play, right-click options, wheel browse,
           drop .exe files anywhere to add them
Xbox pad : D-pad / left stick browse, A play, Y options, X add, Back = style,
           Start = fullscreen, LB/RB skip
Spotify  : the corner card shows what Spotify is playing (read from Windows, no login)
"""
import sys, os, io, json, uuid, time, math, random, subprocess, hashlib, ctypes
import asyncio, threading, shutil, traceback, colorsys, faulthandler
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

try:
    import psutil
except Exception:          # tracking falls back to the single launched process
    psutil = None

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, QUrl, QEvent
from PySide6.QtGui import (
    QPixmap, QIcon, QPainter, QColor, QFont, QFontMetrics, QPen, QLinearGradient,
    QRadialGradient, QPainterPath, QDesktopServices, QImage, QCursor, QGuiApplication,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QWidget, QMenu, QFileDialog, QMessageBox, QDialog, QFormLayout,
    QLineEdit, QHBoxLayout, QPushButton, QDialogButtonBox, QColorDialog, QCheckBox, QSystemTrayIcon,
)

TW, TH = 240, 308          # base card art size (rendered at 2x, scaled at paint time)
ART_R = 22
SPOTIFY_GREEN = QColor("#1ED760")
BRAND1, BRAND2, BRAND3 = QColor(79, 107, 255), QColor(190, 75, 255), QColor(255, 95, 140)   # logo gradient
BOOT_LEN = 2.7             # intro length in seconds (any key / click skips it)
OUTRO_LEN = 1.7            # outro length in seconds
ICON_VERSION = 2           # bump to re-extract every game's icon once after an upgrade


# ====================================================================== storage
def cfg_dir() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home())))
    d = base / "exehub"
    (d / "icons").mkdir(parents=True, exist_ok=True)
    (d / "backups").mkdir(parents=True, exist_ok=True)
    return d


CFG = cfg_dir()
DB = CFG / "games.json"
BAK = CFG / "games.json.bak"
TMP = CFG / "games.json.tmp"
ICONS = CFG / "icons"
BACKUPS = CFG / "backups"
LOG = CFG / "exehub.log"


def log(msg) -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.replace(CFG / "exehub.log.old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except Exception:
        pass


def _read(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict) or not isinstance(d.get("games"), list):
        raise ValueError("not a library file")
    return d


def _valid(path: Path) -> bool:
    try:
        _read(path)
        return True
    except Exception:
        return False


LOAD_NOTE = ""


def load() -> dict:
    """Never silently discards a library: unreadable file -> quarantined copy + restore from backup."""
    global LOAD_NOTE
    cands = [DB, BAK] + sorted(BACKUPS.glob("games-*.json"), reverse=True)
    main_bad = False
    for path in cands:
        if not path.exists():
            continue
        try:
            d = _read(path)
        except Exception as ex:
            log(f"load failed for {path.name}: {ex}")
            if path == DB:
                main_bad = True
                try:
                    shutil.copy2(DB, CFG / f"games.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
                except Exception:
                    pass
            continue
        if path != DB:
            LOAD_NOTE = f"Library restored from backup ({path.name})"
            log(LOAD_NOTE)
        return d
    if main_bad:
        LOAD_NOTE = "Library file was unreadable - a copy was kept in the exehub folder"
    return {"games": []}


_last_snap = 0.0


def save(data: dict, allow_empty: bool = False) -> bool:
    """Atomic save (temp file -> swap) with rolling .bak and timed snapshots."""
    global _last_snap
    try:
        if not data.get("games") and not allow_empty and DB.exists() and _valid(DB) and _read(DB)["games"]:
            log("refused to replace a non-empty library with an empty one")
            return False
        with open(TMP, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        if DB.exists() and _valid(DB):
            shutil.copy2(DB, BAK)
        for attempt in range(6):
            try:
                os.replace(TMP, DB)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.15)   # antivirus / indexer briefly holding the file
        now = time.time()
        if now - _last_snap > 1800 and data.get("games"):
            _last_snap = now
            shutil.copy2(DB, BACKUPS / f"games-{time.strftime('%Y%m%d-%H%M%S')}.json")
            for old in sorted(BACKUPS.glob("games-*.json"))[:-20]:
                old.unlink(missing_ok=True)
        return True
    except Exception:
        log("save failed:\n" + traceback.format_exc())
        return False


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ====================================================================== helpers
def clamp(x, a, b):
    return max(a, min(b, x))


def lerp(a, b, t):
    return a + (b - a) * t


def ease_out(t):
    return 1 - (1 - clamp(t, 0, 1)) ** 3


def ease_out_back(t):
    t = clamp(t, 0, 1) - 1
    return 1 + 2.70158 * t ** 3 + 1.70158 * t ** 2


def force_foreground(hwnd) -> None:
    """Windows refuses to let a background app steal focus; tapping ALT first is the classic way around that."""
    try:
        u = ctypes.windll.user32
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        u.BringWindowToTop(ctypes.c_void_p(hwnd))
    except Exception:
        pass


def hsv(h, s, v, a=1.0) -> QColor:
    c = QColor()
    c.setHsvF(h % 1.0, clamp(s, 0, 1), clamp(v, 0, 1), clamp(a, 0, 1))
    return c


def col(rgb, a=1.0) -> QColor:
    return QColor.fromRgbF(clamp(rgb[0], 0, 1), clamp(rgb[1], 0, 1), clamp(rgb[2], 0, 1), clamp(a, 0, 1))


def alpha(c: QColor, a: float) -> QColor:
    c2 = QColor(c)
    c2.setAlphaF(clamp(a, 0, 1))
    return c2


def rgb_of(c: QColor):
    return [c.redF(), c.greenF(), c.blueF()]


def ui_font(px, weight=QFont.Weight.Normal, spacing=0.0) -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI Variable Display", "Segoe UI", "Inter", "Helvetica Neue", "DejaVu Sans", "Sans Serif"])
    f.setPixelSize(int(px))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def fmt_played(g: dict) -> str:
    s = int(g.get("seconds", 0))
    if s < 60:
        return "<1m played" if g.get("last_played") else ""
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m played" if h else f"{m}m played"


def fmt_clock(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def ago(ts: float) -> str:
    if not ts:
        return "Never played"
    d = time.time() - ts
    if d < 90:
        return "Played just now"
    if d < 3600:
        return f"Played {int(d // 60)}m ago"
    if d < 86400:
        return f"Played {int(d // 3600)}h ago"
    days = int(d // 86400)
    if days == 1:
        return "Played yesterday"
    if days < 30:
        return f"Played {days} days ago"
    return "Played " + time.strftime("%b %d", time.localtime(ts))


def prettify(stem: str) -> str:
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or stem


# ====================================================================== icons (quality pipeline)
def extract_icon(exe: str, out: Path) -> bool:
    """Best icon we can find: biggest image across the exe's icon groups, else a sibling .ico."""
    best = None   # (area, PIL image)
    try:
        from icoextract import IconExtractor
        from PIL import Image
        ie = IconExtractor(exe)
        try:
            groups = ie.list_group_icons() or []
        except Exception:
            groups = []
        for i in range(max(1, min(len(groups), 6))):
            try:
                im = Image.open(io.BytesIO(ie.get_icon(i).getvalue()))
                sizes = im.info.get("sizes") or {im.size}
                sz = max(sizes, key=lambda s: s[0] * s[1])
                im.size = sz
                im = im.convert("RGBA")
                if best is None or sz[0] * sz[1] > best[0]:
                    best = (sz[0] * sz[1], im)
            except Exception:
                continue
    except Exception:
        pass
    if best is None:
        try:
            from PIL import Image
            for f in Path(exe).parent.glob("*.ico"):
                try:
                    im = Image.open(f)
                    sizes = im.info.get("sizes") or {im.size}
                    sz = max(sizes, key=lambda s: s[0] * s[1])
                    im.size = sz
                    im = im.convert("RGBA")
                    if best is None or sz[0] * sz[1] > best[0]:
                        best = (sz[0] * sz[1], im)
                except Exception:
                    continue
        except Exception:
            pass
    if best is None:
        return False
    try:
        best[1].save(out)
        return True
    except Exception:
        return False


_icon_src_cache: dict = {}
_icon_px_cache: dict = {}


def icon_source(g: dict):
    """(trimmed PIL RGBA image or None, longest side)."""
    path = ICONS / f"{g['id']}.png"
    try:
        key = (g["id"], path.stat().st_mtime)
    except OSError:
        return None, 0
    if key in _icon_src_cache:
        return _icon_src_cache[key]
    res = (None, 0)
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA")
        bbox = im.getchannel("A").point(lambda a: 255 if a > 10 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)       # drop transparent padding so icons fill their slot evenly
        res = (im, max(im.size))
    except Exception:
        pass
    _icon_src_cache[key] = res
    return res


def pil_to_pixmap(im) -> QPixmap:
    im = im.convert("RGBA")
    qi = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, im.width * 4, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(qi)


def _resample(im, nw, nh, scale):
    """Lanczos resize that keeps edges clean: transparent areas are filled with the icon's own colour first
    (no dark halo), and small icons get a light blur+sharpen so their stair-steps turn into smooth curves."""
    from PIL import Image, ImageFilter, ImageStat
    r, g, b, a = im.split()
    rgb = Image.merge("RGB", (r, g, b))
    try:
        st = ImageStat.Stat(rgb, a.point(lambda v: 255 if v > 128 else 0))
        mean = tuple(int(x) for x in st.mean[:3])
    except Exception:
        mean = (128, 128, 128)
    rgb = Image.composite(rgb, Image.new("RGB", rgb.size, mean), a)
    if scale >= 1:
        big = rgb.resize((nw, nh), Image.LANCZOS)
        ba = a.resize((nw, nh), Image.LANCZOS)
        if scale > 1.4:
            big = big.filter(ImageFilter.GaussianBlur(0.5 * scale)).filter(
                ImageFilter.UnsharpMask(radius=0.8 * scale, percent=110, threshold=1))
            ba = ba.filter(ImageFilter.GaussianBlur(0.5 * scale)).filter(
                ImageFilter.UnsharpMask(radius=0.8 * scale, percent=110, threshold=1))
    else:
        big = rgb.resize((nw, nh), Image.LANCZOS, reducing_gap=3.0)
        ba = a.resize((nw, nh), Image.LANCZOS, reducing_gap=3.0)
    return Image.merge("RGBA", (*big.split(), ba))


def icon_pixmap(g: dict, want: int):
    """Icon resampled with Lanczos. Small icons are NEVER blown up more than 2.5x (that's what looked pixelated)."""
    im, native = icon_source(g)
    if im is None:
        return None
    key = (g["id"], int(want), native)
    if key in _icon_px_cache:
        return _icon_px_cache[key]
    if len(_icon_px_cache) > 400:
        _icon_px_cache.clear()
    try:
        size = float(want) if native * 5.0 >= want else min(float(want), max(native * 5.0, 112.0))   # 5x device px == 2.5x on screen
        scale = size / native
        nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
        pm = pil_to_pixmap(_resample(im, nw, nh, scale))
    except Exception:
        pm = None
    _icon_px_cache[key] = pm
    return pm


def invalidate_art() -> None:
    for c in (_icon_src_cache, _icon_px_cache, _accent_cache, _art_cache, _bg_cache):
        c.clear()


# ====================================================================== theming
_accent_cache: dict = {}


def dominant_hsv(im):
    """Most common *vivid* hue (histogram mode). Averaging hues is what produced the muddy colours."""
    try:
        raw = im.convert("RGBA").resize((48, 48)).tobytes()
        bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]   # weight, s*w, v*w, hue-offset*w
        for i in range(0, len(raw), 4):
            r, g_, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a < 180:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g_ / 255, b / 255)
            if s < 0.30 or v < 0.28:
                continue
            w = s * v
            k = min(23, int(h * 24))
            B = bins[k]
            B[0] += w
            B[1] += s * w
            B[2] += v * w
            B[3] += (h * 24 - k) * w
        scores = [bins[i][0] + 0.5 * (bins[(i - 1) % 24][0] + bins[(i + 1) % 24][0]) for i in range(24)]
        k = max(range(24), key=lambda i: scores[i])
        B = bins[k]
        if B[0] < 4.0:
            return None          # grey / black / white icon: no real colour to pick
        h = ((k + B[3] / B[0]) / 24) % 1.0
        return h, clamp(B[1] / B[0] * 1.08, 0.55, 1.0), 0.95
    except Exception:
        return None


def accent_hsv(g: dict):
    """Per-game accent: user override > cover art > icon > stable hashed hue."""
    ip = ICONS / f"{g['id']}.png"
    try:
        stamp = ip.stat().st_mtime
    except OSError:
        stamp = 0
    key = (g["id"], g.get("accent", ""), g.get("cover", ""), stamp)
    if key in _accent_cache:
        return _accent_cache[key]
    res = None
    custom = g.get("accent")
    if custom and QColor(custom).isValid():
        h, s, v, _ = QColor(custom).getHsvF()
        res = (max(h, 0.0), s, max(v, 0.6))
    if res is None and g.get("cover") and Path(g["cover"]).exists():
        try:
            from PIL import Image
            res = dominant_hsv(Image.open(g["cover"]))
        except Exception:
            res = None
    if res is None:
        im, _n = icon_source(g)
        if im is not None:
            res = dominant_hsv(im)
    if res is None:
        h = int(hashlib.md5(g["id"].encode()).hexdigest()[:6], 16) % 360 / 360.0
        res = (h, 0.72, 0.95)
    _accent_cache[key] = res
    return res


def theme(g: dict):
    h, s, v = accent_hsv(g)
    return hsv(h, s, v), hsv(h + 0.09, min(1.0, s + 0.05), clamp(v * 0.82 + 0.05, 0, 1))


_art_cache: dict = {}


def _art_cache_stamp(g: dict):
    try:
        return (ICONS / f"{g['id']}.png").stat().st_mtime
    except OSError:
        return 0


def card_art(g: dict) -> QPixmap:
    ip = ICONS / f"{g['id']}.png"
    try:
        stamp = ip.stat().st_mtime
    except OSError:
        stamp = 0
    key = (g["id"], g.get("cover", ""), g.get("accent", ""), g["name"][:1], stamp)
    if key in _art_cache:
        return _art_cache[key]
    D = 2
    pm = QPixmap(TW * D, TH * D)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                     | QPainter.RenderHint.TextAntialiasing)
    p.scale(D, D)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, TW, TH), ART_R, ART_R)
    p.setClipPath(clip)
    full = QRectF(0, 0, TW, TH)

    c1, c2 = theme(g)
    gr = QLinearGradient(0, 0, TW, TH)
    gr.setColorAt(0, c1)
    gr.setColorAt(1, c2.darker(300))
    p.fillRect(full, gr)
    hl = QRadialGradient(TW * 0.25, TH * 0.15, TW * 1.0)
    hl.setColorAt(0, QColor(255, 255, 255, 85))
    hl.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(full, hl)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 16))
    p.drawEllipse(QPointF(TW * 0.92, TH * 0.12), TW * 0.5, TW * 0.5)
    p.setBrush(QColor(0, 0, 0, 42))
    p.drawEllipse(QPointF(TW * 0.05, TH * 1.0), TW * 0.65, TW * 0.65)

    cover = g.get("cover")
    cpm = QPixmap(cover) if cover and Path(cover).exists() else QPixmap()
    cx, cy = TW / 2, TH * 0.43
    ipm = None if not cpm.isNull() else icon_pixmap(g, int(TW * 0.52 * D))

    if not cpm.isNull():
        sc = cpm.scaled(TW * D, TH * D, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(QRectF((TW - sc.width() / D) / 2, (TH - sc.height() / D) / 2, sc.width() / D, sc.height() / D),
                     sc, QRectF(sc.rect()))
    elif ipm is not None and not ipm.isNull():
        sw, sh = ipm.width() / D, ipm.height() / D
        glow = QRadialGradient(cx, cy, max(sw, sh) * 0.95)
        glow.setColorAt(0, QColor(255, 255, 255, 110))
        glow.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(full, glow)
        if max(sw, sh) < TW * 0.40:     # small source icon -> sit it on a glass plate so it doesn't look stranded
            plate = max(sw, sh) + 40
            p.setPen(QPen(QColor(255, 255, 255, 60), 1.5))
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawRoundedRect(QRectF(cx - plate / 2, cy - plate / 2, plate, plate), plate * 0.24, plate * 0.24)
            p.setPen(Qt.PenStyle.NoPen)
        p.drawPixmap(QRectF(cx - sw / 2, cy - sh / 2, sw, sh), ipm, QRectF(ipm.rect()))
    else:
        p.setFont(ui_font(TH * 0.40, QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(0, cy - TH * 0.28, TW, TH * 0.56), Qt.AlignmentFlag.AlignCenter,
                   (g["name"][:1] or "?").upper())

    scrim = QLinearGradient(0, TH * 0.58, 0, TH)
    scrim.setColorAt(0, QColor(0, 0, 0, 0))
    scrim.setColorAt(1, QColor(0, 0, 0, 175))
    p.fillRect(full, scrim)
    p.setClipping(False)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 50), 2))
    p.drawRoundedRect(QRectF(1, 1, TW - 2, TH - 2), ART_R - 1, ART_R - 1)
    p.end()
    while len(_art_cache) >= 80:                       # ~1.2 MB per card: cap it (oldest goes first)
        _art_cache.pop(next(iter(_art_cache)))
    _art_cache[key] = pm
    return pm


_bg_cache: dict = {}


def bg_pixmap(g: dict, w: int, h: int) -> QPixmap:
    """Heavily blurred card, low-res on purpose (cheap, and lets the camera drift)."""
    key = (g["id"], g.get("cover", ""), g.get("accent", ""), w, h, _art_cache_stamp(g))
    if key in _bg_cache:
        return _bg_cache[key]
    if len(_bg_cache) > 10:
        _bg_cache.clear()
    art = card_art(g)
    inner = art.copy(40, 40, art.width() - 80, art.height() - 80)
    smooth = Qt.TransformationMode.SmoothTransformation
    ign = Qt.AspectRatioMode.IgnoreAspectRatio
    mid = inner.scaled(14, 18, ign, smooth).scaled(70, 90, ign, smooth)
    bw, bh = w // 4 + 2, h // 4 + 2
    big = mid.scaled(bw, bh, Qt.AspectRatioMode.KeepAspectRatioByExpanding, smooth)
    out = QPixmap(bw, bh)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap((bw - big.width()) // 2, (bh - big.height()) // 2, big)
    p.end()
    _bg_cache[key] = out
    return out


# ====================================================================== gamepad (XInput)
class XGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", XGamepad)]


class Pad:
    """Tiny XInput poller. Does nothing on systems without XInput."""

    def __init__(self):
        self.fn = None
        self.idx = None
        self.next_scan = 0.0
        try:
            for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
                try:
                    self.fn = ctypes.WinDLL(name).XInputGetState
                    break
                except OSError:
                    continue
            if self.fn:
                self.fn.argtypes = [ctypes.c_uint, ctypes.POINTER(XState)]
                self.fn.restype = ctypes.c_uint
        except Exception:
            self.fn = None

    def poll(self, now):
        if not self.fn:
            return None
        st = XState()
        if self.idx is None:
            if now < self.next_scan:
                return None
            self.next_scan = now + 1.5
            for i in range(4):
                if self.fn(i, ctypes.byref(st)) == 0:
                    self.idx = i
                    break
            else:
                return None
        if self.fn(self.idx, ctypes.byref(st)) != 0:
            self.idx = None
            return None
        g = st.Gamepad
        return g.wButtons, g.sThumbLX, g.sThumbLY


# ====================================================================== game processes (no threads!)
class PopenProc:
    def __init__(self, popen):
        self.p = popen

    def poll(self):
        return self.p.poll()


IGNORE_EXES = {"conhost.exe", "werfault.exe", "crashpad_handler.exe", "crashreportclient.exe",
               "unitycrashhandler32.exe", "unitycrashhandler64.exe", "dxsetup.exe"}


class TreeProc:
    """Follows a game as a process tree, so launcher stubs (Steam, Epic, anti-cheat bootstrappers) don't end the session.
    - children/grandchildren of anything we track are adopted automatically
    - if the launched exe quits fast with code 0 (a hand-off), we look for fresh processes that live in the game's
      folder for a short grace window and adopt those
    The session ends when nothing we track is alive any more."""
    GRACE = 25.0

    def __init__(self, popen, game_dir, started):
        self.p = popen
        self.dir = os.path.normcase(os.path.abspath(game_dir)).rstrip("\\/") + os.sep
        self.started = started
        self.tracked = {}                 # pid -> create_time (guards against pid reuse)
        self.root_code = None
        self.root_gone = None
        self.adopted_other = False
        self.stub = False                 # hand-off we never managed to follow
        self.code = None
        self.next_scan = 0.0
        self.me = os.getpid()
        self._add(popen.pid, force=True)

    def _add(self, pid, force=False):
        try:
            pr = psutil.Process(pid)
            if not force and (pid == self.me or pr.name().lower() in IGNORE_EXES):
                return
            if pid not in self.tracked:
                self.tracked[pid] = pr.create_time()
                if pid != self.p.pid:
                    self.adopted_other = True
        except Exception:
            pass

    def _alive(self):
        n = 0
        for pid, ct in list(self.tracked.items()):
            try:
                pr = psutil.Process(pid)
                if pr.create_time() == ct and pr.status() != psutil.STATUS_ZOMBIE:
                    n += 1
                    continue
            except psutil.NoSuchProcess:
                pass
            except Exception:
                n += 1                     # access denied etc.: assume it's still running
                continue
            self.tracked.pop(pid, None)
        return n

    def _scan(self, hunt):
        for pid in list(self.tracked):
            try:
                for c in psutil.Process(pid).children(recursive=True):
                    self._add(c.pid)
            except Exception:
                pass
        if hunt:
            try:
                for pr in psutil.process_iter(["pid", "exe", "create_time"]):
                    info = pr.info
                    exe, ct = info.get("exe"), info.get("create_time") or 0
                    if exe and ct >= self.started - 1 and os.path.normcase(exe).startswith(self.dir):
                        self._add(info["pid"])
            except Exception:
                pass

    def poll(self):
        if self.code is not None:
            return self.code
        now = time.time()
        if self.root_code is None:
            self.root_code = self.p.poll()
            if self.root_code is not None:
                self.root_gone = now
        hunt = (self.root_code == 0 and self.root_gone - self.started < 10 and now - self.root_gone < self.GRACE)
        if now >= self.next_scan:
            self.next_scan = now + (0.7 if hunt else 2.0)
            self._scan(hunt)
        if self.root_code is None or self._alive():
            return None                    # (root can die before psutil ever saw it: never trust "nothing tracked")
        if hunt and not self.adopted_other:
            return None                    # launcher stub gone, real game not seen yet: keep looking
        self.stub = (self.root_code == 0 and self.root_gone is not None
                     and self.root_gone - self.started < 10 and not self.adopted_other)
        self.code = self.root_code if self.root_code is not None else 0
        return self.code


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


class ElevatedProc:
    """Game started through UAC ('runas'). Popen can't do that, so we hold the process handle ourselves."""

    def __init__(self, handle):
        self.h = handle
        self.code = None

    @staticmethod
    def start(path, args, cwd):
        try:
            k32, sh = ctypes.windll.kernel32, ctypes.windll.shell32
            info = SHELLEXECUTEINFOW()
            info.cbSize = ctypes.sizeof(info)
            info.fMask = 0x40 | 0x100        # NOCLOSEPROCESS | NOASYNC
            info.lpVerb, info.lpFile = "runas", path
            info.lpParameters = args or None
            info.lpDirectory = cwd
            info.nShow = 1
            if not sh.ShellExecuteExW(ctypes.byref(info)):
                return None                   # user said no on the UAC prompt
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k32.WaitForSingleObject.restype = ctypes.c_uint
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            return ElevatedProc(info.hProcess)
        except Exception:
            log("elevated start failed:\n" + traceback.format_exc())
            return None

    def poll(self):
        if self.code is not None:
            return self.code
        if not self.h:
            self.code = 0
            return 0
        try:
            k32 = ctypes.windll.kernel32
            if k32.WaitForSingleObject(self.h, 0) == 0x102:   # still running
                return None
            c = ctypes.c_uint(0)
            k32.GetExitCodeProcess(self.h, ctypes.byref(c))
            k32.CloseHandle(self.h)
            self.code = int(c.value)
        except Exception:
            self.code = 0
        return self.code


# ====================================================================== Spotify "now playing" (Windows media session)
class NowPlaying(threading.Thread):
    """Reads what Spotify is playing from Windows' own media session. No login, no API key, no Premium."""

    def __init__(self):
        super().__init__(daemon=True, name="nowplaying")
        self._lock = threading.Lock()
        self._state = None
        self.error = ""
        self.loop = None
        self.wake = None
        self.session = None
        self._quit = False
        self._last_err = 0.0

    def snapshot(self):
        with self._lock:
            return dict(self._state) if self._state else None

    def _set(self, st):
        with self._lock:
            self._state = st

    def run(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Mgr
        except Exception as ex:
            self.error = f"{type(ex).__name__}: {ex}"
            log("Spotify widget unavailable: " + self.error)
            return
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main(Mgr))
        except Exception:
            log("nowplaying loop died:\n" + traceback.format_exc())

    async def _main(self, Mgr):
        self.wake = asyncio.Event()
        mgr = None
        art_key, art, art_tries = None, None, 0
        while not self._quit:
            try:
                if mgr is None:
                    mgr = await Mgr.request_async()
                spot = None
                for s in mgr.get_sessions():
                    try:
                        if "spotify" in str(s.source_app_user_model_id or "").lower():
                            spot = s
                            break
                    except Exception:
                        continue
                self.session = spot
                if spot is None:
                    self._set(None)
                else:
                    info = await spot.try_get_media_properties_async()
                    title = (info.title or "").strip()
                    artist = (info.artist or "").strip()
                    album = (info.album_title or "").strip()
                    status = int(spot.get_playback_info().playback_status)   # 4 playing, 5 paused
                    if not title or status not in (4, 5):
                        self._set(None)
                    else:
                        tl = spot.get_timeline_properties()
                        dur = max(0.0, (tl.end_time - tl.start_time).total_seconds())
                        pos = max(0.0, tl.position.total_seconds())
                        if status == 4:
                            try:
                                lu = tl.last_updated_time
                                if lu.tzinfo is None:
                                    lu = lu.replace(tzinfo=timezone.utc)
                                el = (datetime.now(timezone.utc) - lu).total_seconds()
                                if 0 <= el < dur + 5:
                                    pos += el
                            except Exception:
                                pass
                        pos = min(pos, dur) if dur > 0 else 0.0
                        key = f"{artist}|{title}|{album}"
                        if key != art_key:
                            art_key, art, art_tries = key, None, 0
                        if art is None and art_tries < 4:
                            art_tries += 1
                            art = await self._read_thumb(info)
                        self._set({"title": title, "artist": artist, "album": album, "playing": status == 4, "key": key,
                                   "pos": pos, "dur": dur, "stamp": time.time(), "art": art,
                                   "art_sig": f"{key}|{len(art) if art else 0}"})
            except Exception:
                mgr = None
                if time.time() - self._last_err > 30:
                    self._last_err = time.time()
                    log("nowplaying poll error:\n" + traceback.format_exc())
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _read_thumb(self, info):
        try:
            ref = info.thumbnail
            if ref is None:
                return None
            from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 8_000_000:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            try:
                reader = DataReader.from_buffer(buf)
                data = bytearray(int(reader.unconsumed_buffer_length))
                reader.read_bytes(data)
                return bytes(data)
            except Exception:
                return bytes(memoryview(buf))
        except Exception:
            log("thumbnail read failed:\n" + traceback.format_exc())
            return None

    def command(self, what: str):
        """prev / toggle / next - safe to call from the UI thread."""
        if not self.loop or not self.session:
            return

        async def go():
            s = self.session
            try:
                if what == "toggle":
                    await s.try_toggle_play_pause_async()
                elif what == "next":
                    await s.try_skip_next_async()
                elif what == "prev":
                    await s.try_skip_previous_async()
            except Exception:
                log("media command failed:\n" + traceback.format_exc())
            if self.wake:
                self.wake.set()

        try:
            asyncio.run_coroutine_threadsafe(go(), self.loop)
        except Exception:
            pass


# ====================================================================== dialogs
class EditDialog(QDialog):
    def __init__(self, g, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit game")
        self.setMinimumWidth(500)
        self.accent = g.get("accent", "")
        self.name = QLineEdit(g["name"])
        self.args = QLineEdit(g.get("args", ""))
        self.args.setPlaceholderText("optional launch arguments")
        self.cover = QLineEdit(g.get("cover", ""))
        self.cover.setPlaceholderText("optional cover image (png / jpg)")
        browse = QPushButton("Browse")
        browse.clicked.connect(self.pick_cover)
        crow = QHBoxLayout()
        crow.addWidget(self.cover)
        crow.addWidget(browse)
        self.sw = QPushButton()
        self.sw.clicked.connect(self.pick_accent)
        auto = QPushButton("Auto")
        auto.clicked.connect(self.reset_accent)
        arow = QHBoxLayout()
        arow.addWidget(self.sw, 1)
        arow.addWidget(auto)
        self.admin = QCheckBox("Always run as administrator")
        self.admin.setChecked(bool(g.get("admin")))
        self.upd_sw()
        form = QFormLayout(self)
        form.setContentsMargins(22, 22, 22, 18)
        form.setSpacing(12)
        form.addRow("Name", self.name)
        form.addRow("Args", self.args)
        form.addRow("Cover", crow)
        form.addRow("Accent", arow)
        form.addRow("", self.admin)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def upd_sw(self):
        if self.accent:
            self.sw.setText(self.accent.upper())
            self.sw.setStyleSheet(f"background:{self.accent}; color:#fff; border-color:{self.accent};")
        else:
            self.sw.setText("Auto (from icon)")
            self.sw.setStyleSheet("")

    def pick_cover(self):
        f, _ = QFileDialog.getOpenFileName(self, "Cover image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if f:
            self.cover.setText(f)

    def pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent or "#5b6cff"), self, "Accent colour")
        if c.isValid():
            self.accent = c.name()
            self.upd_sw()

    def reset_accent(self):
        self.accent = ""
        self.upd_sw()


QSS = """
* { font-family: "Segoe UI Variable Display", "Segoe UI", "DejaVu Sans", sans-serif; font-size: 13px; }
QDialog, QMessageBox { background: #0d0e18; color: #eceeff; }
QLabel { color: #e9ebff; }
QCheckBox { color: #e9ebff; spacing: 8px; }
QLineEdit { background: #151726; color: #fff; border: 1px solid #2a2e4a; border-radius: 8px; padding: 8px 10px;
            selection-background-color: #5b6cff; }
QLineEdit:focus { border: 1px solid #7f8cff; }
QPushButton { background: #1a1d33; color: #fff; border: 1px solid #2f3358; border-radius: 10px; padding: 8px 18px; }
QPushButton:hover { background: #242850; border-color: #7f8cff; }
QPushButton:default { background: #5b6cff; border-color: #5b6cff; }
QMenu { background: #12131f; color: #eceeff; border: 1px solid #2a2d45; padding: 6px; }
QMenu::item { padding: 9px 30px 9px 16px; border-radius: 6px; }
QMenu::item:selected { background: #5b6cff; color: #fff; }
QMenu::item:disabled { color: #6b6f8a; }
QMenu::separator { height: 1px; background: #262944; margin: 6px 8px; }
"""


# ====================================================================== the hub
class Hub(QWidget):
    def __init__(self, ipc=None):
        super().__init__()
        self.setWindowTitle("EXE//HUB")
        self.resize(1280, 780)
        self.setMinimumSize(640, 400)
        self.windowed = "--windowed" in sys.argv          # debugging only; normal mode is borderless fullscreen
        if not self.windowed:
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        self.data = load()
        self.settings = self.data.setdefault("settings", {})
        self.view_style = self.settings.get("style", "carousel")
        if self.view_style not in ("carousel", "grid"):
            self.view_style = "carousel"
        self.ipc = ipc
        if ipc:
            ipc.newConnection.connect(self.on_ipc)

        self.running: dict = {}          # gid -> {"proc": ..., "start": ...}
        self.query = ""
        self.items: list = []
        self.sel_i = 0
        self.prev_sel = 0
        self.scroll_pos = 0.0
        self.gscroll = 0.0
        self.gtarget = 0
        self.t = 0.0
        self.intro = 0.0
        self.info_t = 1.0
        self.bg_t = 1.0
        self.bg_prev = None
        self.bg_cur = None
        self.acc1 = [0.36, 0.42, 1.0]
        self.acc2 = [0.75, 0.35, 1.0]
        self.tgt1 = list(self.acc1)
        self.tgt2 = list(self.acc2)
        self.hit: dict = {}
        self.hover = None
        self.toast = None
        self.dropping = False
        self.lock_until = 0.0
        self.sel_abs = 0
        self.dlg = None
        self.dlg_t = 0.0
        self.black = 0.0
        self.launching = None
        self._quitting = False
        self._exited = False
        self.outro = None                    # None, or progress 0..1 of the quit animation
        skip_intro = ("--no-intro" in sys.argv) or bool(os.environ.get("EXEHUB_NO_INTRO"))
        self.boot_t = 1.0 if skip_intro else 0.0
        rng = random.Random(7)
        self.boot_dots = [(rng.uniform(0, 2 * math.pi), rng.uniform(280, 900), rng.uniform(1.4, 3.6),
                           rng.random(), rng.random()) for _ in range(54)]
        self.logo_big = QIcon(resource("exehub.ico")).pixmap(256, 256)
        self.last_key = 0.0
        self._wheel = 0
        self._modal = False
        self._iv = 16
        self._styled = False
        self._run_chk = 0.0
        self._err_t = 0.0
        self._job_t = 0.0
        self.chrome = 1.0
        self.idle = 0.0
        self._blank = False
        self.pad = Pad()
        self.pad_on = False
        self._pad_prev = 0
        self._rep: dict = {}
        self.logo = QIcon(resource("exehub.ico")).pixmap(72, 72)
        self.bok = [[random.random(), random.random(), random.uniform(20, 85), random.uniform(0.008, 0.03),
                     random.uniform(0.3, 1.0), random.random()] for _ in range(24)]

        # Spotify card state + animation state
        self.np = NowPlaying()
        self.np_fake = None              # tests inject a fake state here
        self.sp = None
        self.sp_in = 0.0                 # slide-in progress 0..1
        self.sp_alpha = 0.0
        self.sp_art = None
        self.sp_art_old = None
        self.sp_sig = None
        self.sp_key = None
        self.sp_cur_text = None
        self.sp_old = None
        self.sp_ct = 1.0                 # text transition progress
        self.sp_at = 1.0                 # art crossfade progress
        self.sp_hover = 0.0
        self.sp_press = {}
        self.sp_play_t = 1.0
        self.sp_pos_disp = 0.0
        self.sp_acc = None
        self.sp_acc_cur = rgb_of(SPOTIFY_GREEN)
        self._art_scaled = {}
        self._sp_warned = False

        # system tray: the way back to the hub while a game is running
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(resource("exehub.ico")), self)
            self.tray_menu = QMenu()
            self.tray_menu.addAction("Show EXE//HUB", self.enter_fullscreen)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction("Quit EXE//HUB", self.quit_now)
            self.tray.setContextMenu(self.tray_menu)
            self.tray.setToolTip("EXE//HUB")
            self.tray.activated.connect(
                lambda r: self.enter_fullscreen() if r in (QSystemTrayIcon.ActivationReason.Trigger,
                                                           QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.show()

        # icons get re-extracted once after an upgrade (one per tick, never blocks the UI)
        self._icon_jobs = []
        if self.settings.get("icons_v", 0) < ICON_VERSION:
            self._icon_jobs = [g["id"] for g in self.data["games"]]
            if not self._icon_jobs:
                self.settings["icons_v"] = ICON_VERSION

        self.rebuild()
        self.scroll_pos = float(self.sel_i)
        self.bg_prev = self.bg_cur
        self.snap_colors()
        if LOAD_NOTE:
            self.say(LOAD_NOTE, 8)

        self.np.start()
        self.last = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ------------------------------------------------------------- data / selection
    def game(self, gid):
        return next((g for g in self.data["games"] if g["id"] == gid), None)

    def current_item(self):
        return self.items[self.sel_i] if 0 <= self.sel_i < len(self.items) else None

    def loop_mode(self):
        """Carousel wraps around (so the sides are never empty) once there are 3+ tiles."""
        return self.view_style == "carousel" and len(self.items) >= 3

    def rebuild(self, select_id=None):
        old_n = len(self.items)
        cur = select_id or (self.current_item() or {}).get("id")
        q = self.query.casefold()
        games = [g for g in self.data["games"] if q in g["name"].casefold()]
        games.sort(key=lambda g: (-max(g.get("last_played", 0), g.get("added", 0)), g["name"].casefold()))
        self.items = games + [None]
        idx = 0
        for i, g in enumerate(games):
            if g["id"] == cur:
                idx = i
        self.set_sel(idx, force=True)
        if len(self.items) != old_n or not self.loop_mode():
            self.sel_abs = idx
            self.scroll_pos = float(self.sel_abs)

    def _sel_changed(self):
        self.info_t = 0.0
        self.bg_prev = self.bg_cur
        self.bg_cur = self.items[self.sel_i]
        self.bg_t = 0.0
        self.update_targets()

    def set_sel(self, i, force=False):
        n = len(self.items)
        i = int(clamp(i, 0, n - 1))
        old = self.sel_i
        if i != old:
            self.prev_sel = old
        if self.loop_mode():
            d = (i - old) % n
            if d > n / 2:
                d -= n
            self.sel_abs += d
        else:
            self.sel_abs = i
        self.sel_i = i
        if force or i != old:
            self._sel_changed()

    def move(self, d):
        n = len(self.items)
        if self.loop_mode():
            old = self.sel_i
            self.sel_abs += d
            self.sel_i = self.sel_abs % n
            if self.sel_i != old:
                self.prev_sel = old
                self._sel_changed()
        else:
            self.set_sel(self.sel_i + d)

    def cols(self):
        return self.grid_metrics()["cols"] if self.view_style == "grid" else 1

    def update_targets(self):
        it = self.current_item()
        if it is None:
            c1, c2 = hsv(0.62, 0.62, 1.0), hsv(0.78, 0.58, 0.9)
        else:
            c1, c2 = theme(it)
        self.tgt1, self.tgt2 = rgb_of(c1), rgb_of(c2)

    def snap_colors(self):
        self.update_targets()
        self.acc1, self.acc2 = list(self.tgt1), list(self.tgt2)

    def say(self, text, secs=3.5):
        self.toast = (text, time.perf_counter() + secs)

    def locked(self):
        return time.perf_counter() < self.lock_until

    def booting(self):
        return self.boot_t < 0.9

    def skip_boot(self):
        if self.boot_t < 0.78:
            self.boot_t = 0.78           # jump straight to the reveal instead of hard-cutting

    def input_blocked(self):
        """Intro/outro swallow input (a key press during the intro just skips ahead)."""
        if self.outro is not None:
            return True
        if self.booting():
            self.skip_boot()
            return True
        return False

    # ------------------------------------------------------------- animation loop
    def tick(self):
        try:
            self._tick()
        except Exception:
            if time.perf_counter() - self._err_t > 5:
                self._err_t = time.perf_counter()
                log("tick error:\n" + traceback.format_exc())

    def _tick(self):
        now = time.perf_counter()
        dt = min(now - self.last, 0.05)
        self.last = now
        self.check_running(now)
        self.handle_pad(now)
        if self.isMinimized() or not self.isVisible():
            if self._iv != 250:
                self._iv = 250
                self.timer.setInterval(250)
            return
        active = self.isActiveWindow()
        iv = 16 if active else (250 if self.running else 40)
        if iv != self._iv:
            self._iv = iv
            self.timer.setInterval(iv)

        self.t += dt
        self.idle += dt
        immersive = self.idle > 4.0 and not self._modal and not self.dropping and not self.dlg
        self.chrome += ((0.0 if immersive else 1.0) - self.chrome) * (1 - math.exp(-dt * 5))
        blank = immersive and self.chrome < 0.05
        if blank != self._blank:
            self._blank = blank
            self.setCursor(Qt.CursorShape.BlankCursor if blank else Qt.CursorShape.ArrowCursor)

        k = 1 - math.exp(-dt * 10)
        self.scroll_pos += (self.sel_abs - self.scroll_pos) * k
        if abs(self.sel_abs - self.scroll_pos) < 0.0008:
            self.scroll_pos = float(self.sel_abs)
        if self.view_style == "grid":
            gm = self.grid_metrics()
            r = self.sel_i // gm["cols"]
            if r < self.gtarget:
                self.gtarget = r
            elif r > self.gtarget + gm["vis"] - 1:
                self.gtarget = r - gm["vis"] + 1
            rows = (len(self.items) + gm["cols"] - 1) // gm["cols"]
            self.gtarget = int(clamp(self.gtarget, 0, max(0, rows - gm["vis"])))
            self.gscroll += (self.gtarget - self.gscroll) * k
            if abs(self.gtarget - self.gscroll) < 0.001:
                self.gscroll = float(self.gtarget)
        for a, b in ((self.acc1, self.tgt1), (self.acc2, self.tgt2)):
            for j in range(3):
                a[j] += (b[j] - a[j]) * k
        if self.boot_t < 1.0:
            self.boot_t = min(1.0, self.boot_t + dt / BOOT_LEN)
        if self.boot_t >= 0.80:                       # tiles start flying in as the iris opens
            self.intro = min(1.0, self.intro + dt / 1.0)
        if self.outro is not None:
            self.outro = min(1.0, self.outro + dt / OUTRO_LEN)
            if self.outro >= 1.0:
                self.final_exit()
        self.info_t = min(1.0, self.info_t + dt / 0.28)
        self.bg_t = min(1.0, self.bg_t + dt / 0.6)
        for b in self.bok:
            b[1] -= b[3] * dt
            if b[1] < -0.1:
                b[0], b[1] = random.random(), 1.1
        if self.toast and now > self.toast[1]:
            self.toast = None
        if self.query and time.perf_counter() - self.last_key > 12:     # stale search can't hide your games
            self.query = ""
            self.rebuild()
        self.dlg_t = min(1.0, self.dlg_t + dt / 0.22)
        self.black = max(0.0, self.black - dt / 0.7)
        if self.launching:
            self.launching["t"] += dt / 0.55
            if self.launching["t"] >= 1.0:
                gid = self.launching["gid"]
                self.launching = None
                if gid in self.running:
                    self.hide()            # like a game: the hub just goes away while the game runs
        self.update_spotify(dt)
        self.run_icon_job(now, active)
        if not (self.running and not active):      # don't waste GPU/CPU while a game is in front
            self.update()

    # ------------------------------------------------------------- running games
    def check_running(self, now):
        if now - self._run_chk < 0.5 or not self.running:
            return
        self._run_chk = now
        for gid, r in list(self.running.items()):
            code = r["proc"].poll()
            if code is not None:
                self.finish_game(gid, code)

    def finish_game(self, gid, code):
        r = self.running.pop(gid, None)
        g = self.game(gid)
        if r is None or g is None:
            return
        life = time.time() - r["start"]
        lost = bool(getattr(r["proc"], "stub", False))      # hand-off we couldn't follow: waiting around isn't playtime
        g["seconds"] = g.get("seconds", 0) + (0 if lost else int(life))
        g["last_played"] = time.time()
        save(self.data)
        self.lock_until = time.perf_counter() + 1.5          # keys you mashed in-game must not hit the hub
        stub = lost or (life < 6 and code == 0)             # launcher stub that handed off to the real game
        hidden = not self.isVisible()
        self.launching = None
        if stub and hidden:
            self.notify(f'{g["name"]} handed off to its own launcher - click the tray icon to bring the hub back')
        else:
            if hidden:
                self.black = 1.0                              # fade back in from black
                self.intro = 0.2
                self.enter_fullscreen()
            if stub:
                self.say(f'{g["name"]} handed off to its own launcher - playtime can\'t be tracked', 6)
            elif code not in (0, None) and life < 8:
                self.say(f'{g["name"]} closed right away (exit code {code})', 6)
        self.rebuild(select_id=gid)

    def notify(self, text):
        if self.tray:
            self.tray.showMessage("EXE//HUB", text, QSystemTrayIcon.MessageIcon.Information, 6000)
        log("notify: " + text)

    def enter_fullscreen(self):
        """Borderless, covers the whole monitor (taskbar included). This is the only window mode."""
        if self.windowed:
            self.show()
            self.raise_()
            self.activateWindow()
            return
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        try:
            self.setScreen(scr)
        except Exception:
            pass
        self.setGeometry(scr.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        try:
            force_foreground(int(self.winId()))
        except Exception:
            pass

    def toggle_fullscreen(self):
        """F11 / Start: borderless fullscreen <-> normal window."""
        self.windowed = not self.windowed
        flags = Qt.WindowType.Window if self.windowed else (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowFlags(flags)               # recreates the native window: re-apply the dark title bar on show
        self._styled = False
        if self.windowed:
            self.showNormal()
            self.resize(1280, 780)
            self.raise_()
            self.activateWindow()
        else:
            self.enter_fullscreen()
        self.say("Windowed" if self.windowed else "Fullscreen", 1.5)

    def bring_front(self):
        self.enter_fullscreen()

    def on_ipc(self):
        s = self.ipc.nextPendingConnection()
        if s:
            s.close()
        self.bring_front()

    def tile_center(self):
        if self.view_style == "grid":
            return self.grid_rect(self.sel_i, self.grid_metrics()).center()
        return self.tile_geom(self.sel_abs, self.metrics())[0].center()

    def launch(self, g, admin=False):
        gid = g["id"]
        if gid in self.running:
            return
        if not os.path.exists(g["path"]):
            self.say(f'File not found: {g["path"]}', 6)
            return
        cwd = str(Path(g["path"]).parent)
        admin = admin or bool(g.get("admin"))
        proc = None
        if not admin:
            try:
                cmdline = subprocess.list2cmdline([g["path"]])
                if g.get("args", "").strip():
                    cmdline += " " + g["args"].strip()
                popen = subprocess.Popen(
                    cmdline, cwd=cwd,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = TreeProc(popen, cwd, time.time()) if psutil else PopenProc(popen)
            except OSError as ex:
                if getattr(ex, "winerror", None) == 740:      # "requires elevation"
                    admin = True
                    g["admin"] = True
                    save(self.data)
                else:
                    log(f"launch failed for {g['name']}: {ex}")
                    self.say(f'Couldn\'t start {g["name"]}: {ex}', 7)
                    return
        if admin:
            proc = ElevatedProc.start(g["path"], g.get("args", ""), cwd)
            if proc is None:
                self.say("Admin launch cancelled or failed")
                return
        self.running[gid] = {"proc": proc, "start": time.time()}
        self.launching = {"t": 0.0, "gid": gid, "name": g["name"], "c": self.tile_center(), "color": theme(g)[0]}
        self.lock_until = time.perf_counter() + 0.7

    def run_icon_job(self, now, active):
        if not self._icon_jobs or self.running or not active or now - self._job_t < 0.2:
            return
        self._job_t = now
        gid = self._icon_jobs.pop()
        g = self.game(gid)
        if g and os.path.exists(g["path"]):
            self.refresh_icon(g)
        if not self._icon_jobs:
            self.settings["icons_v"] = ICON_VERSION
            save(self.data)
            self.say("Icons refreshed")

    def refresh_icon(self, g):
        out = ICONS / f"{g['id']}_new.png"
        cur = ICONS / f"{g['id']}.png"
        try:
            if extract_icon(g["path"], out):
                from PIL import Image
                new_area = Image.open(out).size
                old_area = Image.open(cur).size if cur.exists() else (0, 0)
                if new_area[0] * new_area[1] >= old_area[0] * old_area[1]:
                    os.replace(out, cur)
            out.unlink(missing_ok=True)
        except Exception:
            log("refresh_icon failed:\n" + traceback.format_exc())
        invalidate_art()

    # ------------------------------------------------------------- spotify card
    def update_spotify(self, dt):
        snap = self.np_fake if self.np_fake is not None else self.np.snapshot()

        def ek(rate):
            return 1 - math.exp(-dt * rate)

        appearing = self.sp_in < 0.05
        self.sp_in = min(1.0, self.sp_in + dt / 0.6) if snap else max(0.0, self.sp_in - dt / 0.35)
        self.sp_alpha = self.sp_in
        hov = self.hover in ("sp", "sp_prev", "sp_toggle", "sp_next")
        self.sp_hover += ((1.0 if hov else 0.0) - self.sp_hover) * ek(10)
        for n in list(self.sp_press):
            self.sp_press[n] -= dt / 0.28
            if self.sp_press[n] <= 0:
                del self.sp_press[n]
        self.sp_ct = min(1.0, self.sp_ct + dt / 0.55)
        self.sp_at = min(1.0, self.sp_at + dt / 0.5)
        tgt = rgb_of(self.sp_acc or SPOTIFY_GREEN)
        for j in range(3):
            self.sp_acc_cur[j] += (tgt[j] - self.sp_acc_cur[j]) * ek(6)
        if self.np.error and not self._sp_warned and self.np_fake is None:
            self._sp_warned = True
            log("Spotify card disabled: " + self.np.error)
        if not snap:
            return

        self.sp = snap
        self.sp_play_t += ((1.0 if snap["playing"] else 0.0) - self.sp_play_t) * ek(12)
        key = snap.get("key") or snap.get("art_sig")
        key_changed = key != self.sp_key
        if key_changed:
            if self.sp_key is not None and self.sp_cur_text and self.sp_in > 0.5:
                self.sp_old = self.sp_cur_text        # old title/artist slide out, new ones slide in
                self.sp_art_old = self.sp_art
                self.sp_ct = 0.0
                self.sp_at = 0.0
                self.sp_pos_disp = 0.0
            self.sp_key = key
            self.sp_cur_text = (snap["title"], snap["artist"])
        if snap.get("art_sig") != self.sp_sig:
            self.sp_sig = snap.get("art_sig")
            if not key_changed and self.sp_art is not None and self.sp_in > 0.5:
                self.sp_art_old = self.sp_art         # cover arrived late: crossfade it in
                self.sp_at = 0.0
            pm = QPixmap()
            data = snap.get("art")
            self.sp_art, self.sp_acc = None, None
            if data and pm.loadFromData(data) and not pm.isNull():
                self.sp_art = pm
                try:
                    from PIL import Image
                    d = dominant_hsv(Image.open(io.BytesIO(data)))
                    if d:
                        self.sp_acc = hsv(d[0], d[1], d[2])
                except Exception:
                    pass
        pos = snap["pos"] + ((time.time() - snap["stamp"]) if snap["playing"] else 0.0)
        if snap["dur"] > 0:
            pos = min(pos, snap["dur"])
        if appearing:
            self.sp_pos_disp = pos
        else:
            self.sp_pos_disp += (pos - self.sp_pos_disp) * ek(10)       # seeks glide instead of jumping

    # ------------------------------------------------------------- gamepad
    def handle_pad(self, now):
        s = self.pad.poll(now)
        self.pad_on = s is not None
        if s is None:
            self._pad_prev = 0
            self._rep.clear()
            return
        b, lx, ly = s
        if not self.isActiveWindow() or self._modal or self.isMinimized() or self.locked():
            self._pad_prev = b
            self._rep.clear()
            return
        pressed = b & ~self._pad_prev
        self._pad_prev = b
        if pressed and (self.outro is not None or self.booting()):
            self.input_blocked()
            return
        if self.outro is not None or self.booting():
            return
        if pressed or abs(lx) > 18000 or abs(ly) > 18000 or (b & 0x000F):
            self.idle = 0.0
        if self.dlg:
            if pressed & 0x000C:
                self.dlg["sel"] ^= 1
            if pressed & 0x1000:
                self.dlg_close(self.dlg["sel"] == 1)
            elif pressed & 0x2000:
                self.dlg_close(False)
            return
        dirs = {"l": bool(b & 0x0004) or lx < -18000, "r": bool(b & 0x0008) or lx > 18000,
                "u": bool(b & 0x0001) or ly > 18000, "d": bool(b & 0x0002) or ly < -18000}
        for name, down in dirs.items():
            if down:
                t0 = self._rep.get(name)
                if t0 is None or now >= t0:
                    self._rep[name] = now + (0.38 if t0 is None else 0.11)
                    if name == "l":
                        self.move(-1)
                    elif name == "r":
                        self.move(1)
                    elif self.view_style == "grid":
                        self.move(-self.cols() if name == "u" else self.cols())
            else:
                self._rep.pop(name, None)
        if pressed & 0x1000:
            self.activate()
        if pressed & 0x8000:
            self.options_menu()
        if pressed & 0x4000:
            self.add_dialog()
        if pressed & 0x2000:
            self.back()
        if pressed & 0x0010:
            self.toggle_fullscreen()
        if pressed & 0x0020:
            self.toggle_style()
        if pressed & 0x0100:
            self.move(-5)
        if pressed & 0x0200:
            self.move(5)

    # ------------------------------------------------------------- actions
    @contextmanager
    def modal(self):
        self._modal = True
        try:
            yield
        finally:
            self._modal = False

    def back(self):
        if self.dlg:
            self.dlg_close(False)
        elif self.query:
            self.query = ""
            self.rebuild()

    # ---- in-canvas confirm dialog
    def ask(self, title, text, yes_label, fn, danger=False):
        self.dlg = {"title": title, "text": text, "yes": yes_label, "fn": fn, "danger": danger, "sel": 0}
        self.dlg_t = 0.0

    def dlg_close(self, yes):
        d, self.dlg = self.dlg, None
        self.hit.pop("dlg_yes", None)
        self.hit.pop("dlg_no", None)
        if yes and d:
            try:
                d["fn"]()
            except Exception:
                log("dialog action failed:\n" + traceback.format_exc())

    def dlg_key(self, k):
        K = Qt.Key
        if k in (K.Key_Left, K.Key_Right, K.Key_Tab, K.Key_Up, K.Key_Down):
            self.dlg["sel"] ^= 1
        elif k in (K.Key_Return, K.Key_Enter, K.Key_Space):
            self.dlg_close(self.dlg["sel"] == 1)
        elif k == K.Key_Escape:
            self.dlg_close(False)

    def ask_quit(self):
        txt = ("A game is still running - its playtime so far will be saved, but the hub can't track it after you quit."
               if self.running else "Close EXE//HUB completely?")
        self.ask("Quit EXE//HUB", txt, "Quit", self.quit_now, danger=True)

    def shutdown_save(self):
        now = time.time()
        for gid, r in self.running.items():          # credit what's been played so far (start resets: safe to call twice)
            g = self.game(gid)
            if g is not None:
                g["seconds"] = g.get("seconds", 0) + int(now - r["start"])
                g["last_played"] = now
            r["start"] = now
        save(self.data)
        self.np._quit = True

    def quit_now(self):
        """Really quit. closeEvent normally vetoes closing (Alt+F4 protection), and Qt's quit() asks every window
        first - so we flag that this is intentional and use exit(), which can't be vetoed."""
        if self._quitting:
            return
        self._quitting = True
        self.dlg = None
        self.launching = None
        self.shutdown_save()
        if self.isVisible() and not self.isMinimized():
            self.outro = 0.0                                   # play the power-off animation, then exit
            QTimer.singleShot(int(OUTRO_LEN * 1000) + 1500, self.final_exit)   # safety net
        else:
            self.final_exit()                                  # nothing on screen (tray quit): leave at once

    def final_exit(self):
        if self._exited:
            return
        self._exited = True
        if self.tray:
            self.tray.hide()
        QApplication.instance().exit(0)

    def toggle_style(self):
        self.view_style = "grid" if self.view_style == "carousel" else "carousel"
        self.settings["style"] = self.view_style
        save(self.data)
        self.sel_abs = self.sel_i
        self.scroll_pos = float(self.sel_abs)
        self.info_t = 0.0
        self.gscroll = self.gtarget = 0
        self.say("Style: Grid" if self.view_style == "grid" else "Style: Carousel", 1.8)

    def activate(self):
        it = self.current_item()
        if it is None:
            self.add_dialog()
        elif it["id"] not in self.running:
            self.launch(it)

    def add_dialog(self):
        with self.modal():
            files, _ = QFileDialog.getOpenFileNames(
                self, "Pick game executables", str(Path.home()), "Executables (*.exe *.EXE);;All files (*)")
        self.add_paths(files)

    def add_paths(self, paths):
        added, dupes = [], 0
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.isfile(p) or not p.lower().endswith(".exe"):
                continue
            if any(g["path"] == p for g in self.data["games"]):
                dupes += 1
                continue
            gid = uuid.uuid4().hex[:10]
            extract_icon(p, ICONS / f"{gid}.png")
            self.data["games"].append({
                "id": gid, "name": prettify(Path(p).stem), "path": p, "args": "", "cover": "",
                "last_played": 0, "seconds": 0, "added": time.time()})
            added.append(gid)
        if added:
            save(self.data)
            self.query = ""
            self.rebuild(select_id=added[-1])
            self.say(f"Added {len(added)} game{'s' if len(added) != 1 else ''}")
        elif dupes:
            self.say("Already in your library")

    def edit_game(self, g):
        with self.modal():
            d = EditDialog(g, self)
            ok = d.exec()
        if ok:
            g["name"] = d.name.text().strip() or g["name"]
            g["args"] = d.args.text().strip()
            g["cover"] = d.cover.text().strip()
            g["accent"] = d.accent
            g["admin"] = d.admin.isChecked()
            save(self.data)
            self.rebuild(select_id=g["id"])
            self.update_targets()

    def remove_game(self, g):
        def do():
            self.data["games"] = [x for x in self.data["games"] if x["id"] != g["id"]]
            (ICONS / f"{g['id']}.png").unlink(missing_ok=True)
            save(self.data, allow_empty=True)
            i = self.sel_i
            self.rebuild()
            self.set_sel(i)
        self.ask("Remove game", f'Remove "{g["name"]}" from the hub?\nThe game files stay where they are.',
                 "Remove", do, danger=True)

    def options_menu(self, global_pos=None):
        g = self.current_item()
        if g is None:
            return
        m = QMenu(self)
        a_play = m.addAction("Play")
        a_admin = m.addAction("Run as administrator")
        a_stop = m.addAction("Mark as finished") if g["id"] in self.running else None
        m.addSeparator()
        a_edit = m.addAction("Edit...")
        a_icon = m.addAction("Refresh icon")
        a_open = m.addAction("Open file location")
        m.addSeparator()
        a_rm = m.addAction("Remove from hub")
        if g["id"] in self.running:
            a_play.setEnabled(False)
            a_admin.setEnabled(False)
        if global_pos is None:
            r = self.hit.get("opt") or QRectF(self.width() / 2, self.height() / 2, 0, 0)
            global_pos = self.mapToGlobal(QPointF(r.center().x(), r.bottom() + 6).toPoint())
        with self.modal():
            act = m.exec(global_pos)
        if act == a_play:
            self.launch(g)
        elif act == a_admin:
            self.launch(g, admin=True)
        elif a_stop is not None and act == a_stop:
            self.finish_game(g["id"], 0)
        elif act == a_edit:
            self.edit_game(g)
        elif act == a_icon:
            self.refresh_icon(g)
            self.say("Icon refreshed")
        elif act == a_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(g["path"]).parent)))
        elif act == a_rm:
            self.remove_game(g)

    # ------------------------------------------------------------- input events
    def changeEvent(self, e):
        if e.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.lock_until = time.perf_counter() + 0.7       # focus just came back: swallow stray input
        if (e.type() == QEvent.Type.WindowStateChange and self.isMinimized()
                and not self.windowed and not self.running):
            QTimer.singleShot(0, self.enter_fullscreen)       # no minimizing: it snaps straight back
        super().changeEvent(e)

    def keyPressEvent(self, e):
        self.idle = 0.0
        if self.input_blocked() or self.locked():
            return
        self.last_key = time.perf_counter()
        k, mod = e.key(), e.modifiers()
        K = Qt.Key
        if self.dlg:
            self.dlg_key(k)
            return
        ctrl = bool(mod & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier))
        cols = self.cols()
        if k == K.Key_Left:
            self.move(-1)
        elif k == K.Key_Right:
            self.move(1)
        elif k == K.Key_Up:
            self.move(-cols) if cols > 1 else None
        elif k == K.Key_Down:
            self.move(cols) if cols > 1 else None
        elif k == K.Key_PageUp:
            self.move(-5 if cols == 1 else -2 * cols)
        elif k == K.Key_PageDown:
            self.move(5 if cols == 1 else 2 * cols)
        elif k == K.Key_Home:
            self.set_sel(0)
        elif k == K.Key_End:
            self.set_sel(len(self.items) - 1)
        elif k == K.Key_Tab:
            self.toggle_style()
        elif k in (K.Key_Return, K.Key_Enter):
            self.activate()
        elif k == K.Key_Delete:
            g = self.current_item()
            if g:
                self.remove_game(g)
        elif k == K.Key_F2:
            g = self.current_item()
            if g:
                self.edit_game(g)
        elif k == K.Key_Escape:
            self.back()
        elif k == K.Key_F11:
            self.toggle_fullscreen()
        elif k == K.Key_Menu:
            self.options_menu()
        elif k == K.Key_O and ctrl:
            self.add_dialog()
        elif k == K.Key_Backspace:
            if self.query:
                self.query = self.query[:-1]
                self.rebuild()
        elif k == K.Key_Space and not self.query:
            self.activate()
        elif not ctrl and e.text() and e.text().isprintable():
            self.query += e.text()
            self.rebuild()
            self.set_sel(0)
        else:
            super().keyPressEvent(e)

    def tile_at(self, pos):
        if self.view_style == "grid":
            gm = self.grid_metrics()
            for i in range(len(self.items)):
                r = self.grid_rect(i, gm)
                if gm["top"] - 6 <= pos.y() <= gm["bottom"] + 6 and r.adjusted(-4, -4, 4, 4).contains(pos):
                    return i
            return None
        m = self.metrics()
        n = len(self.items)
        for k in sorted(self.carousel_ks(m), key=lambda k: abs(k - self.scroll_pos)):
            rect, _s, _n = self.tile_geom(k, m)
            if rect.adjusted(-6, -6, 6, 6).contains(pos):
                return k % n
        return None

    def mousePressEvent(self, e):
        pos = e.position()
        self.idle = 0.0
        if self.input_blocked() or self.locked():
            return
        if self.dlg:
            if e.button() == Qt.MouseButton.LeftButton:
                for name, yes in (("dlg_yes", True), ("dlg_no", False)):
                    r = self.hit.get(name)
                    if r and r.contains(pos):
                        self.dlg_close(yes)
                        break
            return
        if e.button() == Qt.MouseButton.LeftButton:
            def media(what):
                self.sp_press[{"prev": "sp_prev", "toggle": "sp_toggle", "next": "sp_next"}[what]] = 1.0
                self.np.command(what)
            for name, fn in (("power", self.ask_quit), ("style", self.toggle_style),
                             ("sp_prev", lambda: media("prev")), ("sp_toggle", lambda: media("toggle")),
                             ("sp_next", lambda: media("next")),
                             ("play", self.activate), ("opt", self.options_menu)):
                r = self.hit.get(name)
                if r and r.contains(pos):
                    fn()
                    return
            i = self.tile_at(pos)
            if i is not None:
                self.set_sel(i)
        elif e.button() == Qt.MouseButton.RightButton:
            i = self.tile_at(pos)
            if i is not None:
                self.set_sel(i)
                self.options_menu(e.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.locked():
            return
        pos = e.position()
        i = self.tile_at(pos)
        if i is not None and i == self.sel_i:
            self.activate()


    def mouseMoveEvent(self, e):
        pos = e.position()
        self.idle = 0.0
        if self.dlg:
            for name, sel in (("dlg_no", 0), ("dlg_yes", 1)):
                r = self.hit.get(name)
                if r and r.contains(pos):
                    self.dlg["sel"] = sel
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        h = None
        for name in ("power", "style", "sp_prev", "sp_toggle", "sp_next", "sp", "play", "opt"):
            r = self.hit.get(name)
            if r and r.contains(pos):
                h = name
                break
        self.hover = h
        over = h or self.tile_at(pos) is not None
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, e):
        self.idle = 0.0
        if self.input_blocked() or self.locked() or self.dlg:
            return
        d = e.angleDelta()
        self._wheel += d.y() or d.x()
        step = self.cols()
        while abs(self._wheel) >= 120:
            self.move(-step if self._wheel > 0 else step)
            self._wheel -= 120 if self._wheel > 0 else -120

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            self.dropping = True
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.dropping = False

    def dropEvent(self, e):
        self.dropping = False
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def closeEvent(self, e):
        if self._quitting:
            e.ignore() if self.outro is not None else e.accept()     # outro is playing: it will exit by itself
            return
        if QApplication.instance().isSavingSession():                # Windows is shutting down: never block it
            self.shutdown_save()
            e.accept()
            return
        e.ignore()
        if self.isVisible() and not self.dlg:
            self.boot_t = 1.0
            self.ask_quit()

    def showEvent(self, e):
        super().showEvent(e)
        if self._styled:
            return
        self._styled = True
        try:  # dark title bar when windowed (Windows 10 2004+ / 11)
            dwm = ctypes.windll.dwmapi
            hwnd = ctypes.c_void_p(int(self.winId()))
            one = ctypes.c_int(1)
            for attr in (20, 19):
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(one), 4)
            bar = ctypes.c_int(0x000F0807)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bar), 4)
        except Exception:
            pass

    # ------------------------------------------------------------- layout
    def metrics(self):
        w, h = self.width(), self.height()
        th = clamp(h * 0.44, 240, 460)
        tw = th * 0.78
        ts = clamp(h * 0.056, 30, 50)
        block = th + 30 + ts * 1.4 + 2 + 24 + 22 + 52
        top_safe, bottom_safe = 84, 70
        y0 = top_safe + max(0.0, (h - top_safe - bottom_safe - block) / 2)
        return w, h, tw, th, y0 + th / 2

    def tile_geom(self, i, m):
        w, h, tw, th, cy = m
        d = i - self.scroll_pos
        ad = abs(d)
        near = 1 - min(ad, 1.0)
        s = 0.80 + 0.20 * near
        step = tw * 0.80 + 22
        off = d * step + (1 if d > 0 else -1) * 0.10 * tw * min(ad, 1.0)
        cx = w / 2 + off
        return QRectF(cx - tw * s / 2, cy - th * s / 2, tw * s, th * s), s, near

    def grid_metrics(self):
        w, h = self.width(), self.height()
        ts = clamp(h * 0.056, 30, 50)
        hero_h = ts * 1.4 + 2 + 24 + 18 + 52
        gs = clamp(h * 0.19, 124, 184)
        gap = 16
        pitch = gs + gap
        cols = max(1, int((w - 88 + gap) // pitch))
        x0 = (w - (cols * pitch - gap)) / 2
        top_safe, bottom_safe = 84, 168      # bottom reserve keeps tiles clear of the Spotify card + hints
        hero_top = top_safe + 8
        top = hero_top + hero_h + 30
        bottom = h - bottom_safe
        vis = max(1, int((bottom - top + gap) // pitch))
        rows = (len(self.items) + cols - 1) // cols
        if rows <= vis:      # few games: centre the whole block vertically
            block = hero_h + 30 + rows * pitch - gap
            shift = max(0.0, (h - top_safe - bottom_safe - block) / 2)
            hero_top += shift
            top += shift
            bottom = top + rows * pitch
        return dict(w=w, h=h, gs=gs, gap=gap, pitch=pitch, cols=cols, x0=x0, top=top, bottom=bottom,
                    vis=vis, hero_top=hero_top)

    def grid_rect(self, i, gm):
        r, c = divmod(i, gm["cols"])
        cnt = min(gm["cols"], len(self.items) - r * gm["cols"])          # every row is centred, even a short last row
        x0 = (gm["w"] - (cnt * gm["pitch"] - gm["gap"])) / 2
        return QRectF(x0 + c * gm["pitch"], gm["top"] + (r - self.gscroll) * gm["pitch"], gm["gs"], gm["gs"])

    # ------------------------------------------------------------- painting
    def paintEvent(self, _e):
        p = QPainter(self)
        try:
            p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                             | QPainter.RenderHint.TextAntialiasing)
            w, h = self.width(), self.height()
            if self.outro is not None:
                self.paint_with_outro(p, w, h)
                return
            self.paint_scene(p, w, h)
            self.paint_boot(p, w, h)
        except Exception:
            if time.perf_counter() - self._err_t > 5:
                self._err_t = time.perf_counter()
                log("paint error:\n" + traceback.format_exc())
        finally:
            p.end()

    def paint_scene(self, p, w, h):
        self.paint_bg(p, w, h)
        p.setOpacity(self.chrome)
        self.paint_top(p, w)
        p.setOpacity(1.0)
        if self.view_style == "grid":
            gm = self.grid_metrics()
            self.paint_grid(p, gm)
            self.paint_info(p, w, h, gm["hero_top"])
        else:
            m = self.metrics()
            self.paint_carousel(p, m)
            self.paint_info(p, w, h, m[4] + m[3] / 2 + 30)
        p.setOpacity(self.chrome)
        self.paint_bottom(p, w, h)
        p.setOpacity(1.0)
        self.paint_spotify(p, w, h)
        self.paint_overlays(p, w, h)
        self.paint_launch(p, w, h)
        self.paint_dialog(p, w, h)
        self.paint_fade(p, w, h)

    def paint_bg(self, p, w, h):
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0, QColor("#0b0c18"))
        base.setColorAt(1, QColor("#05050b"))
        p.fillRect(self.rect(), base)

        bt = ease_out(self.bg_t)
        zoom = 1.08 + 0.03 * math.sin(self.t * 0.12)
        zw, zh = w * zoom, h * zoom
        zr = QRectF((w - zw) / 2 + math.sin(self.t * 0.07) * w * 0.012,
                    (h - zh) / 2 + math.cos(self.t * 0.05) * h * 0.012, zw, zh)
        for g, a in ((self.bg_prev, 1 - bt), (self.bg_cur, bt)):
            if g and a > 0.01:
                pm = bg_pixmap(g, w, h)
                p.setOpacity(0.78 * a)
                p.drawPixmap(zr, pm, QRectF(pm.rect()))
        p.setOpacity(1.0)

        vig = QLinearGradient(0, 0, 0, h)
        vig.setColorAt(0, QColor(5, 5, 11, 60))
        vig.setColorAt(0.5, QColor(5, 5, 11, 105))
        vig.setColorAt(1, QColor(5, 5, 11, 225))
        p.fillRect(self.rect(), vig)
        edge = QRadialGradient(w / 2, h * 0.48, max(w, h) * 0.78)
        edge.setColorAt(0.55, QColor(0, 0, 0, 0))
        edge.setColorAt(1, QColor(0, 0, 0, 150))
        p.fillRect(self.rect(), edge)

        c1, c2 = col(self.acc1), col(self.acc2)
        big = max(w, h)
        for (cx, cy, r, c, a) in (
            (w * (0.22 + 0.08 * math.sin(self.t * 0.13)), h * 0.10, big * 0.75, c1, 0.30),
            (w * (0.82 + 0.06 * math.cos(self.t * 0.11)), h * 0.78, big * 0.65, c2, 0.24),
        ):
            gr = QRadialGradient(cx, cy, r)
            gr.setColorAt(0, alpha(c, a))
            gr.setColorAt(1, alpha(c, 0))
            p.fillRect(self.rect(), gr)

        p.setPen(Qt.PenStyle.NoPen)
        for x, y, r, _sp, a, mt in self.bok:
            cx = x * w + math.sin(self.t * 0.3 + x * 10) * 12
            cy = y * h
            c = QColor.fromRgbF(lerp(c1.redF(), c2.redF(), mt), lerp(c1.greenF(), c2.greenF(), mt),
                                lerp(c1.blueF(), c2.blueF(), mt), a * 0.16)
            gr = QRadialGradient(cx, cy, r)
            gr.setColorAt(0, c)
            gr.setColorAt(1, alpha(c, 0))
            p.setBrush(gr)
            p.drawEllipse(QPointF(cx, cy), r, r)

    def round_button(self, p, r, hover):
        p.setPen(QPen(QColor(255, 255, 255, 130 if hover else 70), 1.5))
        p.setBrush(QColor(255, 255, 255, 48 if hover else 22))
        p.drawEllipse(r)

    def draw_power_icon(self, p, r):
        pen = QPen(QColor(255, 255, 255, 235), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        c = r.center()
        rad = 7.5
        p.drawArc(QRectF(c.x() - rad, c.y() - rad + 1.5, 2 * rad, 2 * rad), 125 * 16, 290 * 16)
        p.drawLine(QPointF(c.x(), c.y() - 9), QPointF(c.x(), c.y() - 1.5))

    def draw_style_icon(self, p, r, to_grid):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 235))
        c = r.center()
        if to_grid:                       # shows what you'd switch TO
            for dx in (-5.5, 5.5):
                for dy in (-5.5, 5.5):
                    p.drawRoundedRect(QRectF(c.x() + dx - 4, c.y() + dy - 4, 8, 8), 2, 2)
        else:
            p.drawRoundedRect(QRectF(c.x() - 5, c.y() - 8, 10, 16), 2.5, 2.5)
            p.setBrush(QColor(255, 255, 255, 150))
            p.drawRoundedRect(QRectF(c.x() - 12.5, c.y() - 5.5, 5, 11), 2, 2)
            p.drawRoundedRect(QRectF(c.x() + 7.5, c.y() - 5.5, 5, 11), 2, 2)

    def paint_top(self, p, w):
        mx = 44
        x = mx
        if not self.logo.isNull():
            path = QPainterPath()
            path.addRoundedRect(QRectF(mx, 24, 36, 36), 9, 9)
            p.save()
            p.setClipPath(path)
            p.drawPixmap(QRectF(mx, 24, 36, 36), self.logo, QRectF(self.logo.rect()))
            p.restore()
            x = mx + 48
        f = ui_font(20, QFont.Weight.Bold, 3.0)
        fm = QFontMetrics(f)
        p.setFont(f)
        for txt, c in (("EXE", QColor("#ffffff")), ("//", col(self.acc1)), ("HUB", QColor("#ffffff"))):
            p.setPen(c)
            p.drawText(QPointF(x, 49), txt)
            x += fm.horizontalAdvance(txt)

        vis = self.chrome > 0.3
        pw_r = QRectF(w - mx - 38, 23, 38, 38)
        st_r = QRectF(pw_r.left() - 12 - 38, 23, 38, 38)
        self.hit["power"] = pw_r if vis else None
        self.hit["style"] = st_r if vis else None
        self.round_button(p, pw_r, self.hover == "power")
        self.draw_power_icon(p, pw_r)
        self.round_button(p, st_r, self.hover == "style")
        self.draw_style_icon(p, st_r, self.view_style == "carousel")
        right = st_r.left() - 22

        now = time.localtime()
        p.setFont(ui_font(24, QFont.Weight.DemiBold))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(right - 200, 18, 200, 30), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   time.strftime("%H:%M", now))
        p.setFont(ui_font(12))
        p.setPen(QColor(255, 255, 255, 140))
        p.drawText(QRectF(right - 200, 46, 200, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   time.strftime("%a, %b %d", now))
        if self.pad_on:
            lr = right - 88 - 14
            p.setFont(ui_font(12, QFont.Weight.DemiBold))
            p.setPen(QColor(120, 255, 170, 220))
            p.drawText(QRectF(lr - 200, 30, 200, 26), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       "CONTROLLER")
            p.setBrush(QColor(120, 255, 170, 230))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(lr + 10, 43), 4, 4)

        if self.query:
            sf = ui_font(15, QFont.Weight.DemiBold)
            pw = QFontMetrics(sf).horizontalAdvance(self.query) + 76
            r = QRectF((w - pw) / 2, 24, pw, 36)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.setBrush(QColor(255, 255, 255, 24))
            p.drawRoundedRect(r, 18, 18)
            p.setPen(QPen(QColor(255, 255, 255, 190), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(r.left() + 24, r.center().y() - 1), 5.5, 5.5)
            p.drawLine(QPointF(r.left() + 28, r.center().y() + 3), QPointF(r.left() + 33, r.center().y() + 8))
            p.setFont(sf)
            p.setPen(QColor(255, 255, 255, 240))
            caret = "|" if int(self.t * 2) % 2 == 0 else " "
            p.drawText(QRectF(r.left() + 42, r.top(), pw - 46, r.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.query + caret)

    # ---- carousel style
    def carousel_ks(self, m):
        w, tw = m[0], m[2]
        n = len(self.items)
        R = int((w / 2) / (tw * 0.8) + 2.2)
        base = math.floor(self.scroll_pos)
        if self.loop_mode():
            return range(base - R, base + R + 2)
        return range(max(0, base - R), min(n, base + R + 2))

    def paint_carousel(self, p, m):
        w = m[0]
        n = len(self.items)
        for k in sorted(self.carousel_ks(m), key=lambda k: -abs(k - self.scroll_pos)):
            ad = abs(k - self.scroll_pos)
            te = ease_out(self.intro * 1.7 - ad * 0.10)
            if te <= 0:
                continue
            rect, s, near = self.tile_geom(k, m)
            if rect.right() < -40 or rect.left() > w + 40:
                continue
            rect.translate(0, (1 - te) * 46)
            self.draw_tile(p, self.items[k % n], rect, s, near, te)

    def draw_tile(self, p, g, rect, s, near, a):
        p.save()
        p.setOpacity(a * (0.52 + 0.48 * near))
        radius = ART_R * s
        pulse = 0.85 + 0.15 * math.sin(self.t * 2.2)

        p.save()
        p.translate(rect.center().x(), rect.bottom() + 12)
        p.scale(1, 0.12)
        sh = QRadialGradient(0, 0, rect.width() * 0.52)
        sh.setColorAt(0, QColor(0, 0, 0, 150))
        sh.setColorAt(1, QColor(0, 0, 0, 0))
        r0 = rect.width() * 0.52
        p.fillRect(QRectF(-r0, -r0, 2 * r0, 2 * r0), sh)
        p.restore()

        if near > 0.02:
            gc = col(self.acc1)
            gr_r = max(rect.width(), rect.height()) * 0.95
            gr = QRadialGradient(rect.center(), gr_r)
            gr.setColorAt(0, alpha(gc, 0.55 * near * pulse))
            gr.setColorAt(1, alpha(gc, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gr)
            p.drawEllipse(rect.center(), gr_r, gr_r)

        if g is None:
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            p.fillPath(path, QColor(255, 255, 255, int(18 + 16 * near)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(70 + 80 * near)), 2, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), radius - 4, radius - 4)
            self.draw_plus(p, rect.center(), rect.width() * 0.13, s, 14 * s)
            p.setFont(ui_font(15 * s, QFont.Weight.DemiBold, 1.5))
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(QRectF(rect.left(), rect.center().y() + 34 * s, rect.width(), 24 * s),
                       Qt.AlignmentFlag.AlignCenter, "ADD GAME")
        else:
            art = card_art(g)
            p.drawPixmap(rect, art, QRectF(art.rect()))
            na = 1 - near
            if na > 0.02:
                f = ui_font(15 * s, QFont.Weight.DemiBold)
                txt = QFontMetrics(f).elidedText(g["name"], Qt.TextElideMode.ElideRight, int(rect.width() - 28 * s))
                p.setFont(f)
                p.setPen(QColor(255, 255, 255, int(240 * na)))
                p.drawText(QRectF(rect.left() + 14 * s, rect.bottom() - 40 * s, rect.width() - 28 * s, 28 * s),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
            if g["id"] in self.running:
                self.draw_running_badge(p, QRectF(rect.left() + 12 * s, rect.top() + 12 * s, 92 * s, 24 * s), s)

        if near > 0.05:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(235 * near)), 3))
            p.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), radius + 6, radius + 6)
        p.restore()

    def draw_plus(self, p, c, L, s, dy):
        pen = QPen(QColor(255, 255, 255, 230), 5 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(c.x() - L, c.y() - dy), QPointF(c.x() + L, c.y() - dy))
        p.drawLine(QPointF(c.x(), c.y() - dy - L), QPointF(c.x(), c.y() - dy + L))

    def draw_running_badge(self, p, pr, s):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(pr, 12 * s, 12 * s)
        p.setBrush(QColor(90, 255, 150, int(150 + 100 * math.sin(self.t * 4) ** 2)))
        p.drawEllipse(QPointF(pr.left() + 13 * s, pr.center().y()), 4 * s, 4 * s)
        p.setFont(ui_font(11 * s, QFont.Weight.Bold, 1.0))
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(QRectF(pr.left() + 24 * s, pr.top(), pr.width() - 24 * s, pr.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "RUNNING")

    # ---- grid style (small crisp icons on glass tiles)
    def sel_weight(self, i):
        e = ease_out(self.info_t)
        if i == self.sel_i:
            return e
        if i == self.prev_sel:
            return 1 - e
        return 0.0

    def paint_grid(self, p, gm):
        n = len(self.items)
        top, bottom, gs = gm["top"], gm["bottom"], gm["gs"]
        p.save()
        p.setClipRect(QRectF(0, top - 14, gm["w"], bottom - top + 28))
        for i in sorted(range(n), key=lambda i: self.sel_weight(i)):
            rect = self.grid_rect(i, gm)
            if rect.bottom() < top - 10 or rect.top() > bottom + 10:
                continue
            a_top = clamp(((rect.bottom() - top) / gs - 0.55) / 0.4, 0, 1)
            a_bot = clamp(((bottom - rect.top()) / gs - 0.55) / 0.4, 0, 1)
            r_, c_ = divmod(i, gm["cols"])
            te = ease_out(self.intro * 1.7 - (r_ + c_) * 0.09)
            if te <= 0:
                continue
            rect.translate(0, (1 - te) * 30)
            self.draw_grid_tile(p, self.items[i], rect, self.sel_weight(i), a_top * a_bot * te)
        p.restore()

    def draw_grid_tile(self, p, g, rect, wsel, a):
        p.save()
        p.setOpacity(a * (0.80 + 0.20 * wsel))
        s = 1 + 0.07 * wsel
        rr = QRectF(0, 0, rect.width() * s, rect.height() * s)
        rr.moveCenter(rect.center())
        radius = 20.0
        if wsel > 0.02:
            gc = col(self.acc1)
            gr_r = rr.width() * 0.95
            gr = QRadialGradient(rr.center(), gr_r)
            gr.setColorAt(0, alpha(gc, 0.50 * wsel))
            gr.setColorAt(1, alpha(gc, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gr)
            p.drawEllipse(rr.center(), gr_r, gr_r)
        path = QPainterPath()
        path.addRoundedRect(rr, radius, radius)
        if g is None:
            p.fillPath(path, QColor(255, 255, 255, int(20 + 14 * wsel)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(80 + 80 * wsel)), 2, Qt.PenStyle.DashLine))
            p.drawRoundedRect(rr.adjusted(5, 5, -5, -5), radius - 3, radius - 3)
            self.draw_plus(p, rr.center(), rr.width() * 0.12, 0.8, 10)
            p.setFont(ui_font(12, QFont.Weight.DemiBold, 1.4))
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(QRectF(rr.left(), rr.center().y() + 26, rr.width(), 22), Qt.AlignmentFlag.AlignCenter, "ADD GAME")
        else:
            c1, c2 = theme(g)
            p.fillPath(path, QColor(18, 20, 34, 238))
            tint = QLinearGradient(rr.topLeft(), rr.bottomRight())
            tint.setColorAt(0, alpha(c1, 0.55))
            tint.setColorAt(1, alpha(c2, 0.10))
            p.fillPath(path, tint)
            hl = QRadialGradient(rr.left() + rr.width() * 0.25, rr.top() + rr.height() * 0.18, rr.width() * 0.9)
            hl.setColorAt(0, QColor(255, 255, 255, 46))
            hl.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillPath(path, hl)
            p.save()
            p.setClipPath(path)
            cover = g.get("cover")
            cpm = QPixmap(cover) if cover and Path(cover).exists() else QPixmap()
            if not cpm.isNull():
                sc = cpm.scaled(int(rr.width() * 2), int(rr.height() * 2),
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                p.drawPixmap(QRectF(rr.center().x() - sc.width() / 4, rr.center().y() - sc.height() / 4,
                                    sc.width() / 2, sc.height() / 2), sc, QRectF(sc.rect()))
            else:
                want = int(round(rr.width() * 0.56 * 2 / 16.0) * 16)
                ipm = icon_pixmap(g, want)
                ic = QPointF(rr.center().x(), rr.center().y() - rr.height() * 0.08)
                if ipm is not None and not ipm.isNull():
                    sw, sh_ = ipm.width() / 2, ipm.height() / 2
                    glow = QRadialGradient(ic, max(sw, sh_) * 0.9)
                    glow.setColorAt(0, alpha(c1, 0.55))
                    glow.setColorAt(1, alpha(c1, 0))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(glow)
                    p.drawEllipse(ic, max(sw, sh_) * 0.9, max(sw, sh_) * 0.9)
                    p.drawPixmap(QRectF(ic.x() - sw / 2, ic.y() - sh_ / 2, sw, sh_), ipm, QRectF(ipm.rect()))
                else:
                    p.setFont(ui_font(rr.height() * 0.40, QFont.Weight.Bold))
                    p.setPen(QColor(255, 255, 255, 230))
                    p.drawText(QRectF(rr.left(), ic.y() - rr.height() * 0.26, rr.width(), rr.height() * 0.52),
                               Qt.AlignmentFlag.AlignCenter, (g["name"][:1] or "?").upper())
            scrim = QLinearGradient(0, rr.top() + rr.height() * 0.6, 0, rr.bottom())
            scrim.setColorAt(0, QColor(0, 0, 0, 0))
            scrim.setColorAt(1, QColor(0, 0, 0, 170))
            p.fillRect(rr, scrim)
            p.restore()
            f = ui_font(13, QFont.Weight.DemiBold)
            txt = QFontMetrics(f).elidedText(g["name"], Qt.TextElideMode.ElideRight, int(rr.width() - 22))
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(QRectF(rr.left() + 11, rr.bottom() - 34, rr.width() - 22, 26), Qt.AlignmentFlag.AlignCenter, txt)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.5))
            p.drawPath(path)
            if g["id"] in self.running:
                self.draw_running_badge(p, QRectF(rr.left() + 10, rr.top() + 10, 88, 22), 0.9)
        if wsel > 0.05:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(235 * wsel)), 3))
            p.drawRoundedRect(rr.adjusted(-5, -5, 5, 5), radius + 5, radius + 5)
        p.restore()

    # ---- shared: title + buttons block
    def paint_info(self, p, w, h, base):
        g = self.current_item()
        a = ease_out(self.info_t)
        base = base + (1 - a) * 10
        p.save()
        p.setOpacity(a)
        if g is None:
            if self.query and len(self.items) == 1:
                title, sub = "No matches", f'Nothing in your library matches "{self.query}"'
            elif self.data["games"]:
                title, sub = "Add a game", "Pick .exe files - or just drop them anywhere on this window"
            else:
                title, sub = "Add your first game", "Pick .exe files - or just drop them anywhere on this window"
            kind = "add"
        else:
            title = g["name"]
            parts = [ago(g.get("last_played", 0))]
            pl = fmt_played(g)
            if pl:
                parts.append(pl)
            sub = "   \u2022   ".join(parts)
            kind = "running" if g["id"] in self.running else "play"

        tf = ui_font(clamp(h * 0.056, 30, 50), QFont.Weight.Bold)
        tfm = QFontMetrics(tf)
        title = tfm.elidedText(title, Qt.TextElideMode.ElideRight, int(w * 0.7))
        th_ = tfm.height()
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255, 250))
        p.drawText(QRectF(0, base, w, th_), Qt.AlignmentFlag.AlignCenter, title)
        p.setFont(ui_font(15))
        p.setPen(QColor(255, 255, 255, 165))
        sub_y = base + th_ + 2
        p.drawText(QRectF(0, sub_y, w, 24), Qt.AlignmentFlag.AlignCenter, sub)

        by = sub_y + 24 + 22 if self.view_style == "carousel" else sub_y + 24 + 18
        bw, bh, gap = 214, 52, 14
        bx = (w - bw) / 2
        play_r = QRectF(bx, by, bw, bh)
        self.hit["play"] = play_r
        self.paint_main_button(p, play_r, kind, g)
        if g is not None:
            opt_r = QRectF(bx + bw + gap, by, bh, bh)
            self.hit["opt"] = opt_r
            hv = self.hover == "opt"
            p.setPen(QPen(QColor(255, 255, 255, 120 if hv else 60), 1.5))
            p.setBrush(QColor(255, 255, 255, 52 if hv else 26))
            p.drawEllipse(opt_r)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            for dx in (-10, 0, 10):
                p.drawEllipse(QPointF(opt_r.center().x() + dx, opt_r.center().y()), 2.6, 2.6)
        else:
            self.hit["opt"] = None
        p.restore()

    def paint_main_button(self, p, r, kind, g):
        c1, c2 = col(self.acc1), col(self.acc2)
        hv = self.hover == "play" and kind != "running"
        radius = r.height() / 2
        if kind != "running":
            p.save()
            p.translate(r.center().x(), r.bottom() + 6)
            p.scale(1, 0.2)
            rr = r.width() * 0.62
            sh = QRadialGradient(0, 0, rr)
            sh.setColorAt(0, alpha(c1, 0.55))
            sh.setColorAt(1, alpha(c1, 0))
            p.fillRect(QRectF(-rr, -rr, 2 * rr, 2 * rr), sh)
            p.restore()
            gr = QLinearGradient(r.left(), 0, r.right(), 0)
            gr.setColorAt(0, c1.lighter(112) if hv else c1)
            gr.setColorAt(1, c2.lighter(112) if hv else c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gr)
            p.drawRoundedRect(r, radius, radius)
            lum = 0.299 * c1.redF() + 0.587 * c1.greenF() + 0.114 * c1.blueF()
            fg = QColor("#10121c") if lum > 0.66 else QColor("#ffffff")
        else:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1.5))
            p.setBrush(QColor(255, 255, 255, 22))
            p.drawRoundedRect(r, radius, radius)
            fg = QColor(255, 255, 255, 235)

        if kind == "running":
            secs = int(time.time() - self.running[g["id"]]["start"]) if g and g["id"] in self.running else 0
            label = "RUNNING  %02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
        elif kind == "add":
            label = "ADD GAMES"
        else:
            label = "PLAY"
        f = ui_font(16, QFont.Weight.Bold, 2.0)
        tw_ = QFontMetrics(f).horizontalAdvance(label)
        total = 16 + 12 + tw_
        x0 = r.center().x() - total / 2
        cy = r.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fg)
        if kind == "play":
            tri = QPainterPath()
            tri.moveTo(x0, cy - 8)
            tri.lineTo(x0 + 14, cy)
            tri.lineTo(x0, cy + 8)
            tri.closeSubpath()
            p.drawPath(tri)
        elif kind == "add":
            pen = QPen(fg, 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x0 + 1, cy), QPointF(x0 + 15, cy))
            p.drawLine(QPointF(x0 + 8, cy - 7), QPointF(x0 + 8, cy + 7))
        else:
            p.setBrush(QColor(90, 255, 150, int(150 + 100 * math.sin(self.t * 4) ** 2)))
            p.drawEllipse(QPointF(x0 + 6, cy), 5, 5)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(QRectF(x0 + 28, r.top(), tw_ + 6, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

    def paint_bottom(self, p, w, h):
        mx = 44
        y = h - 52
        n = len(self.data["games"])
        total = sum(g.get("seconds", 0) for g in self.data["games"])
        th_, tm_ = divmod(total // 60, 60)
        stats = f"{n} game{'s' if n != 1 else ''}   \u2022   {th_}h {tm_}m played"
        p.setFont(ui_font(13))
        p.setPen(QColor(255, 255, 255, 130))
        if w >= 1100:
            p.drawText(QRectF(0, y, w, 28), Qt.AlignmentFlag.AlignCenter, stats)
        elif self.sp_alpha < 0.05:
            p.drawText(QRectF(mx, y, 420, 28), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, stats)

        if self.pad_on:
            hints = [("A", "Play"), ("Y", "Options"), ("X", "Add"), ("Back", "Style")]
        else:
            hints = [("\u2190 \u2192", "Browse"), ("Enter", "Play"), ("Tab", "Style"), ("Type", "Search")]
        kf, lf = ui_font(11, QFont.Weight.Bold, 0.5), ui_font(13)
        kfm, lfm = QFontMetrics(kf), QFontMetrics(lf)
        widths = [kfm.horizontalAdvance(k) + 20 + 8 + lfm.horizontalAdvance(lbl) + 26 for k, lbl in hints]
        x = w - mx - sum(widths) + 26
        for (k, lbl), wd in zip(hints, widths):
            kw = kfm.horizontalAdvance(k) + 20
            kr = QRectF(x, y + 2, kw, 24)
            p.setPen(QPen(QColor(255, 255, 255, 90), 1.2))
            p.setBrush(QColor(255, 255, 255, 18))
            rad = 12 if self.pad_on else 6
            p.drawRoundedRect(kr, rad, rad)
            p.setFont(kf)
            p.setPen(QColor(255, 255, 255, 220))
            p.drawText(kr, Qt.AlignmentFlag.AlignCenter, k)
            p.setFont(lf)
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QRectF(kr.right() + 8, y, lfm.horizontalAdvance(lbl) + 4, 28),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
            x += wd

    # ---- spotify card (bottom-left)
    def draw_eq(self, p, x, y, playing, color):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for i in range(4):
            hh = 4 + (7 * abs(math.sin(self.t * 5 + i * 1.7)) if playing else 0)
            p.drawRoundedRect(QRectF(x + i * 5, y + 12 - hh, 3, hh), 1.2, 1.2)

    def marquee(self, p, text, font, x, y, w, hh, color, a=1.0, dx=0.0):
        """Single-line text; if it doesn't fit it glides back and forth instead of being cut off."""
        tw_ = QFontMetrics(font).horizontalAdvance(text)
        c = QColor(color)
        c.setAlphaF(c.alphaF() * clamp(a, 0, 1))
        p.save()
        p.setClipRect(QRectF(x - 1, y, w + 2, hh))
        p.setFont(font)
        p.setPen(c)
        if tw_ <= w:
            p.drawText(QRectF(x + dx, y, w, hh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        else:
            ph = (self.t * 0.22) % 2
            tri = ph if ph < 1 else 2 - ph
            e = clamp((tri - 0.12) / 0.76, 0, 1)
            e = e * e * (3 - 2 * e)
            p.drawText(QRectF(x + dx - (tw_ - w + 8) * e, y, tw_ + 10, hh),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.restore()

    def art_scaled(self, pm, size):
        key = (pm.cacheKey(), size)
        r = self._art_scaled.get(key)
        if r is None:
            if len(self._art_scaled) > 6:
                self._art_scaled.clear()
            r = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
            self._art_scaled[key] = r
        return r

    def paint_spotify(self, p, w, h):
        for k in ("sp", "sp_prev", "sp_toggle", "sp_next"):
            self.hit[k] = None
        st = self.sp
        if not st or self.sp_in < 0.02:
            return
        a_in = ease_out_back(self.sp_in)                      # slides in with a little overshoot
        op = clamp(self.sp_in * 1.5, 0, 1) * max(self.chrome, 0.70)
        k = 0.8 if (w < 1200 or h < 700) else 1.0
        CW, CH = 420.0, 126.0
        hov = self.sp_hover
        pt = self.sp_play_t
        x = 44 - (1 - a_in) * 70
        y = h - 36 - CH * k - 3 * hov
        ac = col(self.sp_acc_cur)
        live = self.sp_in > 0.5

        def G(rx, ry, rw, rh):      # card coordinates -> screen coordinates (for hit-testing)
            return QRectF(x + rx * k, y + ry * k, rw * k, rh * k)

        if live:
            self.hit["sp"] = G(0, 0, CW, CH)
        p.save()
        p.setOpacity(op)
        p.translate(x, y)
        p.scale(k, k)

        # soft ground shadow
        p.save()
        p.translate(CW / 2, CH + 8)
        p.scale(1, 0.14)
        rad = CW * 0.56
        sh = QRadialGradient(0, 0, rad)
        sh.setColorAt(0, QColor(0, 0, 0, 140))
        sh.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(-rad, -rad, 2 * rad, 2 * rad), sh)
        p.restore()

        panel = QPainterPath()
        panel.addRoundedRect(QRectF(0, 0, CW, CH), 24, 24)
        bgg = QLinearGradient(0, 0, 0, CH)
        bgg.setColorAt(0, QColor(28, 30, 50, 242))
        bgg.setColorAt(1, QColor(14, 15, 28, 242))
        p.fillPath(panel, bgg)
        wash = QLinearGradient(0, 0, CW, CH * 1.2)
        wash.setColorAt(0, alpha(ac, 0.34 + 0.12 * hov))
        wash.setColorAt(0.7, alpha(ac, 0.04))
        wash.setColorAt(1, alpha(ac, 0))
        p.fillPath(panel, wash)

        # breathing glow behind the cover (clipped to the card)
        p.save()
        p.setClipPath(panel)
        gc = QPointF(62, 62)
        gl = QRadialGradient(gc, 96)
        gl.setColorAt(0, alpha(ac, (0.42 + 0.14 * math.sin(self.t * 1.6)) * (0.4 + 0.6 * pt)))
        gl.setColorAt(1, alpha(ac, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gl)
        p.drawEllipse(gc, 96, 96)
        p.restore()

        def draw_art(pm, a, sc):
            p.save()
            p.setOpacity(op * a)
            r0 = QRectF(14, 14, 96, 96)
            rr = QRectF(0, 0, 96 * sc, 96 * sc)
            rr.moveCenter(r0.center())
            path = QPainterPath()
            path.addRoundedRect(rr, 18 * sc, 18 * sc)
            p.setClipPath(path)
            if pm is not None:
                scl = self.art_scaled(pm, 192)
                p.drawPixmap(rr, scl, QRectF((scl.width() - 192) / 2, (scl.height() - 192) / 2, 192, 192))
            else:
                ag = QLinearGradient(rr.topLeft(), rr.bottomRight())
                ag.setColorAt(0, ac)
                ag.setColorAt(1, ac.darker(230))
                p.fillRect(rr, ag)
                self.draw_eq(p, rr.center().x() - 9.5, rr.center().y() - 6, st["playing"], QColor(255, 255, 255, 235))
            p.restore()

        breathe = 1 + 0.02 * math.sin(self.t * 1.6) * pt
        if self.sp_at < 1.0:                                   # track changed: old cover shrinks away, new one grows in
            e = ease_out(self.sp_at)
            draw_art(self.sp_art_old, 1 - e, 1.0)
            draw_art(self.sp_art, e, (0.92 + 0.08 * e) * breathe)
        else:
            draw_art(self.sp_art, 1.0, breathe)
        if pt < 0.98:                                          # paused: cover dims, pause glyph fades in
            pa = 1 - pt
            cp = QPainterPath()
            cp.addRoundedRect(QRectF(14, 14, 96, 96), 18, 18)
            p.fillPath(cp, QColor(0, 0, 0, int(125 * pa)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(235 * pa)))
            p.drawRoundedRect(QRectF(53, 51, 6, 22), 2, 2)
            p.drawRoundedRect(QRectF(65, 51, 6, 22), 2, 2)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, int(44 + 34 * hov)), 1.3))
        p.drawPath(panel)

        # ---- right column: label + transport on one row, then title / artist, then progress
        x0, tw = 128.0, 278.0
        lab_col = QColor(int(lerp(255, SPOTIFY_GREEN.red(), pt)), int(lerp(255, SPOTIFY_GREEN.green(), pt)),
                         int(lerp(255, SPOTIFY_GREEN.blue(), pt)), int(lerp(120, 255, pt)))
        self.draw_eq(p, x0, 17, st["playing"], lab_col)
        p.setFont(ui_font(10, QFont.Weight.Bold, 1.8))
        p.setPen(lab_col)
        p.drawText(QRectF(x0 + 24, 14, 110, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "SPOTIFY" if st["playing"] else "PAUSED")

        cy_ = 29.0
        dark = QColor(18, 19, 32)
        for name, cx, rad in (("sp_prev", 317, 13), ("sp_toggle", 355, 17), ("sp_next", 393, 13)):
            if live:
                self.hit[name] = G(cx - rad - 3, cy_ - rad - 3, 2 * (rad + 3), 2 * (rad + 3))
            hv = self.hover == name
            ps = 1 - 0.16 * clamp(self.sp_press.get(name, 0.0), 0, 1)       # tiny squish when pressed
            p.save()
            p.translate(cx, cy_)
            p.scale(ps, ps)
            p.setPen(Qt.PenStyle.NoPen)
            if name == "sp_toggle":
                p.setBrush(QColor(255, 255, 255, 255 if hv else 235))
                p.drawEllipse(QPointF(0, 0), rad + (1.5 if hv else 0), rad + (1.5 if hv else 0))
                p.setBrush(QColor(dark.red(), dark.green(), dark.blue(), int(255 * pt)))      # pause <-> play morph
                p.drawRoundedRect(QRectF(-5.2, -6.2, 3.6, 12.4), 1, 1)
                p.drawRoundedRect(QRectF(1.6, -6.2, 3.6, 12.4), 1, 1)
                p.setBrush(QColor(dark.red(), dark.green(), dark.blue(), int(255 * (1 - pt))))
                tri = QPainterPath()
                tri.moveTo(-3.6, -7)
                tri.lineTo(6.4, 0)
                tri.lineTo(-3.6, 7)
                tri.closeSubpath()
                p.drawPath(tri)
            else:
                if hv:
                    p.setBrush(QColor(255, 255, 255, 46))
                    p.drawEllipse(QPointF(0, 0), rad, rad)
                p.setBrush(QColor(255, 255, 255, 255 if hv else 205))
                d = -1 if name == "sp_prev" else 1
                tri = QPainterPath()
                tri.moveTo(-6 * d, -6)
                tri.lineTo(3 * d, 0)
                tri.lineTo(-6 * d, 6)
                tri.closeSubpath()
                p.drawPath(tri)
                p.drawRoundedRect(QRectF(min(5 * d, 7.4 * d), -6, 2.4, 12), 1, 1)
            p.restore()

        tf, af = ui_font(18, QFont.Weight.Bold), ui_font(13)

        def text_block(title, artist, dx, a):
            self.marquee(p, title, tf, x0, 48, tw, 26, QColor(255, 255, 255, 250), a, dx)
            self.marquee(p, artist, af, x0, 74, tw, 20, QColor(255, 255, 255, 160), a, dx)

        if self.sp_ct < 1.0 and self.sp_old:                   # old text slides out left, new slides in from the right
            eo = ease_out(clamp(self.sp_ct / 0.45, 0, 1))              # old text clears first...
            en = ease_out(clamp((self.sp_ct - 0.30) / 0.70, 0, 1))     # ...then the new text arrives (no overlap)
            text_block(self.sp_old[0], self.sp_old[1], -28 * eo, 1 - eo)
            text_block(st["title"], st["artist"], 28 * (1 - en), en)
        else:
            text_block(st["title"], st["artist"], 0, 1)

        dur = st["dur"]
        if dur > 0:
            frac = clamp(self.sp_pos_disp / dur, 0, 1)
            bar = QRectF(x0, 102, tw, 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(38 + 20 * hov)))
            p.drawRoundedRect(bar, 2.5, 2.5)
            fw = max(5.0, tw * frac)
            fg = QLinearGradient(bar.left(), 0, bar.right(), 0)
            fg.setColorAt(0, ac.lighter(115))
            fg.setColorAt(1, ac.lighter(150))
            p.setBrush(fg)
            p.save()
            p.setOpacity(op * (0.55 + 0.45 * pt))
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), fw, 5), 2.5, 2.5)
            p.restore()
            p.setBrush(QColor(255, 255, 255, int(255 * (0.65 + 0.35 * hov))))
            hr = 4.5 + 1.5 * hov
            p.drawEllipse(QPointF(bar.left() + fw, bar.center().y()), hr, hr)
            p.setFont(ui_font(10))
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(QRectF(x0, 110, 90, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fmt_clock(self.sp_pos_disp))
            p.drawText(QRectF(x0 + tw - 90, 110, 90, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       fmt_clock(dur))
        p.restore()

    # ---- confirm dialog, launch transition, fade-in
    def paint_dialog(self, p, w, h):
        d = self.dlg
        if not d:
            return
        a = ease_out(self.dlg_t)
        p.save()
        p.fillRect(self.rect(), QColor(4, 4, 10, int(205 * a)))
        pw, ph = 540.0, 244.0
        panel = QRectF((w - pw) / 2, (h - ph) / 2 + (1 - a) * 22, pw, ph)
        p.setOpacity(a)
        path = QPainterPath()
        path.addRoundedRect(panel, 26, 26)
        g = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        g.setColorAt(0, QColor(30, 32, 52, 250))
        g.setColorAt(1, QColor(16, 17, 30, 250))
        p.fillPath(path, g)
        tint = col(self.acc1) if not d["danger"] else QColor(255, 70, 90)
        wash = QLinearGradient(panel.topLeft(), panel.bottomRight())
        wash.setColorAt(0, alpha(tint, 0.20))
        wash.setColorAt(0.6, alpha(tint, 0.0))
        p.fillPath(path, wash)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 55), 1.3))
        p.drawPath(path)
        p.setFont(ui_font(24, QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255, 250))
        p.drawText(QRectF(panel.left() + 36, panel.top() + 30, pw - 72, 34),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, d["title"])
        p.setFont(ui_font(15))
        p.setPen(QColor(255, 255, 255, 175))
        p.drawText(QRectF(panel.left() + 36, panel.top() + 76, pw - 72, 80),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, d["text"])
        bw, bh = 160.0, 50.0
        by = panel.bottom() - 32 - bh
        no_r = QRectF(panel.right() - 36 - bw * 2 - 14, by, bw, bh)
        yes_r = QRectF(panel.right() - 36 - bw, by, bw, bh)
        self.hit["dlg_no"], self.hit["dlg_yes"] = no_r, yes_r
        for r, label, is_yes in ((no_r, "Cancel", False), (yes_r, d["yes"], True)):
            sel = (d["sel"] == 1) == is_yes
            rr = r.adjusted(-3, -3, 3, 3) if sel else r
            p.setPen(Qt.PenStyle.NoPen)
            if is_yes:
                gg = QLinearGradient(rr.left(), 0, rr.right(), 0)
                if d["danger"]:
                    gg.setColorAt(0, QColor(255, 82, 100))
                    gg.setColorAt(1, QColor(222, 44, 78))
                else:
                    gg.setColorAt(0, col(self.acc1))
                    gg.setColorAt(1, col(self.acc2))
                p.setBrush(gg)
            else:
                p.setBrush(QColor(255, 255, 255, 30 if not sel else 52))
            p.drawRoundedRect(rr, bh / 2, bh / 2)
            if sel:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 235), 2.5))
                p.drawRoundedRect(rr.adjusted(-4, -4, 4, 4), bh / 2 + 4, bh / 2 + 4)
            p.setFont(ui_font(15, QFont.Weight.Bold, 1.0))
            p.setPen(QColor(255, 255, 255, 250))
            p.drawText(rr, Qt.AlignmentFlag.AlignCenter, label)
        p.restore()

    def paint_launch(self, p, w, h):
        L = self.launching
        if not L:
            return
        t = clamp(L["t"], 0, 1)
        e = ease_out(t)
        rad = lerp(60, math.hypot(w, h) * 1.15, e)
        gr = QRadialGradient(L["c"], rad)                      # colour bloom grows out of the tile...
        gr.setColorAt(0.0, alpha(L["color"], 0.0))
        gr.setColorAt(0.70, alpha(L["color"], 0.50 * (1 - 0.4 * t)))
        gr.setColorAt(1.0, alpha(L["color"], 0.0))
        p.fillRect(self.rect(), gr)
        kk = clamp((t - 0.30) / 0.70, 0, 1)                    # ...then the screen falls to black
        p.fillRect(self.rect(), QColor(0, 0, 0, int(255 * kk * kk)))
        ta = math.sin(clamp((t - 0.1) / 0.9, 0, 1) * math.pi)
        p.setFont(ui_font(13, QFont.Weight.Bold, 4))
        p.setPen(QColor(255, 255, 255, int(170 * ta)))
        p.drawText(QRectF(0, h * 0.5 - 40, w, 22), Qt.AlignmentFlag.AlignCenter, "LAUNCHING")
        p.setFont(ui_font(34, QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255, int(255 * ta)))
        p.drawText(QRectF(0, h * 0.5 - 14, w, 50), Qt.AlignmentFlag.AlignCenter, L["name"])

    # ---- intro: particles converge into the logo, ring + streak, wordmark types in, loading bar, iris reveal
    def paint_boot(self, p, w, h):
        t = self.boot_t
        if t >= 1.0:
            return
        c = QPointF(w / 2, h / 2)
        reveal = clamp((t - 0.78) / 0.22, 0, 1)
        er = reveal * reveal * (3 - 2 * reveal)
        diag = math.hypot(w, h) / 2
        p.save()
        if reveal > 0:                                   # black cover with a circular hole that opens onto the hub
            R = diag * 1.1 * er
            cover = QPainterPath()
            cover.addRect(QRectF(0, 0, w, h))
            hole = QPainterPath()
            hole.addEllipse(c, R, R)
            p.fillPath(cover.subtracted(hole), QColor(3, 3, 8))
            if R > 2:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(alpha(BRAND2.lighter(125), 0.85 * (1 - er)), 3 + 12 * (1 - er)))
                p.drawEllipse(c, R, R)
        else:
            p.fillRect(self.rect(), QColor(3, 3, 8))
        fa = 1 - clamp(reveal * 1.7, 0, 1)               # splash fades + zooms through as the iris opens
        if fa <= 0.001:
            p.restore()
            return
        zs = 1 + 0.45 * er
        p.translate(c)
        p.scale(zs, zs)
        p.translate(-c)
        p.setPen(Qt.PenStyle.NoPen)

        gl = QRadialGradient(c, 560 * (0.55 + 0.45 * clamp(t / 0.35, 0, 1)))        # ambient glow
        a = 0.34 * fa * clamp(t / 0.14, 0, 1)
        gl.setColorAt(0, alpha(BRAND2, a))
        gl.setColorAt(0.45, alpha(BRAND1, a * 0.45))
        gl.setColorAt(1, alpha(BRAND1, 0))
        p.fillRect(QRectF(c.x() - 700, c.y() - 700, 1400, 1400), gl)

        for ang, dist, sz, delay, mix in self.boot_dots:                            # particles fly into the logo
            pr = ease_out(clamp((t - 0.04 - delay * 0.16) / 0.34, 0, 1))
            if pr <= 0 or pr >= 1:
                continue
            r = dist * (1 - pr)
            pos = QPointF(c.x() + math.cos(ang) * r, c.y() - 34 + math.sin(ang) * r * 0.62)
            colr = QColor(int(lerp(BRAND1.red(), BRAND3.red(), mix)), int(lerp(BRAND1.green(), BRAND3.green(), mix)),
                          int(lerp(BRAND1.blue(), BRAND3.blue(), mix)))
            pa = clamp(pr * 4, 0, 1) * (1 - pr ** 3) * fa
            p.setBrush(alpha(colr, 0.18 * pa))
            p.drawEllipse(pos, sz * 3.2, sz * 3.2)
            p.setBrush(alpha(colr.lighter(140), 0.95 * pa))
            p.drawEllipse(pos, sz, sz)

        sT = clamp((t - 0.30) / 0.22, 0, 1)                                          # anamorphic light streak
        if 0 < sT < 1:
            sw = w * 1.15 * ease_out(sT)
            sa = (1 - sT) ** 1.3 * fa
            for hh, aa in ((26, 0.20), (4, 0.95)):
                sg = QLinearGradient(c.x() - sw / 2, 0, c.x() + sw / 2, 0)
                sg.setColorAt(0, QColor(255, 255, 255, 0))
                sg.setColorAt(0.5, alpha(QColor(235, 230, 255), aa * sa))
                sg.setColorAt(1, QColor(255, 255, 255, 0))
                p.fillRect(QRectF(c.x() - sw / 2, c.y() - 34 - hh / 2, sw, hh), sg)

        rT = clamp((t - 0.28) / 0.34, 0, 1)                                          # shockwave ring
        if 0 < rT < 1:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(alpha(BRAND1.lighter(150), (1 - rT) * 0.6 * fa), 2 + 6 * (1 - rT)))
            rr = lerp(70, 560, ease_out(rT))
            p.drawEllipse(QPointF(c.x(), c.y() - 34), rr, rr)
            p.setPen(Qt.PenStyle.NoPen)

        lT = clamp((t - 0.10) / 0.30, 0, 1)                                          # logo pops in
        la = clamp((t - 0.10) / 0.14, 0, 1) * fa
        if la > 0 and not self.logo_big.isNull():
            size = 168 * (0.55 + 0.45 * ease_out_back(lT))
            lc = QPointF(c.x(), c.y() - 34)
            lg = QRadialGradient(lc, size * 1.25)
            lg.setColorAt(0, alpha(BRAND2, (0.55 + 0.12 * math.sin(t * 22)) * la))
            lg.setColorAt(1, alpha(BRAND2, 0))
            p.setBrush(lg)
            p.drawEllipse(lc, size * 1.25, size * 1.25)
            p.setOpacity(la)
            p.drawPixmap(QRectF(lc.x() - size / 2, lc.y() - size / 2, size, size), self.logo_big,
                         QRectF(self.logo_big.rect()))
            p.setOpacity(1.0)

        text = "EXE//HUB"                                                              # wordmark types in letter by letter
        f = ui_font(46, QFont.Weight.Bold, 16)
        fm = QFontMetrics(f)
        x0 = c.x() - fm.horizontalAdvance(text) / 2
        base_y = c.y() + 118
        p.setFont(f)
        for i, ch in enumerate(text):
            la_ = ease_out(clamp((t - (0.36 + i * 0.028)) / 0.13, 0, 1))
            if la_ <= 0:
                continue
            slash = i in (3, 4)
            colr = BRAND3 if slash else QColor(255, 255, 255)
            p.setPen(alpha(colr, la_ * fa))
            p.drawText(QPointF(x0 + fm.horizontalAdvance(text[:i]), base_y + (1 - la_) * 22), ch)

        bT = clamp((t - 0.52) / 0.26, 0, 1)                                          # loading bar
        ba = clamp((t - 0.50) / 0.06, 0, 1) * fa
        if ba > 0:
            bw = 360.0
            by = c.y() + 152
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(32 * ba)))
            p.drawRoundedRect(QRectF(c.x() - bw / 2, by, bw, 4), 2, 2)
            fw = max(4.0, bw * (bT * bT * (3 - 2 * bT)))
            bg_ = QLinearGradient(c.x() - bw / 2, 0, c.x() + bw / 2, 0)
            bg_.setColorAt(0, alpha(BRAND1, ba))
            bg_.setColorAt(0.5, alpha(BRAND2, ba))
            bg_.setColorAt(1, alpha(BRAND3, ba))
            p.setBrush(bg_)
            p.drawRoundedRect(QRectF(c.x() - bw / 2, by, fw, 4), 2, 2)
            hg = QRadialGradient(QPointF(c.x() - bw / 2 + fw, by + 2), 26)
            hg.setColorAt(0, alpha(QColor(255, 255, 255), 0.9 * ba))
            hg.setColorAt(1, alpha(BRAND2, 0))
            p.setBrush(hg)
            p.drawEllipse(QPointF(c.x() - bw / 2 + fw, by + 2), 26, 26)
            p.setFont(ui_font(12, QFont.Weight.DemiBold, 4))
            p.setPen(QColor(255, 255, 255, int((150 if bT < 1 else 230) * ba)))
            p.drawText(QRectF(c.x() - 200, by + 14, 400, 20), Qt.AlignmentFlag.AlignCenter,
                       "LOADING YOUR LIBRARY" if bT < 1 else "READY")
        p.restore()

    # ---- outro: the hub pulls back while a glowing iris closes in; logo + wordmark appear in the dark, then fade out
    def paint_with_outro(self, p, w, h):
        o = self.outro
        c = QPointF(w / 2, h / 2)
        diag = math.hypot(w, h) / 2
        p.fillRect(self.rect(), QColor(3, 3, 8))
        q1 = clamp(o / 0.58, 0, 1)
        e1 = q1 * q1 * (3 - 2 * q1)                       # smoothstep: the iris eases in and out
        zoom = 1 - 0.12 * e1                              # scene gently recedes as it closes
        p.save()
        p.translate(c)
        p.scale(zoom, zoom)
        p.translate(-c)
        self.paint_scene(p, w, h)
        p.restore()
        R = diag * 1.1 * (1 - e1)
        cover = QPainterPath()
        cover.addRect(QRectF(0, 0, w, h))
        if R > 1:
            hole = QPainterPath()
            hole.addEllipse(c, R, R)
            cover = cover.subtracted(hole)
        p.fillPath(cover, QColor(3, 3, 8))
        if R > 4:                                         # glowing rim on the closing iris
            rim_a = 0.9 * (1 - clamp((o - 0.5) / 0.1, 0, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(alpha(BRAND2.lighter(130), rim_a * 0.35), 16 * (0.4 + e1)))
            p.drawEllipse(c, R, R)
            p.setPen(QPen(alpha(BRAND1.lighter(165), rim_a), 2.5 + 5 * e1))
            p.drawEllipse(c, R, R)
        p.setPen(Qt.PenStyle.NoPen)

        ft = clamp((o - 0.86) / 0.14, 0, 1)               # final fade to black
        fade = 1 - ft
        lt = clamp((o - 0.42) / 0.30, 0, 1)
        if lt > 0 and fade > 0.003 and not self.logo_big.isNull():
            lc = QPointF(c.x(), c.y() - 34)
            size = 168 * (0.6 + 0.4 * ease_out_back(lt)) * (1 - 0.15 * ft)
            la = clamp(lt * 2, 0, 1) * fade
            glow = QRadialGradient(lc, size * 1.3)
            glow.setColorAt(0, alpha(BRAND2, (0.55 + 0.1 * math.sin(self.t * 14)) * la))
            glow.setColorAt(1, alpha(BRAND2, 0))
            p.setBrush(glow)
            p.drawEllipse(lc, size * 1.3, size * 1.3)
            rt = clamp((o - 0.50) / 0.40, 0, 1)           # ring ripple
            if 0 < rt < 1:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(alpha(BRAND1.lighter(150), (1 - rt) * 0.55 * fade), 2 + 5 * (1 - rt)))
                rr = lerp(size * 0.7, 520, ease_out(rt))
                p.drawEllipse(lc, rr, rr)
                p.setPen(Qt.PenStyle.NoPen)
            p.setOpacity(la)
            p.drawPixmap(QRectF(lc.x() - size / 2, lc.y() - size / 2, size, size), self.logo_big,
                         QRectF(self.logo_big.rect()))
            p.setOpacity(1.0)
            for ang, dist, sz, delay, mix in self.boot_dots:     # particles burst outward (the intro, in reverse)
                pr = ease_out(clamp((o - 0.50 - delay * 0.12) / 0.34, 0, 1))
                if pr <= 0 or pr >= 1:
                    continue
                r = dist * 0.55 * pr
                pos = QPointF(lc.x() + math.cos(ang) * r, lc.y() + math.sin(ang) * r * 0.62)
                colr = QColor(int(lerp(BRAND1.red(), BRAND3.red(), mix)), int(lerp(BRAND1.green(), BRAND3.green(), mix)),
                              int(lerp(BRAND1.blue(), BRAND3.blue(), mix)))
                pa = math.sin(pr * math.pi) * fade
                p.setBrush(alpha(colr, 0.16 * pa))
                p.drawEllipse(pos, sz * 3.0, sz * 3.0)
                p.setBrush(alpha(colr.lighter(140), 0.9 * pa))
                p.drawEllipse(pos, sz, sz)

            wt = ease_out(clamp((o - 0.52) / 0.24, 0, 1))        # wordmark: letters glide together as it fades in
            if wt > 0:
                text = "EXE//HUB"
                f = ui_font(38, QFont.Weight.Bold, lerp(44, 14, wt))
                fm = QFontMetrics(f)
                x0 = c.x() - fm.horizontalAdvance(text) / 2
                p.setFont(f)
                for i, ch in enumerate(text):
                    colr = BRAND3 if i in (3, 4) else QColor(255, 255, 255)
                    p.setPen(alpha(colr, wt * fade))
                    p.drawText(QPointF(x0 + fm.horizontalAdvance(text[:i]), c.y() + 112), ch)
            tg = ease_out(clamp((o - 0.64) / 0.20, 0, 1))
            if tg > 0:
                p.setFont(ui_font(12, QFont.Weight.DemiBold, 5))
                p.setPen(QColor(255, 255, 255, int(150 * tg * fade)))
                p.drawText(QRectF(c.x() - 250, c.y() + 132, 500, 22), Qt.AlignmentFlag.AlignCenter, "SEE YOU NEXT TIME")

    def paint_fade(self, p, w, h):
        if self.black > 0.002:
            p.fillRect(self.rect(), QColor(0, 0, 0, int(255 * self.black ** 1.5)))

    def paint_overlays(self, p, w, h):
        if self.toast:
            text, exp = self.toast
            a = clamp((exp - time.perf_counter()) / 0.4, 0, 1)
            f = ui_font(14, QFont.Weight.DemiBold)
            tw_ = QFontMetrics(f).horizontalAdvance(text) + 44
            r = QRectF((w - tw_) / 2, h - 108, tw_, 38)
            p.save()
            p.setOpacity(a)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.setBrush(QColor(20, 22, 38, 235))
            p.drawRoundedRect(r, 19, 19)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 240))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            p.restore()
        if self.dropping:
            p.fillRect(self.rect(), QColor(5, 5, 11, 190))
            c = col(self.acc1)
            p.setBrush(alpha(c, 0.10))
            p.setPen(QPen(alpha(c.lighter(130), 0.9), 3, Qt.PenStyle.DashLine))
            p.drawRoundedRect(QRectF(28, 28, w - 56, h - 56), 28, 28)
            p.setFont(ui_font(30, QFont.Weight.Bold))
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "Drop .exe files to add them")


# ====================================================================== main
IPC_NAME = "exehub-single-instance-v4"


def single_instance():
    """Second launch just wakes the first one (two copies fighting over games.json was a data-loss risk)."""
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return False, None
    QLocalServer.removeServer(IPC_NAME)
    srv = QLocalServer()
    srv.listen(IPC_NAME)
    return True, srv


_fault_file = None


def main():
    global _fault_file
    try:
        _fault_file = open(CFG / "native-crash.log", "a")
        faulthandler.enable(file=_fault_file)
    except Exception:
        pass

    def hook(t, v, tb):
        log("UNHANDLED:\n" + "".join(traceback.format_exception(t, v, tb)))

    sys.excepthook = hook
    threading.excepthook = lambda a: log("THREAD ERROR:\n" + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))

    try:  # own taskbar identity so our icon shows instead of python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("exehub.launcher")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)       # hiding the hub while a game runs must not quit the app
    app.setStyleSheet(QSS)
    app.setWindowIcon(QIcon(resource("exehub.ico")))
    first, srv = single_instance()
    if not first:
        return 0
    win = Hub(srv)
    win.enter_fullscreen()
    code = app.exec()
    # everything is already saved; leave immediately so a background thread can never keep the process alive
    try:
        log("exited cleanly")
        if _fault_file:
            _fault_file.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
