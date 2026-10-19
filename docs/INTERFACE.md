# Scripting interface

Bar widgets, GUIs and scripts drive mousectl through its CLI with `--json`.
This page is the contract. Within one interface version, fields are only
ever added, never renamed, removed or retyped, so ignore keys you don't know.
Anything that would break a consumer bumps the version.

Every JSON document carries the version at the top level:

```json
{ "interface": 1, ... }
```

Check it and refuse (or warn) when it isn't a version you were written for.
`mousectl --version` gives the program version (for bug reports), which is a
different number.

## Conventions

- **Exit codes.** `0` when a document was printed, including one that
  reports per-mouse errors or no mice at all. `1` when `get`/`set` could not
  reach the mouse (the document then has an `error`). Any other non-zero
  code is a usage error (unknown mouse or key, bad value, several mice
  without `-m`). In that case nothing goes to stdout and a one-line message
  goes to stderr.
- **No mouse is not an error.** `list`, `status`, `battery` and `keys`
  print `"mice": []` and exit 0, also with `-m` naming a mouse that isn't
  plugged in. Only `get`/`set` need exactly one mouse.
- **Mouse ids.** `mouse` is the stable driver id (`attackshark-x11`). Pass
  it back with `-m`. `name` is for display.
- **Links.** `link` is the connection used (`wired`, `dongle`) and `path`
  is its `/dev/hidrawN`. Both are `null` when no link answered.
- **Values.** Numbers are JSON numbers, on/off settings are `true`/`false`,
  choices are their value (a number or string), and colours are
  `"#rrggbb"`. A setting that is switched off (e.g. a disabled DPI stage)
  is the string `"off"`. Any value can go straight back into `set`: a
  string as-is, anything else as its JSON text (`dpi.active=3`,
  `angle_snap=true`, `light.color=#ff8800`, `dpi.stage5=off`).
- **Battery.** `null` when there is nothing to report, otherwise
  `{"percent": 0-100, "state": ...}` with `state` one of `charging`,
  `discharging`, `full` and `unknown`.
- **Errors.** `null`, or `{"code": ..., "message": ...}`. The `message` is
  for people. Branch on `code`:

  | code         | meaning                                   | what a widget should do |
  |--------------|-------------------------------------------|-------------------------|
  | `permission` | can't open the hidraw node                | tell the user to run `mousectl install-udev` |
  | `no-answer`  | mouse asleep, out of range or busy        | show the last value or a placeholder, retry on the next poll |
  | `io`         | anything else                             | show the message |

## Commands

### `mousectl list --json`

Connected mice. This doesn't talk to the mice, so it's cheap.

```json
{
  "interface": 1,
  "mice": [
    {"mouse": "attackshark-x11", "name": "Attack Shark X11",
     "links": [{"mode": "dongle", "path": "/dev/hidraw6"}]}
  ]
}
```

### `mousectl battery --json [-m MOUSE] [--link MODE] [--timeout S]`

One entry per connected mouse. Each link is tried in turn: the first
reading wins. A mouse on its cable with no level to report comes back as
`"battery": null, "error": null`. Otherwise you get the error from the last
link tried.

```json
{
  "interface": 1,
  "mice": [
    {"mouse": "attackshark-x11", "name": "Attack Shark X11",
     "link": "dongle", "path": "/dev/hidraw6",
     "battery": {"percent": 80, "state": "discharging"},
     "error": null}
  ]
}
```

Some mice (the Attack Shark X11) only send their level every few seconds,
so this can block for up to `--timeout` (default 5 s). Poll it every 30-60 s
in the background, never on the bar's render path.

### `mousectl status --json [-m MOUSE] [--link MODE] [--no-battery]`

Every setting and the battery of every connected mouse. A battery failure
only nulls `battery`. It doesn't set `error`, which is reserved for "could
not read the settings at all" (and then `settings` is `null`).

```json
{
  "interface": 1,
  "mice": [
    {"mouse": "attackshark-x11", "name": "Attack Shark X11",
     "link": "dongle", "path": "/dev/hidraw6",
     "settings": {"dpi.stage1": 800, "dpi.stage5": "off", "dpi.active": 2,
                  "polling": 1000, "light.color": "#ff8800", "...": "..."},
     "battery": {"percent": 80, "state": "discharging"},
     "error": null}
  ]
}
```

### `mousectl keys --json [-m MOUSE]`

The settings schema, enough to build a settings UI without knowing the
mouse. This doesn't talk to the mice. Settings come in display order.

```json
{
  "interface": 1,
  "mice": [
    {"mouse": "attackshark-x11", "name": "Attack Shark X11",
     "settings": [
       {"key": "dpi.stage1", "label": "Stage 1", "group": "DPI", "kind": "number",
        "min": 50, "max": 26000, "values": [50, 100, "..."], "unit": "DPI",
        "read_only": false, "can_disable": true, "help": ""},
       {"key": "polling", "label": "Polling rate", "group": "Performance", "kind": "choice",
        "values": [125, 250, 500, 1000], "unit": "Hz",
        "read_only": false, "can_disable": false, "help": ""}
     ]}
  ]
}
```

`kind` is one of:

- `number`: comes with `min`, `max` and `values`, the exact allowed grid.
  Some mice get coarser at high DPI, so snap to `values` rather than
  stepping from `min`.
- `choice`: comes with `values`.
- `bool`
- `color`

`can_disable` means `"off"` is also accepted.

### `mousectl get --json -m MOUSE KEY...`

```json
{"interface": 1, "mouse": "attackshark-x11", "name": "Attack Shark X11",
 "link": "dongle", "path": "/dev/hidraw6",
 "settings": {"polling": 1000, "dpi.active": 2}, "error": null}
```

If no link answers, `link`, `path` and `settings` are `null`, `error` is
set, and the exit code is 1.

### `mousectl set --json -m MOUSE KEY=VALUE...`

The same document as `get`, with the values as written, plus `changed`
(`false` when the mouse already had them and nothing was sent). The
values are checked before anything is written: a bad one is a usage error
and nothing is changed.

```json
{"interface": 1, "mouse": "attackshark-x11", "name": "Attack Shark X11",
 "link": "dongle", "path": "/dev/hidraw6",
 "settings": {"dpi.active": 3}, "changed": true, "error": null}
```

## Notes for tool authors

- Call `mousectl` from `$PATH` and don't import the Python package. The
  internals aren't part of this contract.
- Don't run two mousectl processes against one mouse at the same time.
  Dongles share one reply register, and overlapping requests read each
  other's answers. Serialise your calls.
- Permission errors go away for good after `mousectl install-udev`.
