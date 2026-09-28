"""Checks that a new-driver pull request filled in its template.

Run by .github/workflows/pr-checklist.yml with the PR title and body in
PR_TITLE / PR_BODY and the files the PR adds as arguments.
"""

import os
import re
import sys

MARKER = "<!-- new-driver -->"
DRIVER_FILE = re.compile(r"mousectl/drivers/(?!__init__)\w+\.py")


def problems(title, body, added):
    body = body.replace("\r\n", "\n")
    errs = []
    new_driver = [f for f in added if DRIVER_FILE.fullmatch(f)]
    if MARKER not in body:
        if new_driver:
            errs.append(f"adds {', '.join(new_driver)}: use the new-driver PR template "
                        "(`gh pr create --template new-driver.md`, or ?template=new-driver.md on the PR URL)")
        return errs
    if not re.fullmatch(r"Add .+ driver", title.strip()):
        errs.append(f'title should be "Add <Vendor Model> driver", not "{title}"')
    text = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    for field, value in re.findall(r"^- \*\*(.+?):\*\*[ \t]*(.*)$", text, re.M):
        if not value.strip():
            errs.append(f"empty field: {field}")
    for item in re.findall(r"^\s*- \[ \] (.*)$", text, re.M):
        errs.append(f"unticked: {item}")
    if "| check | result |" not in text:
        errs.append("paste the hardware_check report (python3 -m tests.hardware_check -m <driver-id>)")
    elif "**FAIL**" in text:
        errs.append("the hardware_check report has FAIL rows: fix them or explain under known issues")
    return errs


def main():
    errs = problems(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), sys.argv[1:])
    for e in errs:
        print(f"::error::{e}")
    if not errs:
        print("pull request form complete")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
