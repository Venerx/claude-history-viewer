#!/usr/bin/env python3
"""
Claude History Viewer
=====================
Place your exported conversations.json in a source/ directory, then run:

    python3 app.py

Requires Python 3.9+. No third-party packages needed.
"""
import sys
import threading
import webbrowser
from pathlib import Path

DB_PATH = Path("history.db")
SOURCE  = Path("source") / "conversations.json"
PORT    = 5174


def main() -> None:
    if not SOURCE.exists():
        print(f"Error: {SOURCE} not found.")
        print("Export your Claude history and place conversations.json in source/")
        sys.exit(1)

    if not DB_PATH.exists():
        print("First run — building search index (may take ~30 s for large exports)…")
        from build_db import build
        build(SOURCE, DB_PATH)

    from server import serve

    url = f"http://127.0.0.1:{PORT}"
    print(f"Claude History → {url}  (Ctrl-C to quit)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    serve(port=PORT, db_path=DB_PATH)


if __name__ == "__main__":
    main()
