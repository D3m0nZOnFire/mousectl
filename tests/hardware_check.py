"""Hardware check for a new driver, run by whoever owns the mouse.

    python3 -m tests.hardware_check -m <driver-id> [--link wired|dongle] [--yes]

For every link and every writable setting it writes a different valid value,
reads it back from the mouse, then writes the original back. The mouse ends
as it started (a snapshot is saved first, just in case). Paste the printed
report into the pull request. Not part of `python3 -m unittest`: it needs
the real mouse.
"""

import argparse
import os
import sys
from collections import namedtuple

from mousectl import cli
from mousectl.core import store
from mousectl.core.driver import DeviceError
from mousectl.core.settings import copy_raw

Result = namedtuple("Result", "name ok detail")


def other_value(s, raw, mode):
    """A valid value different from the current one, or None."""
    cur = s.get(raw, mode)
    for d in (1, -1):
        v = s.kind.nudge(cur, d)
        if v != cur:
            return v
    return None


def _hex(raw):
    return {k: bytes(v).hex() for k, v in raw.items()}


def check_setting(s, st, original, v):
    mode, units = s.mode, tuple(st.units)
    before = s.read(units)
    after = copy_raw(before)
    st.assign(after, mode, v)
    store.apply(s, before, after)
    got = st.get(s.read(units), mode)
    store.apply(s, s.read(units), {u: bytearray(original[u]) for u in units})
    restored = s.read(units) == {u: original[u] for u in units}
    ok = got == v and restored
    detail = f"{st.kind.format(st.get(original, mode))} -> {st.kind.format(v)}"
    if got != v:
        detail += f", read back {got!r}"
    if not restored:
        detail += ", original not restored"
    return Result(st.key, ok, detail)


def check_session(s):
    """Run every check over one open session; the mouse is restored after."""
    drv, mode = s.link.driver, s.mode
    original = s.read()
    results = [Result("read all units", set(original) == set(drv.units),
                      ", ".join(drv.unit_name(u) for u in original)),
               Result("two reads agree", s.read() == original, "")]
    try:
        for st in drv.settings:
            if st.read_only or not st.enabled(original, mode):
                continue
            v = other_value(st, original, mode)
            if v is not None:
                results.append(check_setting(s, st, original, v))
    finally:
        store.apply(s, s.read(), copy_raw(original))
    now = s.read()
    results.append(Result("mouse left as found", now == original,
                          "" if now == original else f"was {_hex(original)}, now {_hex(now)}"))
    return results


def report(drv, mode, results):
    lines = [f"### {drv.name} (`{drv.id}`) over {mode}", "",
             "| check | result | detail |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.name} | {'pass' if r.ok else '**FAIL**'} | {r.detail} |")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m tests.hardware_check", description=__doc__.split("\n\n")[0])
    p.add_argument("-m", "--mouse", required=True, help="driver id or alias")
    p.add_argument("--link", help="only check this connection")
    p.add_argument("--yes", action="store_true", help="don't ask before writing to the mouse")
    args = p.parse_args(argv)
    mouse = cli.select_mice(args, one=True)[0]
    if not args.yes and input(f"This writes every setting of your {mouse.name} and puts it back. "
                              f"Continue? [y/N] ").strip().lower() != "y":
        return 1
    failed = False
    for link in cli.links_of(mouse, args):
        try:
            with link.open() as s:
                path = os.path.join(store.driver_dir(mouse.driver), f"hardware-check-{link.mode}.json")
                store.write_snapshot(path, mouse.driver, s.mode, s.read())
                print(f"(saved {path}; `mousectl restore {path}` if anything goes wrong)", file=sys.stderr)
                results = check_session(s)
        except DeviceError as e:
            results = [Result("open link", False, str(e))]
        print(report(mouse.driver, link.mode, results) + "\n")
        failed |= not all(r.ok for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
