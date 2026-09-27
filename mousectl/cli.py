"""mousectl command line.

Generic commands work for every driver through its Setting schema. Invoked
as `ashark` / `vt3pro` (or `mousectl ashark ...`), the old single-model
command lines are translated onto the generic ones.
"""

import argparse
import getpass
import json
import os
import sys

from .core import store
from .core.driver import DeviceError, discover
from .core.settings import copy_raw
from .drivers import DRIVERS, find


def die(msg):
    print(f"mousectl: {msg}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------- selection

def select_mice(args, one=False):
    mice = discover(DRIVERS)
    want = getattr(args, "mouse", None)
    if want:
        drv = find(want)
        if not drv:
            die(f"unknown mouse '{want}' (known: {', '.join(d.id for d in DRIVERS)})")
        mice = [m for m in mice if m.driver is drv]
        if not mice:
            die(f"no {drv.name} connected")
    if not mice:
        die("no supported mouse found (is it plugged in / the dongle connected?)")
    if one and len(mice) > 1:
        die("several mice connected, pick one with -m: " + ", ".join(m.driver.id for m in mice))
    return mice


def links_of(mouse, args):
    links = mouse.links
    if getattr(args, "link", None):
        links = [l for l in links if l.mode == args.link]
        if not links:
            die(f"{mouse.name}: no '{args.link}' connection present")
    return links


def on_first_link(mouse, args, fn):
    """Run fn(session) on the first link that answers. Settings live in the
    mouse, so any live link will do; later links are fallbacks."""
    errors = []
    for link in links_of(mouse, args):
        try:
            with link.open() as s:
                return fn(s)
        except DeviceError as e:
            errors.append(f"[{link.mode}] {e}")
    for e in errors:
        print(f"{mouse.name} {e}", file=sys.stderr)
    return None


def jsonable(v):
    return "#%02x%02x%02x" % v if isinstance(v, tuple) else v


# ------------------------------------------------------------- commands

def cmd_list(args):
    mice = discover(DRIVERS)
    if not mice:
        print("no supported mouse connected")
    for m in mice:
        links = ", ".join(f"{l.mode} {l.path}" for l in m.links)
        print(f"{m.driver.id:18} {m.name:20} {links}")


def status_lines(drv, raw, mode):
    width = max(len(s.label) for s in drv.settings) + 2
    for group in drv.groups:
        yield f"  {group}"
        for s in drv.settings:
            if s.group != group or s.hidden:
                continue
            text = s.format(raw, mode)
            if s.companion and s.enabled(raw, mode):
                text += "  " + drv.setting(s.companion).format(raw, mode)
            if s.suffix:
                text += s.suffix(raw, mode)
            if s.read_only:
                text += "  (read-only)"
            yield f"    {s.label:<{width}}{text}"


def read_battery(s):
    try:
        return s.battery()
    except DeviceError:
        return None


def cmd_status(args):
    out = []
    for m in select_mice(args):
        def run(s):
            raw = s.read()
            bat = None if args.no_battery else read_battery(s)
            return s.link, raw, bat
        res = on_first_link(m, args, run)
        if not res:
            continue
        link, raw, bat = res
        if args.json:
            out.append({"mouse": m.driver.id, "name": m.name, "link": link.mode, "path": link.path,
                        "settings": {st.key: jsonable(st.value(raw, link.mode)) for st in m.driver.settings},
                        "battery": {"percent": bat[0], "state": bat[1]} if bat else None})
            continue
        print(f"== {link}")
        for line in status_lines(m.driver, raw, link.mode):
            print(line)
        if not args.no_battery:
            print(f"  Battery: {bat[0]}%  ({bat[1]})" if bat else
                  "  Battery: unknown" + ("" if link.mode == "wired" else " (move/click the mouse and retry)"))
    if args.json:
        print(json.dumps(out, indent=2))


def cmd_get(args):
    m = select_mice(args, one=True)[0]
    try:
        settings = [m.driver.setting(k) for k in args.keys]
    except KeyError as e:
        die(f"{m.name} has no setting {e} -- see 'mousectl keys'")
    units = sorted({u for s in settings for u in s.units})
    res = on_first_link(m, args, lambda s: (s.mode, s.read(units)))
    if not res:
        sys.exit(1)
    mode, raw = res
    for s in settings:
        print(json.dumps(jsonable(s.value(raw, mode))) if args.json else f"{s.key} = {s.format(raw, mode)}")


def parse_assignments(drv, items):
    out = []
    for item in items:
        if "=" not in item:
            die(f"expected KEY=VALUE, got '{item}'")
        k, v = item.split("=", 1)
        try:
            s = drv.setting(k.strip())
        except KeyError:
            die(f"{drv.name} has no setting '{k}' -- see 'mousectl keys'")
        if s.read_only:
            die(f"{s.key} is read-only")
        out.append((s, v.strip()))
    return out


def cmd_set(args):
    m = select_mice(args, one=True)[0]
    todo = parse_assignments(m.driver, args.assign)
    if not todo:
        die("nothing to set")
    units = sorted({u for s, _ in todo for u in s.units})

    def run(s):
        before = s.read(units)
        after = copy_raw(before)
        for st, v in todo:
            try:
                st.assign(after, s.mode, v)
            except ValueError as e:
                raise SystemExit(f"mousectl: {st.key}: {e}")
        written = store.apply(s, before, after)
        for st, _ in todo:
            print(f"[{s.mode}] {st.key} = {st.format(after, s.mode)}")
        if not written:
            print(f"[{s.mode}] (unchanged)")
        return True
    if not on_first_link(m, args, run):
        sys.exit(1)


def cmd_keys(args):
    for m in select_mice(args):
        print(f"== {m.name} ({m.driver.id})")
        for s in m.driver.settings:
            ro = "  read-only" if s.read_only else ""
            off = " | off" if s.set_enabled else ""
            print(f"  {s.key:<20} {s.kind.describe()}{off}{ro}" + (f"  -- {s.help}" if s.help else ""))


def cmd_battery(args):
    out = []
    for m in select_mice(args):
        for link in links_of(m, args):
            try:
                with link.open() as s:
                    bat = s.battery(args.timeout)
            except DeviceError as e:
                print(f"{m.name} [{link.mode}]: {e}", file=sys.stderr)
                continue
            if bat is None:
                if link.mode == "wired":
                    continue    # externally powered, nothing to report
                print(f"{m.name} [{link.mode}]: no battery report "
                      f"(move/click the mouse and retry)", file=sys.stderr)
                continue
            out.append({"mouse": m.driver.id, "name": m.name, "link": link.mode,
                        "percent": bat[0], "state": bat[1]})
            break
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for b in out:
            print(f"{b['name']} [{b['link']}]: {b['percent']}%  ({b['state']})")
    if not out:
        sys.exit(1)


def cmd_dump(args):
    for m in select_mice(args):
        for link in links_of(m, args):
            try:
                with link.open() as s:
                    raw = s.read()
            except DeviceError as e:
                print(f"{link}: skipped: {e}", file=sys.stderr)
                continue
            print(f"== {link}")
            if "lengths" in link.info:
                print(f"  feature report lengths: "
                      f"{ {hex(k): v for k, v in sorted(link.info['lengths'].items())} }")
            for u, b in raw.items():
                print(f"  {m.driver.unit_name(u)}: {bytes(b).hex(' ')}")


def cmd_save(args):
    m = select_mice(args, one=True)[0]
    path = os.path.expanduser(args.file) if args.file else \
        os.path.join(store.driver_dir(m.driver), "settings.json")
    res = on_first_link(m, args, lambda s: (s.mode, s.read()))
    if not res:
        sys.exit(1)
    store.write_snapshot(path, m.driver, res[0], res[1])
    print(f"saved {m.name} settings to {path}")


def cmd_restore(args):
    m = select_mice(args, one=True)[0]
    path = os.path.expanduser(args.file)
    try:
        target = store.load_snapshot(path, m.driver)
    except (OSError, ValueError, KeyError) as e:
        die(f"cannot restore from {path}: {e}")

    def run(s):
        before = s.read()
        written = store.apply(s, before, copy_raw(target))
        print(f"[{s.mode}] restored {len(written)} changed block(s) from {path}")
        return True
    if not on_first_link(m, args, run):
        sys.exit(1)


def cmd_tui(args):
    from .tui.app import run
    run(select_mice(args), args)


def cmd_install_udev(args):
    owner = args.owner or getpass.getuser()
    path = os.path.join(store.CONFIG_DIR, "99-mousectl.rules")
    os.makedirs(store.CONFIG_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write("# mousectl: give the desktop user access to supported mice's hidraw nodes.\n"
                "# uaccess alone doesn't always apply, so the owner is set explicitly too.\n")
        for d in DRIVERS:
            f.write(f"# {d.name}\n{d.udev_rules(owner)}")
    print(f"Wrote {path}. Run these to grant '{owner}' access to every supported mouse:\n")
    print(f"  sudo install -m 644 {path} /etc/udev/rules.d/99-mousectl.rules")
    print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
    print("\nThen unplug/replug the mouse (or dongle).")


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="mousectl", description="Configure gaming mice on Linux.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-m", "--mouse", help="driver id or alias (see 'mousectl list')")
    common.add_argument("--link", help="only use this connection (wired, dongle)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="connected mice").set_defaults(fn=cmd_list)

    s = sub.add_parser("status", parents=[common], help="show settings")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-battery", action="store_true", help="skip the battery query")
    s.set_defaults(fn=cmd_status)

    g = sub.add_parser("get", parents=[common], help="read settings by key")
    g.add_argument("keys", nargs="+", metavar="KEY")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_get)

    st = sub.add_parser("set", parents=[common], help="change settings: KEY=VALUE ...")
    st.add_argument("assign", nargs="+", metavar="KEY=VALUE")
    st.set_defaults(fn=cmd_set)

    sub.add_parser("keys", parents=[common], help="settings each mouse supports").set_defaults(fn=cmd_keys)

    b = sub.add_parser("battery", parents=[common], help="battery level")
    b.add_argument("--json", action="store_true")
    b.add_argument("--timeout", type=float, default=5.0,
                   help="seconds to wait for mice that only report periodically (default 5)")
    b.set_defaults(fn=cmd_battery)

    sub.add_parser("dump", parents=[common], help="raw config (debugging)").set_defaults(fn=cmd_dump)

    sv = sub.add_parser("save", parents=[common], help="save settings to a JSON snapshot")
    sv.add_argument("file", nargs="?", help="default ~/.config/mousectl/<mouse>/settings.json")
    sv.set_defaults(fn=cmd_save)

    rs = sub.add_parser("restore", parents=[common],
                        help="write back a snapshot (first-write backup: ~/.config/mousectl/<mouse>/auto-backup.json)")
    rs.add_argument("file")
    rs.set_defaults(fn=cmd_restore)

    sub.add_parser("tui", parents=[common], help="interactive terminal UI").set_defaults(fn=cmd_tui)

    u = sub.add_parser("install-udev", help="print udev setup commands")
    u.add_argument("--owner", help="user to own the device nodes (default: you)")
    u.set_defaults(fn=cmd_install_udev)
    return p


# ---------------------------------------------------------------- legacy

def _generic(drv, largs, cmd, **kw):
    ns = argparse.Namespace(mouse=drv.id, link=getattr(largs, "device", None),
                            json=False, no_battery=False, **kw)
    cmd(ns)


def _onoff():
    return dict(type=lambda s: s == "on", choices=[True, False], metavar="on|off")


def legacy_ashark(drv, argv):
    p = argparse.ArgumentParser(prog="ashark", description="Configure the Attack Shark X11 mouse.")
    p.add_argument("--device", choices=["wired", "dongle"], help="target only one connection")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "dump", "save", "install-udev", "tui"):
        sub.add_parser(name)
    sub.add_parser("battery").add_argument("--timeout", type=float, default=5.0)
    d = sub.add_parser("dpi", help="set DPI stages")
    d.add_argument("--stages", help="comma-separated list, e.g. 800,1600,3200")
    d.add_argument("--stage", type=int, choices=range(1, 9))
    d.add_argument("--value", type=int)
    d.add_argument("--active", type=int, choices=range(1, 9))
    d.add_argument("--color", help="RRGGBB colour for --stage")
    d.add_argument("--angle-snap", **_onoff())
    d.add_argument("--ripple", **_onoff())
    sub.add_parser("polling").add_argument("hz", type=int)
    l = sub.add_parser("light")
    l.add_argument("--mode", choices=sorted(drv.setting("light.mode").kind.values))
    l.add_argument("--color")
    l.add_argument("--brightness", type=int, choices=range(1, 9))
    l.add_argument("--speed", type=int, choices=range(1, 6))
    sub.add_parser("debounce").add_argument("ms", type=int)
    sl = sub.add_parser("sleep")
    sl.add_argument("--idle", type=float)
    sl.add_argument("--deep", type=int)
    a = p.parse_args(argv)

    sets = []
    if a.cmd == "dpi":
        if a.stages:
            vals = a.stages.split(",")
            if not 1 <= len(vals) <= 8:
                die("give between 1 and 8 comma-separated DPI values")
            sets += [f"dpi.stage{i + 1}={v}" for i, v in enumerate(vals)]
            sets += [f"dpi.stage{i + 1}=off" for i in range(len(vals), 8)]
        elif a.stage and a.value is not None:
            sets.append(f"dpi.stage{a.stage}={a.value}")
        elif a.stage and not a.color:
            die("--stage needs --value")
        if a.color:
            if not a.stage:
                die("--color needs --stage")
            sets.append(f"dpi.stage{a.stage}.color={a.color}")
        if a.active:
            sets.append(f"dpi.active={a.active}")
        if a.angle_snap is not None:
            sets.append(f"angle_snap={'on' if a.angle_snap else 'off'}")
        if a.ripple is not None:
            sets.append(f"ripple={'on' if a.ripple else 'off'}")
    elif a.cmd == "polling":
        sets.append(f"polling={a.hz}")
    elif a.cmd == "light":
        for k in ("mode", "color", "brightness", "speed"):
            if getattr(a, k) is not None:
                sets.append(f"light.{k}={getattr(a, k)}")
    elif a.cmd == "debounce":
        sets.append(f"debounce={a.ms}")
    elif a.cmd == "sleep":
        if a.idle is not None:
            sets.append(f"sleep={a.idle}")
        if a.deep is not None:
            sets.append(f"sleep.deep={a.deep}")
    return a, sets


