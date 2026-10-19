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

from . import __version__, interface
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
    if not mice and not one and getattr(args, "json", False):
        return []       # scripts poll whether or not a mouse is there
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


def on_links(mouse, args, fn):
    """Run fn(session) on the first link that answers. Settings live in the
    mouse, so any live link will do; later links are fallbacks.
    Returns (link, result, errors), link None when none answered and
    errors the [(link, DeviceError)] of the links that failed."""
    errors = []
    for link in links_of(mouse, args):
        try:
            with link.open() as s:
                return link, fn(s), errors
        except DeviceError as e:
            errors.append((link, e))
    return None, None, errors


def report_errors(mouse, errors):
    for link, e in errors:
        print(f"{mouse.name} [{link.mode}] {e}", file=sys.stderr)


def on_first_link(mouse, args, fn):
    link, res, errors = on_links(mouse, args, fn)
    if link is None:
        report_errors(mouse, errors)
    return res


def emit(doc):
    print(json.dumps(doc, indent=2))


# ------------------------------------------------------------- commands

def cmd_list(args):
    mice = discover(DRIVERS)
    if args.json:
        return emit(interface.document(mice=[interface.listed(m) for m in mice]))
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
            return raw, bat
        link, res, errors = on_links(m, args, run)
        if args.json:
            out.append(interface.status(m, link, *res) if link else interface.status(m, err=errors[-1][1]))
            continue
        if not link:
            report_errors(m, errors)
            continue
        raw, bat = res
        print(f"== {link}")
        for line in status_lines(m.driver, raw, link.mode):
            print(line)
        if not args.no_battery:
            print(f"  Battery: {bat[0]}%  ({bat[1]})" if bat else
                  "  Battery: unknown" + ("" if link.mode == "wired" else " (move/click the mouse and retry)"))
    if args.json:
        emit(interface.document(mice=out))


def one_mouse_doc(m, link, errors, keys, raw, **extra):
    """get/set --json: the values of `keys`, or the error of the last link."""
    return interface.document(**interface.mouse(m), **interface.link(link),
                              settings=interface.settings(keys, raw, link.mode) if link else None,
                              **extra, error=None if link else interface.error(errors[-1][1]))


def cmd_get(args):
    m = select_mice(args, one=True)[0]
    try:
        settings = [m.driver.setting(k) for k in args.keys]
    except KeyError as e:
        die(f"{m.name} has no setting {e} -- see 'mousectl keys'")
    units = sorted({u for s in settings for u in s.units})
    link, raw, errors = on_links(m, args, lambda s: s.read(units))
    if args.json:
        emit(one_mouse_doc(m, link, errors, settings, raw))
    elif link:
        for s in settings:
            print(f"{s.key} = {s.format(raw, link.mode)}")
    else:
        report_errors(m, errors)
    if not link:
        sys.exit(1)


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
                die(f"{st.key}: {e}")
        return after, store.apply(s, before, after)
    link, res, errors = on_links(m, args, run)
    after, written = res or (None, None)
    if args.json:
        emit(one_mouse_doc(m, link, errors, [st for st, _ in todo], after, changed=bool(written)))
    elif link:
        for st, _ in todo:
            print(f"[{link.mode}] {st.key} = {st.format(after, link.mode)}")
        if not written:
            print(f"[{link.mode}] (unchanged)")
    else:
        report_errors(m, errors)
    if not link:
        sys.exit(1)


def cmd_keys(args):
    mice = select_mice(args)
    if args.json:
        return emit(interface.document(mice=[interface.keys(m) for m in mice]))
    for m in mice:
        print(f"== {m.name} ({m.driver.id})")
        for s in m.driver.settings:
            ro = "  read-only" if s.read_only else ""
            off = " | off" if s.set_enabled else ""
            print(f"  {s.key:<20} {s.kind.describe()}{off}{ro}" + (f"  -- {s.help}" if s.help else ""))


def battery_of(m, args):
    """(link, (percent, state), None) from the first link with a reading;
    else (wired link, None, None) when the cable powers it (no level to
    report); else (link, None, DeviceError) of the last one that failed."""
    wired = failed = None
    for link in links_of(m, args):
        try:
            with link.open() as s:
                bat = s.battery(args.timeout)
        except DeviceError as e:
            failed = (link, e)
            continue
        if bat:
            return link, bat, None
        if link.mode == "wired":
            wired = link
        else:
            failed = (link, DeviceError("no battery report (move/click the mouse and retry)",
                                        code="no-answer"))
    if wired:
        return wired, None, None
    return failed[0], None, failed[1]


def cmd_battery(args):
    out, read = [], 0
    for m in select_mice(args):
        link, bat, err = battery_of(m, args)
        read += bat is not None
        if args.json:
            out.append(interface.battery_entry(m, link, bat, err))
        elif bat:
            print(f"{m.name} [{link.mode}]: {bat[0]}%  ({bat[1]})")
        elif err:
            print(f"{m.name} [{link.mode}]: {err}", file=sys.stderr)
    if args.json:
        emit(interface.document(mice=out))
    elif not read:
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
    p.add_argument("--version", action="version", version=f"mousectl {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-m", "--mouse", help="driver id or alias (see 'mousectl list')")
    common.add_argument("--link", help="only use this connection (wired, dongle)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="connected mice")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

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
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_set)

    k = sub.add_parser("keys", parents=[common], help="settings each mouse supports")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_keys)

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
