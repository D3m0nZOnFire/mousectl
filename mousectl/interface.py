"""The machine-readable interface: the shape of every --json output.

External tools (bar widgets, GUIs, scripts) parse these, so they are a
contract, documented in docs/INTERFACE.md. Adding a field is fine; renaming,
removing or retyping one means bumping VERSION.
"""

from .core.settings import Bool, Choice, Color, Number

VERSION = 1

ERROR_CODES = ("permission", "no-answer", "io")
BATTERY_STATES = ("charging", "discharging", "full", "unknown")


def document(**fields):
    return {"interface": VERSION, **fields}


def value(v):
    """A setting value as JSON: colours as "#rrggbb", the rest as-is."""
    return "#%02x%02x%02x" % tuple(v) if isinstance(v, tuple) else v


def error(e):
    if e is None:
        return None
    code = getattr(e, "code", "io")
    return {"code": code if code in ERROR_CODES else "io", "message": str(e)}


def battery(bat):
    if bat is None:
        return None
    pct, state = bat
    return {"percent": pct, "state": state if state in BATTERY_STATES else "unknown"}


def mouse(m):
    return {"mouse": m.driver.id, "name": m.name}


def link(l):
    return {"link": l.mode if l else None, "path": l.path if l else None}


def listed(m):
    return {**mouse(m), "links": [{"mode": l.mode, "path": l.path} for l in m.links]}


def settings(keys, raw, mode):
    return {s.key: value(s.value(raw, mode)) for s in keys}


def status(m, l=None, raw=None, bat=None, err=None):
    return {**mouse(m), **link(l),
            "settings": settings(m.driver.settings, raw, l.mode) if raw is not None else None,
            "battery": battery(bat), "error": error(err)}


def battery_entry(m, l=None, bat=None, err=None):
    return {**mouse(m), **link(l), "battery": battery(bat), "error": error(err)}


def setting_schema(s):
    k = s.kind
    out = {"key": s.key, "label": s.label, "group": s.group,
           "unit": getattr(k, "unit", "").strip(),
           "read_only": s.read_only, "can_disable": s.set_enabled is not None, "help": s.help}
    if isinstance(k, Number):
        out.update(kind="number", min=k.lo, max=k.hi, values=list(k.values))
    elif isinstance(k, Choice):
        out.update(kind="choice", values=list(k.values))
    elif isinstance(k, Bool):
        out.update(kind="bool")
    elif isinstance(k, Color):
        out.update(kind="color")
    else:
        raise TypeError(f"{s.key}: no interface kind for {type(k).__name__}")
    return out


def keys(m):
    return {**mouse(m), "settings": [setting_schema(s) for s in m.driver.settings]}