def legacy_vt3pro(drv, argv):
    p = argparse.ArgumentParser(prog="vt3pro", description="Configure the Rapoo VT3 PRO mouse.")
    p.add_argument("--device", choices=["wired", "dongle"], help="target only one connection")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "dump", "battery", "tui", "install-udev"):
        sub.add_parser(name)
    sub.add_parser("save").add_argument("file", nargs="?")
    sub.add_parser("restore").add_argument("file")
    d = sub.add_parser("dpi")
    d.add_argument("--stages", help="7 comma-separated values")
    d.add_argument("--stage", type=int, choices=range(1, 8))
    d.add_argument("--value", type=int)
    d.add_argument("--active", type=int, choices=range(1, 8))
    sub.add_parser("polling").add_argument("hz", type=int)
    s = sub.add_parser("sensor")
    s.add_argument("--motion-sync", **_onoff())
    s.add_argument("--angle-snap", **_onoff())
    s.add_argument("--ripple", **_onoff())
    b = sub.add_parser("debounce")
    b.add_argument("--press", type=int)
    b.add_argument("--release", type=int)
    sub.add_parser("sleep").add_argument("minutes", type=int)
    a = p.parse_args(argv)

    sets = []
    onoff = lambda v: "on" if v else "off"
    if a.cmd == "dpi":
        if a.stages:
            vals = a.stages.split(",")
            if len(vals) != 7:
                die("give exactly 7 comma-separated DPI values (one per stage)")
            sets += [f"dpi.stage{i + 1}={v}" for i, v in enumerate(vals)]
        elif a.stage and a.value is not None:
            sets.append(f"dpi.stage{a.stage}={a.value}")
        elif a.stage and a.active is None:
            die("--stage needs --value")
        if a.active:
            sets.append(f"dpi.active={a.active}")
    elif a.cmd == "polling":
        sets.append(f"polling={a.hz}")
    elif a.cmd == "sensor":
        if a.motion_sync is None and a.angle_snap is None and a.ripple is None:
            die("give --motion-sync, --angle-snap and/or --ripple")
        for k, v in (("motion_sync", a.motion_sync), ("angle_snap", a.angle_snap), ("ripple", a.ripple)):
            if v is not None:
                sets.append(f"{k}={onoff(v)}")
    elif a.cmd == "debounce":
        if a.press is None and a.release is None:
            die("give --press and/or --release")
        if a.press is not None:
            sets.append(f"debounce.press={a.press}")
        if a.release is not None:
            sets.append(f"debounce.release={a.release}")
    elif a.cmd == "sleep":
        sets.append(f"sleep={a.minutes}")
    return a, sets


