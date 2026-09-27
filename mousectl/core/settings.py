"""The settings schema: how a driver describes what it can configure.

Every Setting is a pure get/set over a driver's raw snapshot ({unit: bytearray}),
so the core can show, diff, and partially apply settings for any mouse without
knowing its protocol. Values are plain Python (int/float/str/bool/(r,g,b)).
"""

import bisect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ------------------------------------------------------------------- kinds

class Number:
    """Numeric value on a grid. `steps` is ((upto, step), ...) so a range
    can get coarser as it grows (e.g. a sensor with 50-DPI steps up to 10k
    and 100-DPI steps beyond)."""

    def __init__(self, lo, hi, step=1, steps=None, unit=""):
        self.lo, self.hi, self.unit = lo, hi, unit
        self.steps = steps or ((hi, step),)
        vals = [lo]
        for upto, st in self.steps:
            v = vals[-1]
            while v + st <= upto + 1e-9:
                v = round(v + st, 6)
                vals.append(int(v) if v == int(v) else v)
        self.values = vals

    def describe(self):
        grid = ", ".join(f"step {st} up to {fmt_num(upto)}" for upto, st in self.steps)
        return f"{fmt_num(self.lo)}-{fmt_num(self.hi)}{self.unit} ({grid})"

    def parse(self, s):
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"not a number: {s!r}")
        v = int(v) if v == int(v) else v
        self.check(v)
        return v

    def check(self, v):
        if not isinstance(v, (int, float)) or not any(abs(v - x) < 1e-9 for x in self._near(v)):
            raise ValueError(f"must be {self.describe()}")

    def _near(self, v):
        i = bisect.bisect_left(self.values, v - 1e-9)
        return self.values[max(0, i - 1):i + 1]

    def nudge(self, v, d):
        if not isinstance(v, (int, float)):
            return self.values[0]
        i = bisect.bisect_left(self.values, v - 1e-9)
        if d < 0:
            i -= 1
        elif i < len(self.values) and abs(self.values[i] - v) < 1e-9:
            i += 1
        return self.values[max(0, min(len(self.values) - 1, i))]

    def format(self, v):
        return f"{fmt_num(v)}{self.unit}" if isinstance(v, (int, float)) else str(v)

    def text(self, v):
        return fmt_num(v) if isinstance(v, (int, float)) else ""

    typed = True   # Enter opens a text editor for it in the TUI


class Choice:
    def __init__(self, values, unit=""):
        self.values, self.unit = list(values), unit

    def describe(self):
        return "one of " + ", ".join(map(str, self.values)) + (f" ({self.unit.strip()})" if self.unit else "")

    def parse(self, s):
        for v in self.values:
            if str(v) == s:
                return v
        raise ValueError(f"must be {self.describe()}")

    def check(self, v):
        if v not in self.values:
            raise ValueError(f"must be {self.describe()}")

    def nudge(self, v, d):
        i = self.values.index(v) if v in self.values else (-1 if d > 0 else 0)
        return self.values[(i + d) % len(self.values)]

    def format(self, v):
        return f"{v}{self.unit}"

    def text(self, v):
        return str(v)

    typed = False


class Bool:
    values = [False, True]

    def describe(self):
        return "on|off"

    def parse(self, s):
        s = s.lower()
        if s in ("on", "true", "1", "yes"):
            return True
        if s in ("off", "false", "0", "no"):
            return False
        raise ValueError("must be on or off")

    def check(self, v):
        if not isinstance(v, bool):
            raise ValueError("must be on or off")

    def nudge(self, v, d):
        return not v

    def format(self, v):
        return "on" if v else "off"

    def text(self, v):
        return self.format(v)

    typed = False


