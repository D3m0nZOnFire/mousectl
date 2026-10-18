# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**[Report a vulnerability](https://github.com/D3m0nZOnFire/mousectl/security/advisories/new)**
(repository → Security → Report a vulnerability).

Don't open a public issue or pull request for a security problem until a fix is released.

Include what you found, how to reproduce it, the version or commit you tested, and the impact you expect.
You'll get a reply as soon as possible. Once a fix is released, you'll be credited in the advisory unless you'd rather not be.

## Supported versions

Only the latest release (or `main`) gets security fixes. Update with `git pull` in your checkout.

## Scope

In scope: mousectl itself, `install.sh`, and the udev rule written by `mousectl install-udev` (it decides who can
access the mouse's `/dev/hidraw*` nodes).

Out of scope: firmware bugs in the mice themselves.
