"""Interactive terminal UI ("mousectl tui"), one for every mouse.

Panels and fields are generated from the driver's Setting schema, so a new
driver gets a TUI for free. This layer never reimplements protocol logic:
edits go through Setting.assign on a local copy of the raw snapshot, and
Apply replays only the chosen group's edits onto the device's bytes (see
settings.replay), so pending edits in another group that share a unit stay
pending.

Device I/O stays synchronous, single-threaded and is only ever triggered by
an explicit keypress -- no timers, no background polling -- because these
firmwares are timing-sensitive (the X11 stalls its control endpoint when
rushed, the VT3 PRO dongle answers stale data). The one extra thread is the
screen-only spinner in widgets.run_with_spinner, always joined before any
further curses or device work.
"""

import curses

from ..core import store
from ..core.driver import DeviceError, discover
from ..core.settings import copy_raw, dirty_settings, replay
from ..drivers import DRIVERS
from .widgets import (C_ERROR, C_FOCUS, C_INFO, C_PENDING, SPINNER_FRAMES, confirm,
                      edit_line, init_colors, run_with_spinner, safe_addstr)

BATTERY = "Battery"
SIDEBAR_W = 24


class State:
    def __init__(self, mice):
        self.mice = mice
        self.mouse_idx = 0
        self.link_idx = 0
        self.device_raw = {}    # unit -> bytearray, as last read from / written to the device
        self.raw = {}           # local copy with pending edits
        self.battery = None     # (pct, state) | None
        self.battery_loading = False
        self.spinner_frame = SPINNER_FRAMES[0]
        self.message = "Reading device..."
        self.message_kind = "info"
        self.section = 0
        self.field = 0

    @property
    def mouse(self):
        return self.mice[self.mouse_idx]

    @property
    def driver(self):
        return self.mouse.driver

    @property
    def link(self):
        return self.mouse.links[self.link_idx]

    @property
    def mode(self):
        return self.link.mode

    @property
    def sections(self):
        return self.driver.groups + [BATTERY]

    @property
    def group(self):
        return self.sections[self.section]

    def fields(self, group=None):
        g = group or self.group
        return [s for s in self.driver.settings if s.group == g and not s.hidden and not s.read_only]

    @property
    def setting(self):
        f = self.fields()
        return f[self.field] if self.raw and self.field < len(f) else None

    def dirty(self, group=None):
        if not self.raw:
            return []
        return dirty_settings(self.driver.settings, self.device_raw, self.raw, self.mode, group)

    def say(self, msg, kind="info"):
        self.message, self.message_kind = msg, kind


# ------------------------------------------------------------------ edits

def nudge(state, d):
    s = state.setting
    if s is None:
        return
    raw, mode = state.raw, state.mode
    try:
        if not s.enabled(raw, mode):
            s.set_enabled(raw, mode, True)
            return
        v = s.get(raw, mode)
        # Some values can be refused by the device model (e.g. switching to
        # a disabled DPI stage): skip past them.
        for _ in range(len(getattr(s.kind, "values", [0])) or 1):
            v = s.kind.nudge(v, d)
            try:
                s.assign(raw, mode, v)
                return
            except ValueError as e:
                err = e
        state.say(f"{s.label}: {err}", "error")
    except ValueError as e:
        state.say(f"{s.label}: {e}", "error")


def toggle(state):
    s = state.setting
    if s is None or not s.set_enabled:
        state.say("This field can't be switched off.")
        return
    try:
        s.set_enabled(state.raw, state.mode, not s.enabled(state.raw, state.mode))
    except ValueError as e:
        state.say(str(e), "error")


def edit(stdscr, state, s):
    if s is None or s.read_only:
        return
    if not s.kind.typed:
        nudge(state, 1)
        return

    def validate(text):
        s.kind.parse(text)

    cur = s.get(state.raw, state.mode)
    val = edit_line(stdscr, f"{s.label} ({s.kind.describe()})", s.kind.text(cur), validate)
    if val is None:
        return
    try:
        s.assign(state.raw, state.mode, val)
    except ValueError as e:
        state.say(f"{s.label}: {e}", "error")


def edit_companion(stdscr, state):
    s = state.setting
    if s is None or not s.companion:
        state.say("No colour on this field.")
        return
    edit(stdscr, state, state.driver.setting(s.companion))


# ------------------------------------------------------- device operations

def refresh(stdscr, state, confirm_if_dirty=True):
    if confirm_if_dirty and state.dirty():
        if not confirm(stdscr, "Unapplied edits will be discarded. Refresh? [y/N] "):
            return
    state.raw = {}
    state.battery = None
    state.say("Reading device...")
    draw = lambda: render(stdscr, state)
    try:
        with state.link.open() as s:
            dev_raw = run_with_spinner(state, draw, s.read)
            state.device_raw = dev_raw
            state.raw = copy_raw(dev_raw)
            state.battery_loading = True
            state.say("Reading battery...")
            try:
                state.battery = run_with_spinner(state, draw, s.battery)
            except DeviceError:
                state.battery = None
            finally:
                state.battery_loading = False
        state.say("Refreshed.")
    except DeviceError as e:
        state.say(str(e), "error")