class Color:
    def describe(self):
        return "RRGGBB hex, e.g. ff8800"

    def parse(self, s):
        s = s.lstrip("#")
        if len(s) != 6:
            raise ValueError("colour must be RRGGBB hex, e.g. ff8800")
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise ValueError("colour must be RRGGBB hex, e.g. ff8800")

    def check(self, v):
        if not (isinstance(v, tuple) and len(v) == 3 and all(0 <= c <= 255 for c in v)):
            raise ValueError("colour must be an (r, g, b) tuple")

    def nudge(self, v, d):
        return v

    def format(self, v):
        return "#%02x%02x%02x" % tuple(v)

    def text(self, v):
        return "%02x%02x%02x" % tuple(v)

    typed = True


def fmt_num(v):
    return str(int(v)) if isinstance(v, float) and v == int(v) else str(v)


# ----------------------------------------------------------------- setting

@dataclass
class Setting:
    key: str                                   # CLI/JSON name, e.g. "dpi.stage3"
    label: str                                 # TUI/status label
    group: str                                 # section it's shown and applied with
    kind: Any                                  # Number / Choice / Bool / Color
    units: tuple                               # raw units it lives in
    get: Callable                              # (raw, mode) -> value
    set: Optional[Callable] = None             # (raw, mode, value); None = read-only
    # Optional on/off state separate from the value (e.g. DPI stages that
    # can be disabled). Values of a disabled setting are shown as "off".
    get_enabled: Optional[Callable] = None     # (raw, mode) -> bool
    set_enabled: Optional[Callable] = None     # (raw, mode, bool)
    suffix: Optional[Callable] = None          # (raw, mode) -> extra display text
    companion: Optional[str] = None            # key of a setting shown inline (edited with 'c')
    hidden: bool = False                       # not a TUI row (shown via its parent)
    help: str = ""

    @property
    def read_only(self):
        return self.set is None

    def enabled(self, raw, mode):
        return self.get_enabled(raw, mode) if self.get_enabled else True

    def value(self, raw, mode):
        """User-facing value: the decoded value, or "off" when disabled."""
        return self.get(raw, mode) if self.enabled(raw, mode) else "off"

    def format(self, raw, mode):
        v = self.value(raw, mode)
        return "off" if v == "off" else self.kind.format(v)

    def assign(self, raw, mode, text_or_value):
        """Set from user input (string) or a Python value. For a toggleable
        setting "off" disables it and any value enables it -- the way the
        vendor tools treat e.g. setting a DPI stage's value."""
        if self.read_only:
            raise ValueError(f"{self.key} is read-only")
        v = text_or_value
        if self.set_enabled and (v == "off" or v is False) and not isinstance(self.kind, Bool):
            self.set_enabled(raw, mode, False)
            return
        if isinstance(v, str):
            v = self.kind.parse(v)
        else:
            self.kind.check(v)
        if self.set_enabled and not self.enabled(raw, mode):
            self.set_enabled(raw, mode, True)
        self.set(raw, mode, v)


def copy_raw(raw):
    return {k: bytearray(v) for k, v in raw.items()}


def state_of(s, raw, mode):
    """Comparable state of a setting (enabled flag + value)."""
    return (s.enabled(raw, mode), s.get(raw, mode))


def dirty_settings(settings, device_raw, pending_raw, mode, group=None):
    return [s for s in settings
            if not s.read_only and (group is None or s.group == group)
            and state_of(s, device_raw, mode) != state_of(s, pending_raw, mode)]


def replay(settings, device_raw, pending_raw, mode, groups):
    """Target raw = device bytes + only `groups`' pending edits.

    Bytes that belong to other groups -- even inside a shared unit -- keep
    the device's current value, so their edits stay pending. Enable-state
    changes go first so a value/active-stage replay sees the right mask.
    """
    target = copy_raw(device_raw)
    todo = [s for g in groups for s in dirty_settings(settings, device_raw, pending_raw, mode, g)]
    for s in todo:
        if s.set_enabled:
            want = s.enabled(pending_raw, mode)
            if want != s.enabled(target, mode):
                s.set_enabled(target, mode, want)
    for s in todo:
        if s.enabled(pending_raw, mode):
            v = s.get(pending_raw, mode)
            if v != s.get(target, mode):
                s.set(target, mode, v)
    return target
