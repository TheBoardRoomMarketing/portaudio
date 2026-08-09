#!/usr/bin/env python3
"""Inline the prototype into a single self-contained HTML file.

The multi-file version in this directory is the one to develop against. This
script produces build/trading-journal-prototype.html for sharing — one file,
no external requests, works from a file:// URL or any static host.

Usage:  python3 prototype/build_bundle.py
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "dist", "trading-journal-prototype.html")
# Fragment variant: same page without the document skeleton, for hosts that
# supply their own <head>/<body> wrapper.
OUT_FRAGMENT = os.path.join(ROOT, "dist", "trading-journal-prototype.fragment.html")


def read(name):
    with open(os.path.join(HERE, name)) as fh:
        return fh.read()


def main():
    index = read("index.html")
    shell = re.search(r"<!-- APP-SHELL-START -->(.*?)<!-- APP-SHELL-END -->", index, re.S)
    if not shell:
        raise SystemExit("app shell markers not found in index.html")

    body = (
        "<title>Trading Journal v1 — prototype</title>\n"
        "<style>\n" + read("styles.css") + "\n</style>\n"
        + shell.group(1).strip() + "\n"
        "<script>\n" + read("data.js") + "\n</script>\n"
        "<script>\n" + read("app.js") + "\n</script>\n"
    )

    html = (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="description" content="Human-first trading journal prototype. '
        'All data is synthetic placeholder data.">\n'
        "<title>Trading Journal v1 — prototype</title>\n"
        "<style>\n" + read("styles.css") + "\n</style>\n</head>\n<body>\n"
        + shell.group(1).strip() + "\n"
        "<script>\n" + read("data.js") + "\n</script>\n"
        "<script>\n" + read("app.js") + "\n</script>\n"
        "</body>\n</html>\n"
    )

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write(html)
    with open(OUT_FRAGMENT, "w") as fh:
        fh.write(body)
    print(f"{OUT}  ({len(html) / 1024:.0f} KB)")
    print(f"{OUT_FRAGMENT}  ({len(body) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
