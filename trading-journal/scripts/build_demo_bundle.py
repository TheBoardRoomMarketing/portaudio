#!/usr/bin/env python3
"""Build the shareable offline bundle.

Regenerates web/data.js from the live database, then inlines the web directory
into a single self-contained HTML file. The bundle renders the same interface
as the running application; it simply cannot save, because there is no server
behind it.

Usage:  python3 scripts/build_demo_bundle.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from journal import config, db, repo  # noqa: E402

WEB = ROOT / "web"
OUT = ROOT / "data" / "trading-journal-demo.html"
OUT_FRAGMENT = ROOT / "data" / "trading-journal-demo.fragment.html"


def regenerate_data_js() -> int:
    conn = db.connect()
    try:
        payload = repo.journal_payload(conn)
    finally:
        conn.close()
    payload["meta"]["source"] = "offline demo bundle"
    with open(WEB / "data.js", "w") as fh:
        fh.write("// GENERATED — do not edit by hand.\n")
        fh.write("// Built by scripts/build_demo_bundle.py from the live schema.\n")
        fh.write("// Synthetic placeholder data only; no real trading records.\n")
        fh.write("window.JOURNAL = ")
        json.dump(payload, fh, separators=(",", ":"), default=str)
        fh.write(";\n")
    return len(payload["sessions"])


def main() -> int:
    sessions = regenerate_data_js()

    index = (WEB / "index.html").read_text()
    shell = re.search(r"<!-- APP-SHELL-START -->(.*?)<!-- APP-SHELL-END -->", index, re.S)
    if not shell:
        raise SystemExit("app shell markers not found in web/index.html")

    body = (
        "<title>Trading Journal — offline demo</title>\n"
        "<style>\n" + (WEB / "styles.css").read_text() + "\n</style>\n"
        + shell.group(1).strip() + "\n"
        "<script>\n" + (WEB / "data.js").read_text() + "\n</script>\n"
        "<script>\n" + (WEB / "app.js").read_text() + "\n</script>\n"
    )

    html = (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="description" content="Trading Journal interface running on synthetic '
        'placeholder data. No real trading records.">\n'
        "<title>Trading Journal — offline demo</title>\n"
        "<style>\n" + (WEB / "styles.css").read_text() + "\n</style>\n"
        "</head>\n<body>\n"
        + shell.group(1).strip() + "\n"
        "<script>\n" + (WEB / "data.js").read_text() + "\n</script>\n"
        "<script>\n" + (WEB / "app.js").read_text() + "\n</script>\n"
        "</body>\n</html>\n"
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    OUT_FRAGMENT.write_text(body)
    print(f"{OUT} ({len(html) / 1024:.0f} KB, {sessions} sessions)")
    print(f"{OUT_FRAGMENT} ({len(body) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
