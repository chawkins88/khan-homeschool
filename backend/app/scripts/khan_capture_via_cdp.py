from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parents[3]
RESEARCH_DIR = BASE_DIR / "research" / "khan"
SESSION_DIR = RESEARCH_DIR / "session"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

CDP_URL = "http://127.0.0.1:9333"
TARGET_SUBSTRING = "khanacademy.org/profile/gustywarrior/progress"
GRAPHQL_MARKER = "/api/internal/graphql/"


def main() -> None:
    captured: list[dict] = []
    saved_headers = None

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            raise SystemExit("No browser contexts found via CDP")

        target_page = None
        target_context = None
        for context in contexts:
            for page in context.pages:
                if TARGET_SUBSTRING in page.url:
                    target_page = page
                    target_context = context
                    break
            if target_page:
                break

        if not target_page or not target_context:
            raise SystemExit(f"Target Khan page not found via CDP: {TARGET_SUBSTRING}")

        def handle_request(request):
            nonlocal saved_headers
            if GRAPHQL_MARKER not in request.url:
                return
            entry = {
                "url": request.url,
                "method": request.method,
                "headers": request.headers,
                "post_data": request.post_data,
            }
            parsed = urlparse(request.url)
            qs = parse_qs(parsed.query)
            entry["operationName"] = parsed.path.rstrip("/").split("/")[-1]
            entry["query"] = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
            try:
                entry["post_json"] = json.loads(request.post_data) if request.post_data else None
            except Exception:
                entry["post_json"] = None
            captured.append(entry)
            if saved_headers is None:
                saved_headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in {"cookie", "content-length"}
                }

        target_page.on("request", handle_request)
        target_page.reload(wait_until="domcontentloaded", timeout=60000)
        target_page.wait_for_timeout(8000)

        cookies = target_context.cookies()
        (SESSION_DIR / "cookies.json").write_text(json.dumps(cookies, indent=2))
        if saved_headers:
            (SESSION_DIR / "headers.json").write_text(json.dumps(saved_headers, indent=2))
        (SESSION_DIR / "captured_requests.json").write_text(json.dumps(captured, indent=2))

        operations = []
        for item in captured:
            body = item.get("post_json")
            if body and isinstance(body, dict):
                operations.append(
                    {
                        "url": item["url"],
                        "operationName": body.get("operationName") or item.get("operationName"),
                        "body": body,
                    }
                )
                continue

            q = item.get("query") or {}
            hash_value = q.get("hash")
            variables_raw = q.get("variables")
            if hash_value:
                try:
                    variables = json.loads(variables_raw) if variables_raw else {}
                except Exception:
                    variables = {"_raw": variables_raw}
                operations.append(
                    {
                        "url": item["url"],
                        "operationName": item.get("operationName"),
                        "body": {
                            "operationName": item.get("operationName"),
                            "variables": variables,
                            "extensions": {
                                "persistedQuery": {
                                    "version": 1,
                                    "sha256Hash": hash_value,
                                }
                            },
                        },
                    }
                )

        (SESSION_DIR / "operations.json").write_text(json.dumps(operations, indent=2))
        (SESSION_DIR / "capture_summary.json").write_text(
            json.dumps(
                {
                    "captured_requests": len(captured),
                    "replayable_operations": len(operations),
                    "target_url": target_page.url,
                },
                indent=2,
            )
        )

        print(json.dumps({"ok": True, "captured": len(captured), "operations": len(operations)}, indent=2))


if __name__ == "__main__":
    main()
