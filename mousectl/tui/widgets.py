"""curses building blocks: colours, a line editor, a y/N prompt and a
loading spinner. Stdlib only."""

import curses
import threading

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

C_ERROR, C_PENDING, C_FOCUS, C_INFO = 1, 2, 3, 4


def safe_addstr(stdscr, y, x, text, attr=0):
    """addstr that swallows the standard curses edge-of-screen error."""
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_ERROR, curses.COLOR_RED, bg)
    curses.init_pair(C_PENDING, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_FOCUS, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_INFO, curses.COLOR_GREEN, bg)


def edit_line(stdscr, prompt, initial="", validate=None):
    """Blocking single-line editor drawn on the last screen row.

    Enter accepts (re-validating first -- on failure it shows the error and
    keeps editing rather than closing); Esc cancels and returns None.
    """
    curses.curs_set(1)
    buf = list(initial)
    err = ""
    try:
        while True:
            h, w = stdscr.getmaxyx()
            y = h - 1
            safe_addstr(stdscr, y, 0, " " * (w - 1))
            text = f"{prompt}: {''.join(buf)}"
            safe_addstr(stdscr, y, 0, text[:w - 1])
            safe_addstr(stdscr, y - 1, 0, " " * (w - 1))
            if err:
                safe_addstr(stdscr, y - 1, 0, f"! {err}"[:w - 1], curses.color_pair(C_ERROR))
            stdscr.move(y, min(len(text), w - 2))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                val = "".join(buf)
                if validate:
                    try:
                        validate(val)
                    except ValueError as e:
                        err = str(e)
                        continue
                return val
            elif ch == 27:  # Esc
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                err = ""
            elif ch == 21:  # Ctrl-U
                buf = []
                err = ""
            elif 32 <= ch < 127:
                buf.append(chr(ch))
                err = ""
    finally:
        curses.curs_set(0)


def confirm(stdscr, prompt):
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, h - 1, 0, " " * (w - 1))
    safe_addstr(stdscr, h - 1, 0, prompt[:w - 1], curses.color_pair(C_PENDING))
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q"), curses.KEY_ENTER, 10, 13):
            return False


def run_with_spinner(state, draw, fn):
    """Run a blocking call while animating state.spinner_frame in the
    background so loading panels actually spin instead of sitting on a
    static glyph. The spinner thread only ever reads state and draws --
    it never touches the device -- and is always joined (in `finally`)
    before this function returns, so by the time the caller does anything
    else with curses, it is the only thread drawing again.
    """
    stop = threading.Event()

    def animate():
        i = 0
        while not stop.is_set():
            state.spinner_frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            try:
                draw()
            except curses.error:
                pass
            i += 1
            stop.wait(0.08)

    t = threading.Thread(target=animate, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join()
        state.spinner_frame = SPINNER_FRAMES[0]