LEGACY = {"attackshark-x11": legacy_ashark, "rapoo-vt3pro": legacy_vt3pro}


def legacy_main(drv, argv):
    a, sets = LEGACY[drv.id](drv, argv)
    if sets:
        return _generic(drv, a, cmd_set, assign=sets)
    if a.cmd in ("dpi", "light", "sleep"):
        die("nothing to change -- give at least one option (see --help)")
    simple = {"status": cmd_status, "dump": cmd_dump, "tui": cmd_tui}
    if a.cmd in simple:
        _generic(drv, a, simple[a.cmd])
    elif a.cmd == "battery":
        _generic(drv, a, cmd_battery, timeout=getattr(a, "timeout", 5.0))
    elif a.cmd == "save":
        _generic(drv, a, cmd_save, file=getattr(a, "file", None))
    elif a.cmd == "restore":
        _generic(drv, a, cmd_restore, file=a.file)
    elif a.cmd == "install-udev":
        cmd_install_udev(argparse.Namespace(owner=None))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    invoked = os.environ.get("MOUSECTL_ARGV0", "")
    drv = find(invoked) if invoked else None
    if drv is None and argv and not argv[0].startswith("-"):
        drv = find(argv[0])
        if drv:
            argv = argv[1:]
    if drv is not None:
        return legacy_main(drv, argv)
    args = build_parser().parse_args(argv)
    args.fn(args)
