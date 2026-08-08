#!/usr/bin/env python3
"""
hyprnyang - a pixel cat that lives on your Hyprland desktop.

Requires (Arch):  gtk3 gtk-layer-shell python-gobject python-cairo libnotify
Run:              hyprnyang
Autostart:        exec-once = hyprnyang     # in ~/.config/hypr/hyprland.conf
Control it:       hyprnyang --send "say hello"   # bind this to a Hyprland key

Privacy: the input reader only counts key and scroll events so the cat can
react. Keycodes are never stored, logged or sent anywhere.
"""

import argparse
import atexit
import errno
import json
import math
import os
import random
import select
import socket
import struct
import subprocess
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

VERSION = "0.3.0"
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "hyprnyang"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "hyprnyang.sock")
OVERHEAT_WPM = 100.0

DEFAULTS = {
    "name": "",
    "scale": 6,
    "anchor": "bottom-left",
    "margin_x": 24,
    "margin_y": 24,
    "layer": "overlay",
    "coat": "orange",
    "pattern": "plain",
    "follow_cursor": True,
    "stretch_minutes": 45,
    "water_minutes": 0,
    "pomodoro_focus": 25,
    "pomodoro_break": 5,
    "pomodoro_rounds": 4,
    "hide_on_fullscreen": False,
    "peek_on_fullscreen": True,
    "read_input": True,
    "knead_on_typing": True,
    "overheat_wpm": 100,         # realistic: a fast typist sits around 60-90
    "show_keyboard": True,       # tiny keyboard the cat kneads on while you type
    "show_wpm": True,            # live wpm meter above the keyboard
    "click_through": True,       # only the cat itself catches clicks
    "hunt_cursor": True,
    "pet_radius": 90,
    "draggable": True,
    "sleep_after_minutes": 1,
    "sound": True,
    "meow_sound": os.path.join(CONFIG_DIR, "meow.wav"),
    "pin_message": "",
    "reminders": [],
    "watch_processes": [
        "claude",
        "codex",
        "opencode",
        "opencode-tui",
        "cursor-agent",
        "aider",
        "gemini",
    ],
    "agent_reactions": True,
}

COATS = {
    "orange": {"B": (0.95, 0.58, 0.20), "D": (0.80, 0.40, 0.12), "L": (1.00, 0.76, 0.45)},
    "grey": {"B": (0.62, 0.64, 0.70), "D": (0.44, 0.46, 0.52), "L": (0.80, 0.82, 0.87)},
    "black": {"B": (0.24, 0.24, 0.30), "D": (0.15, 0.15, 0.20), "L": (0.38, 0.38, 0.46)},
    "white": {"B": (0.93, 0.93, 0.95), "D": (0.78, 0.78, 0.82), "L": (1.00, 1.00, 1.00)},
    "calico": {"B": (0.95, 0.62, 0.30), "D": (0.30, 0.26, 0.26), "L": (1.00, 0.95, 0.90)},
    "cream": {"B": (0.97, 0.88, 0.74), "D": (0.83, 0.70, 0.53), "L": (1.00, 0.97, 0.90)},
    "blue": {"B": (0.58, 0.70, 0.85), "D": (0.38, 0.50, 0.68), "L": (0.80, 0.88, 0.98)},
}

PALETTE = {
    ".": None,
    "O": (0.10, 0.09, 0.13),          # outline
    "P": (1.00, 0.68, 0.72),          # pink (nose / inner ear)
    "W": (1.00, 1.00, 1.00),          # eye white
    "S": (0.97, 0.97, 0.99),          # snow (pattern white)
}

# 20 wide x 16 tall. B/D/L are replaced by the coat palette.
BODY = [
    "....OO..........OO..",
    "...OBBO........OBBO.",
    "..OBPBBO......OBBPBO",
    "..OBBBBBOOOOOOBBBBBO",
    "..OBBBBBBBBBBBBBBBBO",
    "..OBLLBBBBBBBBLLBBBO",
    "..OBLWWBBBBBBLWWBBBO",
    "..OBBBBBBPPBBBBBBBBO",
    "..OBBBBBBBBBBBBBBBBO",
    "..OBBLLLBBBBBBLLLBBO",
    "...OBBBBBBBBBBBBBBO.",
    "...OBBBBBBBBBBBBBBO.",
    "...OBBDDBBBBDDBBBBO.",
    "...OBBDDBBBBDDBBBBO.",
    "...OOOOOOOOOOOOOOOO.",
    "....................",
]

GRID_W = len(BODY[0])
GRID_H = len(BODY)

EYE_SLOTS = [(6, 5), (6, 13)]  # (row, col) of each eye's white pixel block start

# Extra room around the sprite for particles, paws and the speech bubble.
PAD_X = 6
PAD_TOP = 9
PAD_BOTTOM = 6   # room under the cat for the keyboard + wpm meter

MOUTH = (7, 9)  # row/col of the nose, used to anchor mouth drawing

# the little keyboard the cat kneads on: keys per row, laid out under the paws
KEYBOARD_ROWS = (7, 7, 5)
KEYBOARD_KEYS = sum(KEYBOARD_ROWS)


def apply_pattern(grid, pattern):
    """Return a copy of the sprite grid with coat markings painted in."""
    rows = [list(line) for line in grid]

    def paint(row, cols, char):
        if 0 <= row < len(rows):
            for col in cols:
                if 0 <= col < len(rows[row]) and rows[row][col] in ("B", "D", "L"):
                    rows[row][col] = char

    if pattern == "tabby":
        for row in (4, 8, 11):
            paint(row, range(7, 14, 2), "D")
        paint(3, range(8, 14, 2), "D")
    elif pattern == "tuxedo":
        for row in (7, 8, 9, 10, 11):
            paint(row, range(8, 12), "S")
        paint(12, range(3, 6), "S")
        paint(13, range(3, 6), "S")
        paint(12, range(12, 15), "S")
        paint(13, range(12, 15), "S")
    elif pattern == "siamese":
        for row in (1, 2, 3):
            paint(row, list(range(3, 8)) + list(range(13, 19)), "D")
        paint(7, range(7, 13), "D")
        paint(13, range(3, 7), "D")
        paint(13, range(12, 18), "D")
    elif pattern == "spotted":
        for row, col in ((5, 8), (5, 12), (9, 7), (9, 13), (11, 10)):
            paint(row, (col, col + 1), "D")
            paint(row + 1, (col, col + 1), "D")
    elif pattern == "socks":
        for row in (12, 13):
            paint(row, (3, 4, 5, 12, 13, 14), "S")

    return ["".join(row) for row in rows]


