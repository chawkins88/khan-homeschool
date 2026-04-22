"""One-time login helper for the backend-owned Chromium profile.

This can be invoked two ways:

* **From a terminal** (for manual seeding):

      python -m app.scripts.khan_login

  It will prompt "Press Enter when finished..." — you can also just close
  the Chromium window.

* **From the FastAPI backend** (via the Connect button). The server spawns
  this module with no TTY, in which case it skips the prompt and simply
  exits once you close the Chromium window.

In both cases it launches a visible Chromium against the persistent profile
directory (``~/.cache/khan-homeschool/chromium-profile`` by default, override
with ``KHAN_PROFILE_DIR``). After you sign in and close the window, the auth
cookies remain in that directory and the backend uses them headlessly.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.services.khan_cdp_progress import (
    DEFAULT_PROFILE_DIR,
    DEFAULT_TARGET,
    _launch_persistent_context,
)


def _await_stdin_enter(stop_flag: dict) -> None:
    """Blocking read of one line from stdin; sets stop_flag['stop']=True."""
    try:
        sys.stdin.readline()
    except Exception:
        pass
    stop_flag["stop"] = True


def main() -> int:
    profile_dir = Path(os.getenv("KHAN_PROFILE_DIR") or DEFAULT_PROFILE_DIR).expanduser()
    interactive = sys.stdin.isatty()

    print(f"Using persistent profile: {profile_dir}")
    print(f"Opening: {DEFAULT_TARGET}")
    print("-" * 64)
    print("Sign in to Khan Academy in the Chromium window that just opened.")
    if interactive:
        print("When you're done, either close the Chromium window or press Enter.")
    else:
        print("When you're done, close the Chromium window to finish.")
    print("-" * 64)
    sys.stdout.flush()

    with sync_playwright() as p:
        ctx = _launch_persistent_context(p, profile_dir, headless=False)

        stop_flag = {"stop": False}

        def _mark_closed(*_args, **_kwargs):
            stop_flag["stop"] = True

        ctx.on("close", _mark_closed)

        try:
            page = ctx.new_page()
            page.on("close", _mark_closed)
            try:
                page.goto(DEFAULT_TARGET, wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                print(f"(warning: initial navigation error: {exc})", file=sys.stderr)

            # Background thread listens for Enter on stdin when interactive.
            if interactive:
                t = threading.Thread(target=_await_stdin_enter, args=(stop_flag,), daemon=True)
                t.start()

            # Drive Playwright's event loop until the user closes the window
            # (or hits Enter when interactive). We intentionally use Playwright
            # wait primitives here -- time.sleep() does NOT pump the sync
            # dispatcher, so context/page 'close' events would never fire.
            while not stop_flag["stop"]:
                try:
                    pages = ctx.pages
                except Exception:
                    break
                if not pages:
                    break
                probe = pages[0]
                try:
                    probe.wait_for_timeout(500)
                except Exception:
                    break
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    print(f"\nSession cookies saved in {profile_dir}")
    print("The backend will now use this profile headlessly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