def apply(stdscr, state, groups, label):
    groups = [g for g in groups if state.dirty(g)]
    if not groups:
        state.say(f"Nothing to apply in {label}.")
        return
    state.say(f"Applying {label}...")

    def do():
        target = replay(state.driver.settings, state.device_raw, state.raw, state.mode, groups)
        with state.link.open() as s:
            store.apply(s, copy_raw(state.device_raw), target)
        state.device_raw = target

    try:
        run_with_spinner(state, lambda: render(stdscr, state), do)
        state.say(f"{label} applied.")
    except (DeviceError, ValueError) as e:
        state.say(str(e), "error")


def read_battery(stdscr, state):
    state.battery_loading = True
    state.say("Reading battery... (move/click the mouse if nothing appears)")
    try:
        with state.link.open() as s:
            state.battery = run_with_spinner(state, lambda: render(stdscr, state), s.battery)
        state.say("Battery updated." if state.battery else "No battery report -- try again.",
                  "info" if state.battery else "error")
    except DeviceError as e:
        state.say(str(e), "error")
    finally:
        state.battery_loading = False


def switch(stdscr, state, what):
    if what == "link" and len(state.mouse.links) < 2:
        state.say("Only one connection present -- nothing to switch to.")
        return
    if what == "mouse" and len(state.mice) < 2:
        state.say("Only one mouse connected.")
        return
    if state.dirty() and not confirm(stdscr, f"Unapplied edits will be discarded. Switch {what}? [y/N] "):
        return
    if what == "link":
        state.link_idx = (state.link_idx + 1) % len(state.mouse.links)
    else:
        state.mouse_idx = (state.mouse_idx + 1) % len(state.mice)
        state.link_idx = 0
        state.section = state.field = 0
    refresh(stdscr, state, confirm_if_dirty=False)


def rescan(stdscr, state, args):
    if state.dirty() and not confirm(stdscr, "Unapplied edits will be discarded. Rescan? [y/N] "):
        return
    mice = discover(DRIVERS)
    if getattr(args, "mouse", None):
        mice = [m for m in mice if m.driver is state.driver] or mice
    if not mice:
        state.say("No supported mouse found.", "error")
        return
    current = state.driver.id
    state.mice = mice
    state.mouse_idx = next((i for i, m in enumerate(mice) if m.driver.id == current), 0)
    state.link_idx = 0
    state.section = min(state.section, len(state.sections) - 1)
    state.field = 0
    refresh(stdscr, state, confirm_if_dirty=False)


# -------------------------------------------------------------------- draw

