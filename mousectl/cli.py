"""mousectl command line.

Generic commands work for every driver through its Setting schema.
"""

import argparse
import getpass
import json
import os
import pwd
import re
import sys

from .core import store
from .core.driver import DeviceError, discover
from .core.settings import copy_raw
from .drivers import DRIVERS, find


def die(msg):
    print(f"mousectl: {msg}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------- selection

def wanted_driver(args):
    """The driver -m names, or None when no -m was given."""
    want = getattr(args, "mouse", None)
    if not want:
        return None
    drv = find(want)
    if not drv:
        die(f"unknown mouse '{want}' (known: {', '.join(d.id for d in DRIVERS)})")
    return drv


def scan_mice(drv):
    mice = discover(DRIVERS)
    return [m for m in mice if m.driver is drv] if drv else mice


def select_mice(args, one=False):
    drv = wanted_driver(args)
    mice = scan_mice(drv)
    if drv and not mice:
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
    # No mouse yet is fine: the TUI waits for one instead of exiting.
    drv = wanted_driver(args)
    run(scan_mice(drv), args, scan=lambda: scan_mice(drv),
        wanted=drv.name if drv else "supported mouse")


def cmd_install_udev(args):
    owner = args.owner or getpass.getuser()
    # The name goes verbatim into OWNER="..." of a root-installed rule.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\$?", owner):
        die(f"invalid user name: {owner!r}")
    try:
        pwd.getpwnam(owner)
    except KeyError:
        die(f"no such user: {owner!r}")
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


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)
