from __future__ import annotations

import json
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parents[3]
RESEARCH_DIR = BASE_DIR / "research" / "khan"
SESSION_DIR = RESEARCH_DIR / "session"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

TARGET_URL = "https://www.khanacademy.org/profile/gustywarrior/progress"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"


def browser_eval(js_expr: str) -> str:
    payload = {
        "kind": "evaluate",
        "targetId": "3",
        "fn": f"() => ({js_expr})",
    }
    resp = requests.post(
        "http://127.0.0.1:18789/tools/browser",
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Browser evaluate failed: {data}")
    return data["result"]


def parse_cookie_header(cookie_header: str) -> list[dict]:
    jar = SimpleCookie()
    jar.load(cookie_header)
    out = []
    for morsel in jar.values():
        out.append(
            {
                "name": morsel.key,
                "value": morsel.value,
                "domain": ".khanacademy.org",
                "path": "/",
            }
        )
    return out


def main() -> None:
    result = json.loads(
        browser_eval(
            "JSON.stringify({"
            "href: location.href,"
            "title: document.title,"
            "userAgent: navigator.userAgent,"
            "cookie: document.cookie,"
            "resources: performance.getEntriesByType('resource').map(e => e.name).filter(n => n.includes('/api/internal/graphql/')),"
            "cacheKeys: Object.keys((window.__APOLLO_CLIENT__ && window.__APOLLO_CLIENT__.cache && window.__APOLLO_CLIENT__.cache.data && window.__APOLLO_CLIENT__.cache.data.data) || {})"
            "}, null, 2)"
        )
    )

    if result.get("href") != TARGET_URL:
        raise SystemExit(f"Expected target URL {TARGET_URL}, got {result.get('href')}")

    cookies = parse_cookie_header(result.get("cookie", ""))
    (SESSION_DIR / "cookies.json").write_text(json.dumps(cookies, indent=2))

    headers = {
        "user-agent": result.get("userAgent") or USER_AGENT,
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://www.khanacademy.org",
        "referer": TARGET_URL,
        "x-requested-with": "XMLHttpRequest",
    }
    (SESSION_DIR / "headers.json").write_text(json.dumps(headers, indent=2))

    resources = result.get("resources", [])
    parsed_resources = []
    for url in resources:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        operation_name = parsed.path.rstrip("/").split("/")[-1]
        parsed_resources.append(
            {
                "url": url,
                "operationName": operation_name,
                "hash": qs.get("hash", [None])[0],
                "variables": qs.get("variables", [None])[0],
                "lang": qs.get("lang", [None])[0],
                "app": qs.get("app", [None])[0],
            }
        )

    (SESSION_DIR / "resource_urls.json").write_text(json.dumps(parsed_resources, indent=2))

    operations = []
    for item in parsed_resources:
        if not item["hash"]:
            continue
        body = {
            "operationName": item["operationName"],
            "variables": json.loads(item["variables"]) if item.get("variables") else {},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": item["hash"],
                }
            },
        }
        operations.append(
            {
                "url": item["url"],
                "operationName": item["operationName"],
                "body": body,
            }
        )

    (SESSION_DIR / "operations.json").write_text(json.dumps(operations, indent=2))
    (SESSION_DIR / "capture_summary.json").write_text(
        json.dumps(
            {
                "title": result.get("title"),
                "url": result.get("href"),
                "resourceCount": len(resources),
                "graphqlResourceCount": len(parsed_resources),
                "hashedOperationCount": len(operations),
                "cacheKeyCount": len(result.get("cacheKeys", [])),
            },
            indent=2,
        )
    )

    print(json.dumps({"ok": True, "saved": str(SESSION_DIR), "operations": len(operations)}, indent=2))


if __name__ == "__main__":
    main()