def render(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    x0 = 0
    if len(state.mice) > 1:
        x0 = SIDEBAR_W + 2
        safe_addstr(stdscr, 0, 0, "Mice (m)", curses.A_BOLD)
        for i, m in enumerate(state.mice):
            cur = i == state.mouse_idx
            safe_addstr(stdscr, 2 + i * 2, 0, (("> " if cur else "  ") + m.name)[:SIDEBAR_W],
                        curses.A_BOLD if cur else 0)
            safe_addstr(stdscr, 3 + i * 2, 4, " / ".join(l.mode for l in m.links)[:SIDEBAR_W - 4],
                        curses.A_DIM)
        for y in range(h - 3):
            safe_addstr(stdscr, y, SIDEBAR_W, "│", curses.A_DIM)
    cw = w - x0

    title = f"mousectl -- {state.link}"
    idx = f"(D) link: {state.mode} [{state.link_idx + 1}/{len(state.mouse.links)}]"
    safe_addstr(stdscr, 0, x0, title[:cw - 1], curses.A_BOLD)
    if len(title) + len(idx) + 2 < cw:
        safe_addstr(stdscr, 0, w - len(idx) - 1, idx)
    safe_addstr(stdscr, 1, x0, "-" * min(cw - 1, 100))

    y = 3
    raw, mode, drv = state.raw, state.mode, state.driver
    label_w = max(len(s.label) for s in drv.settings) + 2
    for si, group in enumerate(state.sections):
        head = curses.color_pair(C_FOCUS) | curses.A_BOLD if si == state.section else curses.A_BOLD
        safe_addstr(stdscr, y, x0, group, head)
        y += 1
        if group == BATTERY:
            focus = curses.color_pair(C_FOCUS) if si == state.section else 0
            if state.battery_loading:
                safe_addstr(stdscr, y, x0 + 2, f"{state.spinner_frame} Loading...",
                            curses.color_pair(C_PENDING) | curses.A_DIM)
            elif state.battery:
                safe_addstr(stdscr, y, x0 + 2, f"{state.battery[0]}%  ({state.battery[1]})   "
                                               f"press 'b' to read again", focus)
            elif mode == "wired":
                safe_addstr(stdscr, y, x0 + 2, "-- wired: not reported / not applicable --", focus)
            else:
                safe_addstr(stdscr, y, x0 + 2, "unknown -- press 'b' to read", focus)
            y += 2
            continue
        if not raw:
            safe_addstr(stdscr, y, x0 + 2, f"{state.spinner_frame} Loading...",
                        curses.color_pair(C_PENDING) | curses.A_DIM)
            y += 2
            continue
        dirty = set(s.key for s in state.dirty(group))
        for fi, s in enumerate(state.fields(group)):
            text = s.format(raw, mode)
            if s.companion and s.enabled(raw, mode):
                comp = drv.setting(s.companion)
                text += "  " + comp.format(raw, mode)
                if comp.key in {c.key for c in state.dirty(group)}:
                    dirty.add(s.key)
            if s.suffix:
                text += s.suffix(raw, mode)
            attr = curses.color_pair(C_PENDING) if s.key in dirty else 0
            if si == state.section and fi == state.field:
                attr = curses.color_pair(C_FOCUS)
            safe_addstr(stdscr, y, x0 + 2, f"{s.label:<{label_w}}{text}"[:cw - 3], attr)
            y += 1
        ro = [f"{s.label}: {s.format(raw, mode)}" for s in drv.settings
              if s.group == group and s.read_only and not s.hidden]
        line = ""
        for item in ro:
            if line and len(line) + len(item) + 3 > cw - 16:
                safe_addstr(stdscr, y, x0 + 2, line, curses.A_DIM)
                y, line = y + 1, ""
            line += ("   " if line else "") + item
        if line:
            safe_addstr(stdscr, y, x0 + 2, (line + "   (read-only)")[:cw - 3], curses.A_DIM)
            y += 1
        if dirty:
            safe_addstr(stdscr, y, x0 + 2, "[pending -- press 'a' to apply]", curses.color_pair(C_PENDING))
            y += 1
        y += 1

    hint1 = "Tab:section  Up/Dn:field  Left/Right:change  Enter:edit  x:on/off  c:colour"
    hint2 = "a:apply  A:apply-all  r:refresh  b:battery  D:link  m:mouse  R:rescan  q:quit"
    safe_addstr(stdscr, h - 3, 0, hint1[:w - 1], curses.A_DIM)
    safe_addstr(stdscr, h - 2, 0, hint2[:w - 1], curses.A_DIM)
    attr = curses.color_pair(C_ERROR) if state.message_kind == "error" else curses.color_pair(C_INFO)
    safe_addstr(stdscr, h - 1, 0, state.message[:w - 1], attr)
    stdscr.refresh()


# ---------------------------------------------------------------- key loop

def handle_key(stdscr, state, ch, args):
    if ch in (ord("q"), 27):
        return bool(state.dirty()) and not confirm(stdscr, "Unapplied edits will be lost. Quit? [y/N] ")
    n_fields = max(1, len(state.fields())) if state.group != BATTERY else 1
    if ch == 9:  # Tab
        state.section = (state.section + 1) % len(state.sections)
        state.field = 0
    elif ch == getattr(curses, "KEY_BTAB", -1):
        state.section = (state.section - 1) % len(state.sections)
        state.field = 0
    elif ch in (curses.KEY_DOWN, ord("j")):
        state.field = (state.field + 1) % n_fields
    elif ch in (curses.KEY_UP, ord("k")):
        state.field = (state.field - 1) % n_fields
    elif ch in (curses.KEY_LEFT, ord("h")):
        nudge(state, -1)
    elif ch in (curses.KEY_RIGHT, ord("l")):
        nudge(state, 1)
    elif ch in (curses.KEY_ENTER, 10, 13):
        edit(stdscr, state, state.setting)
    elif ch in (ord("x"), ord(" ")):
        toggle(state)
    elif ch == ord("c"):
        edit_companion(stdscr, state)
    elif ch == ord("a"):
        if state.group == BATTERY:
            state.say("Nothing to apply in Battery.")
        else:
            apply(stdscr, state, [state.group], state.group)
    elif ch == ord("A"):
        apply(stdscr, state, state.driver.groups, "All sections")
    elif ch == ord("r"):
        refresh(stdscr, state)
    elif ch == ord("b"):
        read_battery(stdscr, state)
    elif ch == ord("D"):
        switch(stdscr, state, "link")
    elif ch == ord("m"):
        switch(stdscr, state, "mouse")
    elif ch == ord("R"):
        rescan(stdscr, state, args)
    return True


def _main(stdscr, mice, args):
    curses.curs_set(0)
    init_colors()
    state = State(mice)
    refresh(stdscr, state, confirm_if_dirty=False)
    while True:
        render(stdscr, state)
        if not handle_key(stdscr, state, stdscr.getch(), args):
            return


def run(mice, args):
    if getattr(args, "link", None):
        for m in mice:
            m.links = [l for l in m.links if l.mode == args.link] or m.links
    curses.wrapper(_main, mice, args)
