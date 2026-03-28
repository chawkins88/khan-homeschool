from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
RESEARCH_DIR = BASE_DIR / "research" / "khan"
SESSION_DIR = RESEARCH_DIR / "session"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(
            "Playwright is not installed in this environment. Install it first, then rerun."
        ) from exc

    captured = []
    headers_written = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(request):
            nonlocal headers_written
            url = request.url
            if "graphql" not in url:
                return
            entry = {
                "url": url,
                "method": request.method,
                "headers": request.headers,
                "post_data": request.post_data,
            }
            try:
                body = json.loads(request.post_data or "{}")
            except Exception:
                body = {"raw": request.post_data}
            entry["body"] = body
            captured.append(entry)
            if not headers_written:
                sanitized = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in {"cookie", "content-length"}
                }
                (SESSION_DIR / "headers.json").write_text(json.dumps(sanitized, indent=2))
                headers_written = True

        page.on("request", handle_request)
        page.goto("https://www.khanacademy.org/", wait_until="domcontentloaded")
        print("Log into Khan in the opened browser if needed, then browse the progress pages.")
        print("When done, press Enter here to save cookies and captured requests.")
        input()

        cookies = context.cookies()
        (SESSION_DIR / "cookies.json").write_text(json.dumps(cookies, indent=2))
        (SESSION_DIR / "captured_requests.json").write_text(json.dumps(captured, indent=2))

        operations = []
        for item in captured:
            body = item.get("body") or {}
            if isinstance(body, dict) and (body.get("operationName") or body.get("query") or body.get("extensions")):
                operations.append(
                    {
                        "url": item.get("url"),
                        "operationName": body.get("operationName"),
                        "body": body,
                    }
                )
        (SESSION_DIR / "operations.json").write_text(json.dumps(operations, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