def load_config():
    cfg = dict(DEFAULTS)
    try:
        import tomllib

        with open(CONFIG_PATH, "rb") as fh:
            user = tomllib.load(fh)
        for key, value in user.items():
            if key in cfg:
                cfg[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:  # bad config should never stop the cat
        print("hyprnyang: could not read config:", exc, file=sys.stderr)
    state = load_state()
    if state.get("coat") in COATS:
        cfg["coat"] = state["coat"]
    if state.get("anchor") == cfg["anchor"]:
        for key in ("margin_x", "margin_y"):
            value = state.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                cfg[key] = int(value)
    return cfg


def load_state():
    """Load the companion's last user-selected appearance and position."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print("hyprnyang: could not read state:", exc, file=sys.stderr)
        return {}


def save_state(state):
    """Atomically persist small UI state without rewriting the user's config."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        temporary = STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
        os.replace(temporary, STATE_PATH)
    except Exception as exc:
        print("hyprnyang: could not save state:", exc, file=sys.stderr)


DEFAULT_CONFIG_TEXT = """\
# hyprnyang config - edit and the running cat picks it up within a second

name = ""              # cat calls you by this name in reminders

# ---- looks -----------------------------------------------------------------
scale = 6              # pixel size
coat = "orange"        # orange | grey | black | white | calico | cream | blue
pattern = "plain"      # plain | tabby | tuxedo | siamese | spotted | socks
anchor = "bottom-left" # bottom-left | bottom-right | top-left | top-right
margin_x = 24
margin_y = 24
layer = "overlay"      # overlay | top | bottom | background

# ---- reactions -------------------------------------------------------------
follow_cursor = true       # eyes track the pointer
hunt_cursor = true         # lunges when you sling the pointer around
pet_radius = 90            # px: pointer this close to the head = purring
draggable = true           # drag with the left button, mochi-stretch, shake to wobble
read_input = true          # count key/scroll events from /dev/input (needs `input` group)
knead_on_typing = true     # tiny paws knead the keyboard while you type
overheat_wpm = 100         # above this typing speed the cat overheats and steams
show_keyboard = true       # tiny keyboard appears under the paws while typing
show_wpm = true            # live words-per-minute meter while typing
click_through = true       # clicks pass through everywhere except the cat
sleep_after_minutes = 1    # doze off after this long with no input (0 disables)
peek_on_fullscreen = true  # slide to the screen edge instead of vanishing
hide_on_fullscreen = false # true = disappear completely on fullscreen windows

# ---- reminders -------------------------------------------------------------
stretch_minutes = 45   # 0 disables
water_minutes = 0      # 0 disables
pomodoro_focus = 25
pomodoro_break = 5
pomodoro_rounds = 4    # rounds before the cat calls it a day

pin_message = ""       # pinned note that floats above the cat's head

# timed message reminders, the cat meows and says them
# [[reminders]]
# at = "14:30"
# text = "standup"

# ---- AI agent watching -----------------------------------------------------
agent_reactions = true
watch_processes = ["claude", "codex", "opencode", "opencode-tui", "cursor-agent", "aider", "gemini"]

# ---- sound -----------------------------------------------------------------
sound = true                                     # meow out loud on reminders and pets
meow_sound = "~/.config/hyprnyang/meow.wav"      # any .wav; installed by the installer
# played with paplay, pw-play or aplay - whichever you have
"""


def write_default_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        print("config already exists:", CONFIG_PATH)
        return
    with open(CONFIG_PATH, "w") as fh:
        fh.write(DEFAULT_CONFIG_TEXT)
    print("wrote", CONFIG_PATH)


def hypr_socket_path(name):
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    candidates = [
        os.path.join(runtime, "hypr", sig, name),
        os.path.join("/tmp/hypr", sig, name),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def hypr_cmd(command):
    """Send a command over the Hyprland control socket. Returns text or None."""
    path = hypr_socket_path(".socket.sock")
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            sock.connect(path)
            sock.sendall(command.encode())
            chunks = []
            while True:
                data = sock.recv(8192)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks).decode(errors="replace")
    except OSError:
        return None


def notify(title, body, name=""):
    if name:
        body = "%s, %s" % (name, body)
    try:
        subprocess.Popen(
            ["notify-send", "-a", "hyprnyang", "-i", "face-smile", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("%s: %s" % (title, body))


def play_sound(path):
    path = os.path.expanduser(str(path))
    if not os.path.exists(path):
        return
    for player in (["paplay", path], ["pw-play", path], ["aplay", "-q", path]):
        try:
            subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


# ---------------------------------------------------------------------------
# raw input counting
# ---------------------------------------------------------------------------

EVENT_STRUCT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_STRUCT)
EV_KEY, EV_REL = 0x01, 0x02
REL_WHEEL, REL_HIRES_WHEEL = 0x08, 0x0B


class InputCounter(threading.Thread):
    """Counts keystrokes and wheel ticks from /dev/input. Never stores keycodes."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.keys = 0
        self.scrolls = 0
        self.clicks = 0
        self.last_key = 0.0
        self.last_scroll = 0.0
        self.available = False
        self.reason = ""
        self._lock = threading.Lock()

    def _open_devices(self):
        fds = {}
        denied = False
        try:
            names = sorted(n for n in os.listdir("/dev/input") if n.startswith("event"))
        except OSError as exc:
            self.reason = str(exc)
            return fds
        for name in names:
            path = os.path.join("/dev/input", name)
            try:
                fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError as exc:
                denied = denied or exc.errno in (errno.EACCES, errno.EPERM)
        if not fds:
            self.reason = (
                "no readable /dev/input devices - add yourself to the `input` group "
                "(sudo usermod -aG input $USER) and log back in"
                if denied
                else "no input devices found"
            )
        return fds

    def run(self):
        while True:
            fds = self._open_devices()
            if not fds:
                self.available = False
                time.sleep(5)
                continue
            self.available = True
            try:
                while fds:
                    ready, _, _ = select.select(list(fds), [], [], 0.5)
                    for fd in ready:
                        try:
                            data = os.read(fd, EVENT_SIZE * 32)
                        except OSError:
                            os.close(fd)
                            fds.pop(fd, None)
                            continue
                        self._consume(data)
            finally:
                for fd in list(fds):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            time.sleep(2)

    def _consume(self, data):
        now = time.time()
        keys = scrolls = clicks = 0
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, etype, code, value = struct.unpack(
                EVENT_STRUCT, data[offset : offset + EVENT_SIZE]
            )
            if etype == EV_KEY and value == 1:
                if code >= 0x110:      # BTN_* - mouse and tablet buttons
                    clicks += 1
                elif 1 <= code <= 248:  # keyboard rows only, code is discarded
                    keys += 1
            elif etype == EV_REL and code in (REL_WHEEL, REL_HIRES_WHEEL) and value:
                scrolls += 1
        if not (keys or scrolls or clicks):
            return
        with self._lock:
            self.keys += keys
            self.scrolls += scrolls
            self.clicks += clicks
            if keys:
                self.last_key = now
            if scrolls:
                self.last_scroll = now

    def drain(self):
        with self._lock:
            counts = (self.keys, self.scrolls, self.clicks)
            self.keys = self.scrolls = self.clicks = 0
        return counts


class CursorTracker(threading.Thread):
    """Polls the Hyprland cursor off the main loop so drawing never stalls."""

    daemon = True

    def __init__(self, interval=0.08):
        super().__init__()
        self.interval = interval
        self.enabled = True
        self.pos = None          # (x, y, timestamp)
        self.speed = 0.0
        self._lock = threading.Lock()

    def run(self):
        while True:
            if not self.enabled:
                time.sleep(0.3)
                continue
            raw = hypr_cmd("cursorpos")
            if raw and "," in raw:
                try:
                    cx, cy = [int(float(part.strip())) for part in raw.split(",")[:2]]
                except ValueError:
                    cx = None
                if cx is not None:
                    now = time.time()
                    with self._lock:
                        if self.pos:
                            px_, py_, stamp = self.pos
                            span = max(0.016, now - stamp)
                            speed = math.hypot(cx - px_, cy - py_) / span
                            # smooth so one jittery sample can't trigger a hunt
                            self.speed = self.speed * 0.5 + speed * 0.5
                        self.pos = (cx, cy, now)
            time.sleep(self.interval)

    def read(self):
        with self._lock:
            return self.pos, self.speed


class ControlServer(threading.Thread):
    """Tiny unix socket so `hyprnyang --send ...` can poke the running cat."""

    daemon = True

    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def run(self):
        # a stale socket from a crashed cat would block bind()
        if os.path.exists(SOCKET_PATH):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(SOCKET_PATH)
                probe.close()
                print("hyprnyang: another cat is already running", file=sys.stderr)
                return
            except OSError:
                try:
                    os.unlink(SOCKET_PATH)
                except OSError:
                    pass
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(SOCKET_PATH)
            server.listen(4)
        except OSError as exc:
            print("hyprnyang: control socket unavailable:", exc, file=sys.stderr)
            return
        atexit.register(lambda: os.path.exists(SOCKET_PATH) and os.unlink(SOCKET_PATH))
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                try:
                    message = conn.recv(4096).decode(errors="replace").strip()
                except OSError:
                    continue
                if message:
                    GLib.idle_add(self.handler, message)
                    try:
                        conn.sendall(b"ok\n")
                    except OSError:
                        pass


def send_command(message):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(SOCKET_PATH)
            sock.sendall(message.encode())
            print(sock.recv(64).decode(errors="replace").strip() or "sent")
    except OSError as exc:
        print("hyprnyang: no cat is listening (%s)" % exc, file=sys.stderr)
        sys.exit(1)


class Particle:
    __slots__ = ("x", "y", "vx", "vy", "born", "life", "kind")

    def __init__(self, x, y, vx, vy, life, kind):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.born, self.life, self.kind = time.time(), life, kind

    @property
    def age(self):
        return (time.time() - self.born) / self.life


# mood priorities: higher wins while a mood is still playing
MOOD_RANK = {
    "idle": 0,
    "sleep": 0,
    "knead": 1,
    "purr": 2,
    "think": 2,
    "hop": 3,
    "hunt": 3,
    "stretch": 3,
    "overheat": 4,
    "drag": 5,
}


class Cat(Gtk.Window):
    def __init__(self, cfg):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.cfg = cfg
        self.frame = 0
        self.mood = "idle"          # idle hop stretch purr sleep knead overheat hunt think drag
        self.mood_until = 0.0
        self.blink_until = 0.0
        self.next_blink = time.time() + random.uniform(2, 6)
        self.look = (0.0, 0.0)
        self.look_target = (0.0, 0.0)
        self.hidden = False
        self.peeking = False
        self.pomodoro = None        # (phase, ends_at, round)
        self.next_stretch = self._schedule(cfg["stretch_minutes"])
        self.next_water = self._schedule(cfg["water_minutes"])
        self.config_mtime = self._config_mtime()
        self.grid = apply_pattern(BODY, str(cfg["pattern"]))

        self.wpm = 0.0
        self.key_window = []
        self.knead_phase = 0.0
        self.key_flashes = {}       # keyboard key index -> time it was hit
        self.keyboard_until = 0.0   # keyboard slides away shortly after typing
        self.keyboard_slide = 0.0   # 0 hidden, 1 fully out
        self.hover = False
        self.last_activity = time.time()
        self.particles = []
        self.bubble = ""
        self.bubble_until = 0.0
        self.wobble = 0.0
        self.drag_from = None
        self.dragging = False
        self.petting = False
        self.fired_reminders = set()
        self.known_agents = set()
        self.thinking_agent = None
        self.base_margin_y = int(cfg["margin_y"])

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.on_draw)
        self.add(self.area)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("button-press-event", self.on_click)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("scroll-event", self.on_scroll)
        self.connect("enter-notify-event", self.on_enter)
        self.connect("leave-notify-event", self.on_leave)

        self._init_layer_shell()
        self._apply_geometry()
        self.show_all()
        self._apply_input_region()

        self.input = InputCounter()
        if cfg["read_input"]:
            self.input.start()
        self.cursor_tracker = CursorTracker()
        self.cursor_tracker.enabled = bool(cfg["follow_cursor"])
        self.cursor_tracker.start()
        ControlServer(self.handle_command).start()

        GLib.timeout_add(33, self.tick)          # ~30 fps, smoother animation
        GLib.timeout_add(1000, self.slow_tick)
        threading.Thread(target=self.listen_hyprland, daemon=True).start()

    # ---------- setup ----------

    def _init_layer_shell(self):
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "hyprnyang")
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

    def _apply_geometry(self):
        layers = {
            "background": GtkLayerShell.Layer.BACKGROUND,
            "bottom": GtkLayerShell.Layer.BOTTOM,
            "top": GtkLayerShell.Layer.TOP,
            "overlay": GtkLayerShell.Layer.OVERLAY,
        }
        GtkLayerShell.set_layer(self, layers.get(self.cfg["layer"], layers["overlay"]))

        anchor = str(self.cfg["anchor"])
        edges = {
            "top": GtkLayerShell.Edge.TOP,
            "bottom": GtkLayerShell.Edge.BOTTOM,
            "left": GtkLayerShell.Edge.LEFT,
            "right": GtkLayerShell.Edge.RIGHT,
        }
        self._edges = edges
        for key, edge in edges.items():
            GtkLayerShell.set_anchor(self, edge, key in anchor)
        self.h_edge = "right" if "right" in anchor else "left"
        self.v_edge = "top" if "top" in anchor else "bottom"
        self.base_margin_y = int(self.cfg["margin_y"])
        self._set_margins(int(self.cfg["margin_x"]), self.base_margin_y)

        scale = max(2, int(self.cfg["scale"]))
        self.px = scale
        self.set_size_request(
            (GRID_W + PAD_X * 2) * scale,
            (GRID_H + PAD_TOP + PAD_BOTTOM) * scale,
        )
        self._apply_input_region()

    def _set_margins(self, mx, my):
        self.margin_x, self.margin_y = int(mx), int(my)
        GtkLayerShell.set_margin(self, self._edges[self.h_edge], self.margin_x)
        GtkLayerShell.set_margin(self, self._edges[self.v_edge], self.margin_y)

    def _apply_input_region(self):
        """Only the cat itself eats clicks - the padding stays click-through."""
        window = self.get_window()
        if window is None:
            return
        if not self.cfg["click_through"]:
            window.input_shape_combine_region(None, 0, 0)
            return
        px = self.px
        rect = cairo.RectangleInt(
            int((PAD_X - 1) * px),
            int((PAD_TOP - 1) * px),
            int((GRID_W + 2) * px),
            int((GRID_H + PAD_BOTTOM + 1) * px),
        )
        window.input_shape_combine_region(cairo.Region(rect), 0, 0)

    def _set_cursor(self, name):
        window = self.get_window()
        if window is None:
            return
        try:
            window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))
        except TypeError:
            pass

    def _schedule(self, minutes):
        return time.time() + minutes * 60 if minutes else None

    def _config_mtime(self):
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return 0

    def _save_companion_state(self):
        save_state(
            {
                "anchor": str(self.cfg["anchor"]),
                "margin_x": int(self.margin_x),
                "margin_y": int(self.base_margin_y),
                "coat": str(self.cfg["coat"]),
            }
        )

    # ---------- state ----------

    def set_mood(self, mood, seconds=1.4, priority=None):
        """Switch mood unless a higher-ranked mood is still playing."""
        rank = MOOD_RANK.get(mood, 0) if priority is None else priority
        if time.time() < self.mood_until and MOOD_RANK.get(self.mood, 0) > rank:
            return
        if self.mood == mood:
            # refresh, don't restart - keeps continuous moods (knead) smooth
            self.mood_until = max(self.mood_until, time.time() + seconds)
            return
        self.mood = mood
        self.mood_until = time.time() + seconds

    def say(self, text, seconds=4.5, meow=True):
        self.bubble = str(text)[:64]
        self.bubble_until = time.time() + seconds
        if meow and self.cfg["sound"]:
            play_sound(str(self.cfg["meow_sound"]))

    def spawn(self, kind, count=1):
        px = self.px
        for _ in range(count):
            if kind == "steam":
                self.particles.append(
                    Particle(
                        (random.uniform(6, 14) + PAD_X) * px,
                        (PAD_TOP - 1) * px,
                        random.uniform(-0.4, 0.4) * px,
                        -random.uniform(1.6, 2.6) * px,
                        1.1,
                        kind,
                    )
                )
            elif kind == "heart":
                self.particles.append(
                    Particle(
                        (random.uniform(7, 13) + PAD_X) * px,
                        (PAD_TOP + 1) * px,
                        random.uniform(-0.3, 0.3) * px,
                        -random.uniform(1.4, 2.2) * px,
                        1.4,
                        kind,
                    )
                )
            elif kind == "zzz":
                self.particles.append(
                    Particle(
                        (PAD_X + 15) * px,
                        (PAD_TOP + 2) * px,
                        random.uniform(0.4, 0.9) * px,
                        -random.uniform(0.8, 1.3) * px,
                        2.6,
                        kind,
                    )
                )
            elif kind == "sparkle":
                self.particles.append(
                    Particle(
                        (random.uniform(3, 17) + PAD_X) * px,
                        (PAD_TOP + random.uniform(0, 6)) * px,
                        random.uniform(-0.9, 0.9) * px,
                        -random.uniform(1.0, 2.2) * px,
                        0.9,
                        kind,
                    )
                )

    def on_click(self, _widget, event):
        if event.button == 3:
            self.toggle_pomodoro()
        elif event.button == 2:
            self.say("meow", 2.0)
            self.set_mood("hop", 1.2)
        elif event.type == Gdk.EventType._2BUTTON_PRESS:
            # double click: wake a sleeping cat, or ask it to nap
            if self.mood == "sleep":
                self.last_activity = time.time()
                self.mood_until = 0.0
                self.set_mood("hop", 1.2, priority=6)
                self.say("mrrp", 2.0)
            else:
                self.last_activity = 0.0
                self.say("nap time", 2.0, meow=False)
        else:
            if self.cfg["draggable"]:
                self.drag_from = (event.x_root, event.y_root, self.margin_x, self.margin_y)
                self._set_cursor("grabbing")
            self.set_mood("purr", 2.2)
            self.spawn("heart", 3)
        self.last_activity = time.time()
        return True

    def on_release(self, _widget, _event):
        if self.drag_from:
            self.drag_from = None
            self._set_cursor("grab" if self.cfg["draggable"] else "default")
            if self.dragging:
                self.dragging = False
                self.base_margin_y = self.margin_y
                self._save_companion_state()
                self.set_mood("stretch", 1.0, priority=6)
        return True

    def on_motion(self, _widget, event):
        if self.drag_from and self.cfg["draggable"]:
            sx, sy, mx, my = self.drag_from
            dx, dy = event.x_root - sx, event.y_root - sy
            if abs(dx) + abs(dy) < 3 and not self.dragging:
                return True          # ignore pointer jitter, a click stays a click
            self.dragging = True
            if self.h_edge == "right":
                dx = -dx
            if self.v_edge == "bottom":
                dy = -dy
            self._set_margins(max(0, mx + dx), max(0, my + dy))
            self.wobble = min(1.0, self.wobble + (abs(dx) + abs(dy)) * 0.002)
            self.set_mood("drag", 0.25, priority=5)
        self.last_activity = time.time()
        return True

    def on_enter(self, _widget, _event):
        self.hover = True
        self._set_cursor("grab" if self.cfg["draggable"] else "default")
        return False

    def on_leave(self, _widget, _event):
        self.hover = False
        return False

    def on_scroll(self, _widget, event):
        """Scroll on the cat to flip through coats, shift+scroll for patterns."""
        direction = event.direction
        step = 0
        if direction == Gdk.ScrollDirection.UP:
            step = -1
        elif direction == Gdk.ScrollDirection.DOWN:
            step = 1
        elif direction == Gdk.ScrollDirection.SMOOTH:
            _, _, dy = event.get_scroll_deltas()
            step = 1 if dy > 0 else -1 if dy < 0 else 0
        if not step:
            return True
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            names = ["plain", "tabby", "tuxedo", "siamese", "spotted", "socks"]
            current = str(self.cfg["pattern"])
            index = names.index(current) if current in names else 0
            self.cfg["pattern"] = names[(index + step) % len(names)]
            self.grid = apply_pattern(BODY, self.cfg["pattern"])
            self.say(self.cfg["pattern"], 1.6, meow=False)
        else:
            names = list(COATS)
            current = str(self.cfg["coat"])
            index = names.index(current) if current in names else 0
            self.cfg["coat"] = names[(index + step) % len(names)]
            self._save_companion_state()
            self.say(self.cfg["coat"], 1.6, meow=False)
        self.spawn("sparkle", 3)
        self.last_activity = time.time()
        return True

    def toggle_pomodoro(self):
        if self.pomodoro:
            self.pomodoro = None
            self.say("break it is", 3.0)
            notify("pomodoro cancelled", "back to whatever you were doing.")
            return
        minutes = int(self.cfg["pomodoro_focus"])
        self.pomodoro = ("focus", time.time() + minutes * 60, 1)
        self.say("focus!", 3.0)
        notify("focus started", "%d minutes. i'm watching." % minutes, self.cfg["name"])

    def handle_command(self, message):
        parts = message.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "say":
            self.say(arg or "meow")
            self.set_mood("hop", 1.0)
        elif cmd == "pin":
            self.cfg["pin_message"] = arg
        elif cmd == "pet":
            self.set_mood("purr", 2.5, priority=6)
            self.spawn("heart", 4)
        elif cmd == "hop":
            self.set_mood("hop", 1.4, priority=6)
        elif cmd == "stretch":
            self.set_mood("stretch", 4.0, priority=6)
        elif cmd == "sleep":
            self.last_activity = 0.0
            self.set_mood("sleep", 3600, priority=6)
        elif cmd == "wake":
            self.last_activity = time.time()
            self.mood_until = 0.0
            self.set_mood("hop", 1.0, priority=6)
        elif cmd == "coat" and arg in COATS:
            self.cfg["coat"] = arg
            self._save_companion_state()
        elif cmd == "pattern":
            self.cfg["pattern"] = arg
            self.grid = apply_pattern(BODY, arg)
        elif cmd == "pomodoro":
            if arg.isdigit():
                self.cfg["pomodoro_focus"] = int(arg)
            self.pomodoro = None
            self.toggle_pomodoro()
        elif cmd == "reload":
            self.cfg = load_config()
            self.grid = apply_pattern(BODY, str(self.cfg["pattern"]))
            self.cursor_tracker.enabled = bool(self.cfg["follow_cursor"])
            self._apply_geometry()
        elif cmd == "quit":
            Gtk.main_quit()
        return False

    def listen_hyprland(self):
        """React to compositor events over the Hyprland event socket."""
        while True:
            path = hypr_socket_path(".socket2.sock")
            if not path:
                time.sleep(3)
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(path)
                    buf = b""
                    while True:
                        data = sock.recv(4096)
                        if not data:
                            break
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            GLib.idle_add(self.on_hypr_event, line.decode(errors="replace"))
            except OSError:
                time.sleep(2)

    def on_hypr_event(self, line):
        event, _, payload = line.partition(">>")
        payload = payload.strip()
        if event in ("workspace", "focusedmon", "activespecial"):
            self.set_mood("hop", 1.0)
        elif event == "fullscreen":
            self.set_fullscreen_state(payload == "1")
        elif event == "openwindow":
            self.set_mood("hop", 0.6)
            self.spawn("sparkle", 2)
        elif event == "closewindow":
            self.spawn("steam", 1)
        elif event == "urgent":
            self.say("something wants you", 4.0)
            self.set_mood("hop", 1.5)
        return False

    def set_fullscreen_state(self, is_fullscreen):
        if self.cfg["hide_on_fullscreen"]:
            self.hidden = is_fullscreen
            self.set_visible(not is_fullscreen)
            return
        if not self.cfg["peek_on_fullscreen"] or self.peeking == is_fullscreen:
            return
        # peek mode: tuck most of the body past the screen edge, then come back
        # to wherever the cat actually is (dragging included)
        self.peeking = is_fullscreen
        body = (GRID_H + PAD_BOTTOM) * self.px
        target = self.base_margin_y - int(body * 0.62) if is_fullscreen else self.base_margin_y
        self._set_margins(self.margin_x, target)

    # ---------- loops ----------

    def tick(self):
        self.frame += 1
        now = time.time()
        dt = 1.0 / 30.0

        self.read_input_counters(now, dt)

        if self.mood != "idle" and now > self.mood_until:
            self.mood = "idle"
        if now > self.next_blink and self.mood != "sleep":
            self.blink_until = now + 0.14
            self.next_blink = now + random.uniform(2.5, 7.0)

        if self.cfg["follow_cursor"]:
            self.update_cursor(now)
        # ease the gaze instead of snapping - reads far nicer
        lx, ly = self.look
        tx, ty = self.look_target
        self.look = (lx + (tx - lx) * 0.25, ly + (ty - ly) * 0.25)

        idle_minutes = float(self.cfg["sleep_after_minutes"] or 0)
        if idle_minutes and now - self.last_activity > idle_minutes * 60:
            if self.mood in ("idle", "sleep"):
                self.mood = "sleep"
                self.mood_until = now + 5
                if self.frame % 70 == 0:
                    self.spawn("zzz")

        if self.mood == "overheat" and self.frame % 3 == 0:
            self.spawn("steam", 2)
        if self.mood == "purr" and self.frame % 14 == 0:
            self.spawn("heart")

        # particle physics, frame-rate independent and scaled to pixel size
        alive = []
        gravity = self.px * 6.0 * dt
        for particle in self.particles:
            if particle.age < 1.0:
                particle.x += particle.vx * dt * 6
                particle.y += particle.vy * dt * 6
                particle.vy += gravity
                alive.append(particle)
        self.particles = alive[-60:]

        self.wobble *= 0.88

        self.area.queue_draw()
        return True

    def read_input_counters(self, now, dt):
        keys, scrolls, clicks = self.input.drain()
        if keys or scrolls or clicks:
            self.last_activity = now
        if keys:
            self.key_window.extend([now] * keys)
            # light up a few keys on the little keyboard, one per keystroke
            for _ in range(min(keys, 4)):
                self.key_flashes[random.randrange(KEYBOARD_KEYS)] = now
            self.keyboard_until = now + 2.5
        # fade out old key flashes so the dict can't grow forever
        self.key_flashes = {
            index: stamp for index, stamp in self.key_flashes.items() if now - stamp < 0.35
        }
        target = 1.0 if now < self.keyboard_until and self.cfg["show_keyboard"] else 0.0
        self.keyboard_slide += (target - self.keyboard_slide) * 0.18
        window = 3.0
        self.key_window = [stamp for stamp in self.key_window if now - stamp < window]
        # 5 keystrokes = 1 word, scaled from the sample window up to a minute
        instant = (len(self.key_window) / 5.0) * (60.0 / window)
        # smooth so the cat doesn't flip moods on every keypress
        self.wpm += (instant - self.wpm) * 0.15

        if scrolls:
            self.set_mood("hunt", 0.5)

        # This is deliberately fixed at 70 WPM. Older config files could retain
        # the former 260 WPM value and silently delay the animation.
        if self.wpm >= OVERHEAT_WPM:
            was_overheating = self.mood == "overheat"
            self.set_mood("overheat", 2.2, priority=7)
            if not was_overheating:
                self.spawn("steam", 6)
        elif self.cfg["knead_on_typing"] and self.wpm >= 12:
            # hold the knead as long as typing continues, so the paws keep a
            # single continuous rhythm instead of restarting every keystroke
            self.set_mood("knead", 0.9)
            self.knead_phase += dt * math.tau * min(3.5, 1.2 + self.wpm / 45.0)

    def window_center(self):
        """Absolute screen position of the cat's centre, in monitor pixels."""
        width, height = self.get_size()
        if not width or not height:
            width = (GRID_W + PAD_X * 2) * self.px
            height = (GRID_H + PAD_TOP + PAD_BOTTOM) * self.px
        mon_w, mon_h = self.monitor_size()
        if self.h_edge == "right":
            x = mon_w - self.margin_x - width / 2
        else:
            x = self.margin_x + width / 2
        if self.v_edge == "top":
            y = self.margin_y + height / 2
        else:
            y = mon_h - self.margin_y - height / 2
        return x, y, width, height

    def update_cursor(self, now):
        pos, speed = self.cursor_tracker.read()
        if not pos:
            return
        cx, cy, _stamp = pos
        x, y, width, height = self.window_center()

        # eyes look toward the pointer: normalise against a comfortable range so
        # small pointer moves still register instead of snapping to the extremes
        head_x = x
        head_y = y - height * 0.18
        dx, dy = cx - head_x, cy - head_y
        span_x = max(240.0, width * 2.0)
        span_y = max(200.0, height * 1.6)
        self.look_target = (
            max(-1.0, min(1.0, dx / span_x)),
            max(-1.0, min(1.0, dy / span_y)),
        )

        head = math.hypot(dx, dy)
        was_petting = self.petting
        self.petting = head < float(self.cfg["pet_radius"])
        if self.petting:
            self.last_activity = now
            self.set_mood("purr", 0.8)
            if not was_petting:
                self.spawn("heart", 2)
        elif (
            self.cfg["hunt_cursor"]
            and speed > 1400
            and math.hypot(cx - x, cy - y) < 900
            and self.mood in ("idle", "sleep", "knead")
        ):
            self.set_mood("hunt", 1.1)
            self.spawn("sparkle", 2)

    def slow_tick(self):
        now = time.time()
        name = self.cfg["name"]

        if self.next_stretch and now > self.next_stretch:
            self.set_mood("stretch", 4.0, priority=6)
            self.say("stretch with me", 5.0)
            notify("stretch break", "stand up and stretch with me.", name)
            self.next_stretch = self._schedule(self.cfg["stretch_minutes"])

        if self.next_water and now > self.next_water:
            self.set_mood("hop", 2.0, priority=6)
            self.say("water time", 5.0)
            notify("water", "go drink something. meow.", name)
            self.next_water = self._schedule(self.cfg["water_minutes"])

        self.check_reminders(now, name)
        if self.cfg["agent_reactions"]:
            self.check_agents(name)

        if self.pomodoro:
            phase, ends, rnd = self.pomodoro
            if now > ends:
                if phase == "focus":
                    if rnd >= max(1, int(self.cfg["pomodoro_rounds"])):
                        self.pomodoro = None
                        self.set_mood("stretch", 5.0, priority=6)
                        self.say("all rounds done", 6.0)
                        notify("pomodoro finished", "%d rounds done. go outside." % rnd, name)
                    else:
                        self.pomodoro = ("break", now + int(self.cfg["pomodoro_break"]) * 60, rnd)
                        self.set_mood("stretch", 4.0, priority=6)
                        self.say("break time", 5.0)
                        notify(
                            "break time", "%d minutes off." % int(self.cfg["pomodoro_break"]), name
                        )
                else:
                    self.pomodoro = ("focus", now + int(self.cfg["pomodoro_focus"]) * 60, rnd + 1)
                    self.set_mood("hop", 1.5, priority=6)
                    self.say("round %d" % (rnd + 1), 4.0)
                    notify("back to focus", "%d minutes." % int(self.cfg["pomodoro_focus"]), name)

        mtime = self._config_mtime()
        if mtime and mtime != self.config_mtime:
            self.config_mtime = mtime
            self.cfg = load_config()
            self.grid = apply_pattern(BODY, str(self.cfg["pattern"]))
            self.next_stretch = self._schedule(self.cfg["stretch_minutes"])
            self.next_water = self._schedule(self.cfg["water_minutes"])
            self.cursor_tracker.enabled = bool(self.cfg["follow_cursor"])
            self._apply_geometry()
        return True

    def check_reminders(self, now, name):
        stamp = time.strftime("%H:%M", time.localtime(now))
        if stamp in self.fired_reminders:
            return
        for entry in self.cfg["reminders"] or []:
            try:
                at, text = str(entry.get("at", "")), str(entry.get("text", ""))
            except AttributeError:
                continue
            if at == stamp:
                self.fired_reminders.add(stamp)
                self.set_mood("hop", 2.0, priority=6)
                self.say(text or "reminder", 8.0)
                notify("reminder", text or at, name)
        if len(self.fired_reminders) > 64:
            self.fired_reminders.clear()

    def running_agents(self):
        """Names of watched AI CLI processes that are currently running."""
        wanted = {str(n).lower() for n in (self.cfg["watch_processes"] or [])}
        found = set()
        if not wanted:
            return found
        try:
            pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
        except OSError:
            return found
        for pid in pids:
            try:
                with open("/proc/%s/comm" % pid) as fh:
                    comm = fh.read().strip().lower()
            except OSError:
                continue
            if comm in wanted:
                found.add(comm)
                continue
            # comm is truncated to 15 chars and node-based CLIs report "node",
            # so fall back to the command line
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    argv = fh.read().decode(errors="replace").split("\0")
            except OSError:
                continue
            for part in argv[:3]:
                base = os.path.basename(part.strip()).lower()
                if base in wanted:
                    found.add(base)
                    break
        return found

    @staticmethod
    def agent_label(agent):
        """Pretty name for a watched process (opencode-tui -> opencode)."""
        return agent[:-4] if agent.endswith("-tui") else agent

    def check_agents(self, name):
        current = self.running_agents()
        started = current - self.known_agents
        finished = self.known_agents - current
        self.known_agents = current
        if started:
            agent = sorted(started)[0]
            self.thinking_agent = agent
            self.set_mood("think", 6.0)
            self.say("%s is thinking" % self.agent_label(agent), 4.0, meow=False)
        elif current:
            self.thinking_agent = sorted(current)[0]
            if self.mood in ("idle", "sleep"):
                self.set_mood("think", 3.0)
        else:
            self.thinking_agent = None
        if finished:
            self.set_mood("hop", 2.0, priority=6)
            self.spawn("sparkle", 6)
            self.say("%s is done" % self.agent_label(sorted(finished)[0]), 5.0)
            notify("agent done", "%s finished its task." % self.agent_label(sorted(finished)[0]), name)

    def monitor_size(self):
        now = time.time()
        cached = getattr(self, "_monitor_cache", None)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        size = (1920, 1080)
        raw = hypr_cmd("j/monitors")
        try:
            monitors = json.loads(raw)
            for mon in monitors:
                if mon.get("focused"):
                    size = (mon["width"], mon["height"])
                    break
            else:
                size = (monitors[0]["width"], monitors[0]["height"])
        except Exception:
            pass
        self._monitor_cache = (now, size)
        return size

    # ---------- drawing ----------

    def on_draw(self, _widget, ctx):
        ctx.set_operator(cairo.Operator.SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.Operator.OVER)

        px = self.px
        coat = dict(COATS.get(self.cfg["coat"], COATS["orange"]))
        now = time.time()
        mood = self.mood
        bob = 0.0
        squash = 1.0        # vertical scale, anchored at the feet
        stretch_x = 1.0
        lean = 0.0

        if mood == "hop":
            hop = abs(math.sin(now * 8))
            bob = -hop * px * 2.4
            squash = 1.0 + hop * 0.06
            stretch_x = 1.0 - hop * 0.04
        elif mood == "stretch":
            wave = (math.sin(now * 2.6) + 1) / 2
            squash = 1.0 + wave * 0.16
            stretch_x = 1.0 - wave * 0.06
        elif mood == "purr":
            bob = math.sin(now * 14) * px * 0.18
            squash = 1.0 + math.sin(now * 14) * 0.02
        elif mood == "knead":
            # gentle bounce locked to the paw rhythm, no full-body jitter
            bounce = abs(math.sin(self.knead_phase * 0.5))
            bob = -bounce * px * 0.35
            squash = 1.0 - bounce * 0.03
            stretch_x = 1.0 + bounce * 0.02
        elif mood == "overheat":
            bob = math.sin(now * 20) * px * 0.25
            for key in ("B", "D", "L"):
                r, g, b = coat[key]
                coat[key] = (min(1.0, r * 0.6 + 0.55), g * 0.45, b * 0.45)
        elif mood == "hunt":
            lean = self.look[0] * px * 1.8
            squash = 0.88
            stretch_x = 1.10
        elif mood == "drag":
            squash = 1.22
            stretch_x = 0.90
        elif mood == "think":
            bob = math.sin(now * 1.6) * px * 0.25
        elif mood == "sleep":
            breath = math.sin(now * 1.1)
            bob = breath * px * 0.35
            squash = 0.95 + breath * 0.015
        else:
            bob = math.sin(now * 2.2) * px * 0.3

        wobble = math.sin(now * 26) * self.wobble * px * 1.2
        center_x = (PAD_X + GRID_W / 2) * px + lean + wobble
        baseline = (PAD_TOP + GRID_H) * px + bob     # feet stay planted
        blinking = now < self.blink_until or mood == "sleep"

        def sx(col):
            return center_x + (col - GRID_W / 2) * px * stretch_x

        def sy(row):
            return baseline - (GRID_H - row) * px * squash

        self.draw_shadow(ctx, center_x, bob, stretch_x)

        cell_w = px * stretch_x + 0.6
        cell_h = px * squash + 0.6
        for row, line in enumerate(self.grid):
            y = sy(row)
            for col, key in enumerate(line):
                color = coat.get(key) or PALETTE.get(key)
                if color is None:
                    continue
                ctx.set_source_rgb(*color)
                ctx.rectangle(sx(col), y, cell_w, cell_h)
                ctx.fill()

        self.draw_face(ctx, sx, sy, stretch_x, squash, blinking, mood)
        self.draw_tail(ctx, sx, sy, coat, mood, now)
        keyboard_top = self.draw_keyboard(ctx, now)
        if mood in ("knead", "overheat") or self.keyboard_slide > 0.05:
            self.draw_paws(ctx, sx, sy, coat, now, mood, keyboard_top)
        self.draw_particles(ctx)
        self.draw_wpm(ctx, keyboard_top)
        self.draw_bubble(ctx, now)
        self.draw_timer(ctx, now)
        return False

    def draw_shadow(self, ctx, center_x, bob, stretch_x):
        """Soft ellipse under the cat so it sits on the desktop instead of floating."""
        px = self.px
        lift = max(0.0, -bob) / max(1.0, px)
        width = (GRID_W - 6) * px * stretch_x * (1.0 - lift * 0.12)
        ctx.save()
        ctx.translate(center_x, (PAD_TOP + GRID_H - 0.4) * px)
        ctx.scale(width / 2, px * 1.1)
        ctx.arc(0, 0, 1, 0, math.tau)
        ctx.restore()
        ctx.set_source_rgba(0.05, 0.04, 0.08, max(0.05, 0.22 - lift * 0.05))
        ctx.fill()

    def draw_keyboard(self, ctx, now):
        """The keyboard slides in while you type. Keys flash as they're hit."""
        slide = self.keyboard_slide
        if slide <= 0.02 or not self.cfg["show_keyboard"]:
            return None
        px = self.px
        board_w = (GRID_W - 4) * px
        board_h = px * 3.4
        x = (PAD_X + 2) * px
        hidden_y = (PAD_TOP + GRID_H + PAD_BOTTOM) * px
        y = hidden_y - (hidden_y - (PAD_TOP + GRID_H - 1.6) * px) * slide
        alpha = min(1.0, slide * 1.2)

        ctx.set_source_rgba(0.10, 0.09, 0.13, 0.85 * alpha)
        ctx.rectangle(x - px * 0.3, y - px * 0.3, board_w + px * 0.6, board_h + px * 0.6)
        ctx.fill()
        ctx.set_source_rgba(0.22, 0.22, 0.28, 0.95 * alpha)
        ctx.rectangle(x, y, board_w, board_h)
        ctx.fill()

        index = 0
        gap = px * 0.18
        row_h = (board_h - px * 0.5) / len(KEYBOARD_ROWS)
        for row, count in enumerate(KEYBOARD_ROWS):
            key_w = (board_w - px * 0.6 - gap * (count - 1)) / count
            row_x = x + px * 0.3 + (board_w - px * 0.6 - (key_w * count + gap * (count - 1))) / 2
            row_y = y + px * 0.25 + row * row_h
            for key in range(count):
                hit = self.key_flashes.get(index)
                heat = 0.0 if hit is None else max(0.0, 1.0 - (now - hit) / 0.35)
                if heat:
                    ctx.set_source_rgba(1.0, 0.82 - heat * 0.2, 0.45, alpha)
                else:
                    ctx.set_source_rgba(0.62, 0.64, 0.72, 0.9 * alpha)
                kx = row_x + key * (key_w + gap)
                ky = row_y + heat * px * 0.18      # keys visibly press down
                ctx.rectangle(kx, ky, key_w, row_h - gap)
                ctx.fill()
                index += 1
        return y

    def draw_wpm(self, ctx, keyboard_top=None):
        """Small live typing meter, sitting just below the keyboard."""
        if not self.cfg["show_wpm"] or self.keyboard_slide <= 0.15:
            return
        px = self.px
        alpha = min(1.0, self.keyboard_slide)
        wpm = int(self.wpm)
        ratio = max(0.0, min(1.0, self.wpm / OVERHEAT_WPM))
        label = "%d wpm" % wpm
        ctx.select_font_face("monospace")
        ctx.set_font_size(px * 1.2)
        extents = ctx.text_extents(label)
        bar_w = (GRID_W - 4) * px
        x = (PAD_X + 2) * px
        board_h = px * 3.4
        if keyboard_top is None:
            keyboard_top = (PAD_TOP + GRID_H - 1.6) * px
        y = keyboard_top + board_h + px * 0.6   # sits just below the keyboard

        ctx.set_source_rgba(0.10, 0.09, 0.13, 0.7 * alpha)
        ctx.rectangle(x, y, bar_w, px * 0.5)
        ctx.fill()
        if ratio >= 1.0:
            ctx.set_source_rgba(1.0, 0.45, 0.38, alpha)
        else:
            ctx.set_source_rgba(0.55, 0.86, 1.0, alpha)
        ctx.rectangle(x, y, bar_w * ratio, px * 0.5)
        ctx.fill()
        ctx.set_source_rgba(0.92, 0.94, 1.0, alpha)
        ctx.move_to(x + bar_w - extents.width, y + px * 0.5 + extents.height + px * 0.45)
        ctx.show_text(label)

    def draw_face(self, ctx, sx, sy, stretch_x, squash, blinking, mood):
        px = self.px
        w = px * stretch_x
        h = px * squash
        look_x, look_y = self.look
        for row, col in EYE_SLOTS:
            ex = sx(col)
            ey = sy(row) + h * 0.1
            if blinking:
                ctx.set_source_rgb(*PALETTE["O"])
                ctx.rectangle(ex - w * 0.1, ey + h * 0.45, w * 2.1, h * 0.4)
                ctx.fill()
                continue
            # eye white first, so the pupil can slide inside it
            ctx.set_source_rgb(*PALETTE["W"])
            ctx.rectangle(ex - w * 0.1, ey, w * 2.1, h * 1.3)
            ctx.fill()
            ctx.set_source_rgb(*PALETTE["O"])
            if mood == "hunt":
                ctx.rectangle(ex + w * 0.75 + look_x * w * 0.55, ey + h * 0.1, w * 0.5, h * 1.1)
            elif mood == "purr":
                ctx.rectangle(ex - w * 0.1, ey + h * 0.45, w * 2.1, h * 0.4)
            else:
                ctx.rectangle(
                    ex + w * 0.55 + look_x * w * 0.5,
                    ey + h * 0.25 + look_y * h * 0.35,
                    w * 0.9,
                    h * 0.8,
                )
            ctx.fill()

        mouth_x = sx(MOUTH[1])
        mouth_y = sy(MOUTH[0]) + h * 1.4
        ctx.set_source_rgb(*PALETTE["O"])
        ctx.set_line_width(max(1.0, px * 0.3))
        if mood in ("purr", "hop"):
            ctx.move_to(mouth_x - w * 0.6, mouth_y)
            ctx.curve_to(mouth_x, mouth_y + h * 0.6, mouth_x + w * 1.4,
                         mouth_y + h * 0.6, mouth_x + w * 2, mouth_y)
            ctx.stroke()
        elif mood == "overheat":
            ctx.rectangle(mouth_x, mouth_y, w * 1.4, h * 0.9)
            ctx.fill()
        elif mood == "knead":
            ctx.rectangle(mouth_x + w * 0.3, mouth_y, w * 0.9, h * 0.3)
            ctx.fill()
        elif mood == "think":
            ctx.rectangle(mouth_x + w * 0.2, mouth_y, w * 1.0, h * 0.35)
            ctx.fill()

    def draw_tail(self, ctx, sx, sy, coat, mood, now):
        px = self.px
        speed = {"hunt": 9.0, "overheat": 8.0, "knead": 5.0, "sleep": 1.0}.get(mood, 3.4)
        amount = {"hunt": 2.6, "sleep": 0.6}.get(mood, 1.6)
        swing = math.sin(now * speed) * px * amount
        ctx.set_source_rgb(*coat["B"])
        ctx.set_line_width(px)
        ctx.move_to(sx(18), sy(12))
        ctx.curve_to(
            sx(20), sy(10),
            sx(21) + swing, sy(8),
            sx(19) + swing, sy(5),
        )
        ctx.stroke()

    def draw_paws(self, ctx, sx, sy, coat, now, mood, keyboard_top=None):
        px = self.px
        phase = self.knead_phase if mood == "knead" else now * 14
        # rest the paws on the keyboard when it's out, otherwise on the floor
        base = (keyboard_top - px * 1.1) if keyboard_top else sy(GRID_H - 1)
        for index, col in enumerate((6, 12)):
            lift = (math.sin(phase + index * math.pi) * 0.5 + 0.5) * px * 1.3
            ctx.set_source_rgb(*coat["L"])
            ctx.rectangle(sx(col), base - lift, px * 2, px * 1.4)
            ctx.fill()
            ctx.set_source_rgb(*PALETTE["O"])
            ctx.set_line_width(max(1.0, px * 0.2))
            ctx.rectangle(sx(col), base - lift, px * 2, px * 1.4)
            ctx.stroke()
            ctx.set_source_rgb(*PALETTE["P"])
            ctx.rectangle(sx(col) + px * 0.5, base - lift + px * 0.35, px, px * 0.6)
            ctx.fill()

    def draw_particles(self, ctx):
        px = self.px
        for particle in self.particles:
            fade = max(0.0, 1.0 - particle.age)
            if particle.kind == "steam":
                ctx.set_source_rgba(0.85, 0.88, 0.94, fade * 0.8)
                size = px * (0.7 + particle.age * 1.2)
                ctx.arc(particle.x, particle.y, size, 0, math.tau)
                ctx.fill()
            elif particle.kind == "heart":
                ctx.set_source_rgba(1.0, 0.42, 0.55, fade)
                s = px * 0.55
                ctx.rectangle(particle.x, particle.y, s, s)
                ctx.rectangle(particle.x + s * 1.2, particle.y, s, s)
                ctx.rectangle(particle.x, particle.y + s, s * 2.2, s)
                ctx.rectangle(particle.x + s * 0.6, particle.y + s * 2, s, s)
                ctx.fill()
            elif particle.kind == "zzz":
                ctx.set_source_rgba(0.75, 0.80, 0.92, fade)
                ctx.select_font_face("monospace")
                ctx.set_font_size(px * (1.4 + particle.age))
                ctx.move_to(particle.x, particle.y)
                ctx.show_text("z")
            elif particle.kind == "sparkle":
                ctx.set_source_rgba(1.0, 0.92, 0.55, fade)
                s = px * 0.4
                ctx.rectangle(particle.x - s, particle.y, s * 3, s)
                ctx.rectangle(particle.x, particle.y - s, s, s * 3)
                ctx.fill()

    def draw_bubble(self, ctx, now):
        text = ""
        if now < self.bubble_until and self.bubble:
            text = self.bubble
        elif str(self.cfg["pin_message"]).strip():
            text = str(self.cfg["pin_message"]).strip()
        elif self.mood == "think" and self.thinking_agent:
            dots = "." * (1 + int(now * 2) % 3)
            text = dots
        if not text:
            return

        px = self.px
        ctx.select_font_face("monospace")
        ctx.set_font_size(px * 1.6)
        extents = ctx.text_extents(text)
        pad = px * 0.9
        width = extents.width + pad * 2
        height = px * 2.8
        total_w = (GRID_W + PAD_X * 2) * px
        x = min(max(px * 0.5, (PAD_X + GRID_W / 2) * px - width / 2), total_w - width - px * 0.5)
        y = px * 0.8

        radius = px * 0.6
        def rounded(x0, y0, w, h, r):
            ctx.new_sub_path()
            ctx.arc(x0 + w - r, y0 + r, r, -math.pi / 2, 0)
            ctx.arc(x0 + w - r, y0 + h - r, r, 0, math.pi / 2)
            ctx.arc(x0 + r, y0 + h - r, r, math.pi / 2, math.pi)
            ctx.arc(x0 + r, y0 + r, r, math.pi, 1.5 * math.pi)
            ctx.close_path()

        ctx.set_source_rgba(0.10, 0.09, 0.13, 0.92)
        rounded(x - 2, y - 2, width + 4, height + 4, radius)
        ctx.fill()
        ctx.set_source_rgba(0.98, 0.97, 0.99, 0.97)
        rounded(x, y, width, height, radius)
        ctx.fill()
        # little tail pointing at the cat's head
        tip = (PAD_X + GRID_W / 2) * px
        ctx.move_to(max(x + px, tip - px * 0.8), y + height - 1)
        ctx.line_to(tip, y + height + px)
        ctx.line_to(min(x + width - px, tip + px * 0.8), y + height - 1)
        ctx.close_path()
        ctx.fill()

        ctx.set_source_rgb(0.12, 0.11, 0.16)
        ctx.move_to(x + pad, y + height * 0.68)
        ctx.show_text(text)

    def draw_timer(self, ctx, now):
        if not self.pomodoro:
            return
        px = self.px
        phase, ends, rnd = self.pomodoro
        left_seconds = max(0, int(ends - now))
        label = "%02d:%02d" % (left_seconds // 60, left_seconds % 60)
        ctx.select_font_face("monospace")
        ctx.set_font_size(px * 1.9)
        extents = ctx.text_extents(label)
        box_w = extents.width + px * 3.4
        bx = (PAD_X + GRID_W / 2) * px - box_w / 2
        by = (PAD_TOP - 3.2) * px
        ctx.set_source_rgba(0.10, 0.09, 0.13, 0.85)
        ctx.rectangle(bx, by, box_w, px * 2.6)
        ctx.fill()
        if phase == "focus":
            ctx.set_source_rgb(0.55, 0.86, 1.0)
        else:
            ctx.set_source_rgb(0.62, 0.92, 0.78)
        ctx.move_to(bx + px * 0.7, by + px * 2)
        ctx.show_text(label)
        ctx.set_font_size(px * 1.2)
        ctx.move_to(bx + px * 0.7 + extents.width + px * 0.5, by + px * 2)
        ctx.show_text("%d" % rnd)


def main():
    parser = argparse.ArgumentParser(prog="hyprnyang", description="pixel cat for Hyprland")
    parser.add_argument("--init", action="store_true", help="write a default config file")
    parser.add_argument("--version", action="store_true", help="print version")
    parser.add_argument("--coat", help="override coat colour for this run")
    parser.add_argument("--pattern", help="override coat pattern for this run")
    parser.add_argument("--wpm", type=int, help="overheat threshold in words per minute")
    parser.add_argument("--pomodoro", type=int, help="start a focus block of N minutes")
    parser.add_argument("--no-input", action="store_true", help="do not read /dev/input")
    parser.add_argument(
        "--send",
        metavar="CMD",
        help="talk to a running cat: say <text> | pin <text> | pet | hop | stretch | "
        "sleep | wake | coat <name> | pattern <name> | pomodoro [n] | reload | quit",
    )
    args = parser.parse_args()

    if args.version:
        print("hyprnyang", VERSION)
        return
    if args.init:
        write_default_config()
        return
    if args.send:
        send_command(args.send)
        return

    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        print(
            "hyprnyang: no Hyprland instance detected - cursor tracking and workspace "
            "reactions will be disabled.",
            file=sys.stderr,
        )

    cfg = load_config()
    if args.coat:
        cfg["coat"] = args.coat
    if args.pattern:
        cfg["pattern"] = args.pattern
    if args.wpm:
        cfg["overheat_wpm"] = args.wpm
    if args.no_input:
        cfg["read_input"] = False

    cat = Cat(cfg)
    if args.pomodoro:
        cfg["pomodoro_focus"] = args.pomodoro
        cat.toggle_pomodoro()

    def input_hint():
        if cfg["read_input"] and not cat.input.available and cat.input.reason:
            print("hyprnyang:", cat.input.reason, file=sys.stderr)
        return False

    GLib.timeout_add_seconds(6, input_hint)
    Gtk.main()


if __name__ == "__main__":
    main()
