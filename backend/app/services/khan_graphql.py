from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

BASE_DIR = Path(__file__).resolve().parents[3]
RESEARCH_DIR = BASE_DIR / "research" / "khan"
SESSION_DIR = RESEARCH_DIR / "session"
COOKIES_PATH = SESSION_DIR / "cookies.json"
HEADERS_PATH = SESSION_DIR / "headers.json"
OPERATIONS_PATH = SESSION_DIR / "operations.json"

DEFAULT_GRAPHQL_URL = "https://www.khanacademy.org/api/internal/graphql"


@dataclass
class KhanOperation:
    operation_name: str
    sha256_hash: Optional[str]
    variables: Dict[str, Any]
    raw_body: Dict[str, Any]


class KhanGraphQLClient:
    def __init__(
        self,
        graphql_url: str = DEFAULT_GRAPHQL_URL,
        cookies_path: Path = COOKIES_PATH,
        headers_path: Path = HEADERS_PATH,
    ):
        self.graphql_url = graphql_url
        self.cookies_path = Path(cookies_path)
        self.headers_path = Path(headers_path)
        self.session = requests.Session()
        self._load_cookies()
        self.base_headers = self._load_headers()

    def _load_cookies(self) -> None:
        if not self.cookies_path.exists():
            return
        data = json.loads(self.cookies_path.read_text())
        for cookie in data:
            if not cookie.get("name"):
                continue
            self.session.cookies.set(
                cookie["name"],
                cookie.get("value", ""),
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )

    def _load_headers(self) -> Dict[str, str]:
        if not self.headers_path.exists():
            return {}
        data = json.loads(self.headers_path.read_text())
        allowed = {
            "user-agent",
            "accept",
            "accept-language",
            "content-type",
            "origin",
            "referer",
            "x-ka-fkey",
            "x-ka-locale",
            "x-requested-with",
        }
        return {k: v for k, v in data.items() if k.lower() in allowed}

    def post(self, body: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> requests.Response:
        headers = dict(self.base_headers)
        headers.setdefault("content-type", "application/json")
        headers.setdefault("accept", "application/json")
        if extra_headers:
            headers.update(extra_headers)
        return self.session.post(self.graphql_url, headers=headers, json=body, timeout=30)

    def replay_operation(self, operation: KhanOperation) -> Dict[str, Any]:
        response = self.post(operation.raw_body)
        response.raise_for_status()
        return response.json()


def load_saved_operations(path: Path = OPERATIONS_PATH) -> list[KhanOperation]:
    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    out = []
    for item in raw:
        body = item.get("body") or {}
        out.append(
            KhanOperation(
                operation_name=item.get("operationName") or body.get("operationName") or "unknown",
                sha256_hash=((body.get("extensions") or {}).get("persistedQuery") or {}).get("sha256Hash"),
                variables=body.get("variables") or {},
                raw_body=body,
            )
        )
    return out
