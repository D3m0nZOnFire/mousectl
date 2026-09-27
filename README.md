# mousectl

Configure gaming mice on Linux over `/dev/hidraw*`, without vendor software.
Pure Python 3 standard library: no packages, no venv.

Supported:

| id                | mouse             | links                  | alias    |
|-------------------|-------------------|------------------------|----------|
| `attackshark-x11` | Attack Shark X11  | wired `1d57:fa55`, dongle `1d57:fa60` | `ashark` |
| `rapoo-vt3pro`    | Rapoo VT3 PRO     | wired `24ae:4431`, dongle `24ae:1231` | `vt3pro` |

## Install

```bash
./install.sh            # symlinks mousectl, ashark, vt3pro into ~/.local/bin
mousectl install-udev   # only if you get permission errors
```

## Use

```bash
mousectl list                          # connected mice and their links
mousectl status [--json]               # every connected mouse
mousectl keys -m ashark                # what a mouse supports, with ranges
mousectl get -m vt3pro polling dpi.active
mousectl set -m ashark dpi.stage3=3200 dpi.active=3 light.mode=static light.color=ff8800
mousectl set -m ashark dpi.stage5=off  # stages that can be disabled
mousectl battery [--json]
mousectl tui                           # all mice in one TUI ('m' switches mouse)
mousectl save [FILE] / restore FILE    # raw snapshots
```

`-m` takes a driver id or alias and can be left out when only one mouse is
connected. `--link wired|dongle` pins the connection; otherwise the first one
that answers is used (settings live in the mouse, so either will do).

Before the first write to a mouse, its untouched settings are saved to
`~/.config/mousectl/<id>/auto-backup.json` (`mousectl restore` it to go back).

The old command lines still work: `ashark dpi --stages 800,1600`,
`vt3pro sensor --ripple off`, … (also as `mousectl ashark …`).

## Layout

```
bin/mousectl            launcher (also run as ashark / vt3pro)
mousectl/core/          hidraw, Driver/Session, Setting schema, backups
mousectl/drivers/       one module per mouse
mousectl/tui/           the schema-driven curses TUI
tests/                  python3 -m unittest
```

New mouse? See [docs/ADDING_A_DRIVER.md](docs/ADDING_A_DRIVER.md).

## Roadmap

- Rapoo VT0 Air MAX / VT3 Air driver (port of `rapoo-software-linux/backend/rapoo`).
- Declarative profiles (`mousectl apply prefs.toml`), auto-applied on hotplug
  via a udev-triggered systemd user unit.
- Waybar battery module on top of `mousectl battery --json`.

## Credits

- Attack Shark X11 protocol: [HarukaYamamoto0/attack-shark-x11-driver](https://github.com/HarukaYamamoto0/attack-shark-x11-driver) (MIT)
- Rapoo protocol research: [pedro3z0/rapoo-software-linux](https://github.com/pedro3z0/rapoo-software-linux) (MIT)

## License

MIT, see [LICENSE](LICENSE).
