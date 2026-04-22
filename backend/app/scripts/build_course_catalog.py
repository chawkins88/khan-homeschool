"""Build a structured Khan course catalog from the authoritative GraphQL tree.

Strategy
--------
The previous version of this script tried to scrape Apollo cache / rendered
page text on each course landing page. That produced two problems:

1. Apollo cache on course landing pages was effectively empty for the catalog
   builder we had, so `node_count` was always 0.
2. The text-fallback regex harvested noise (unit mastery %, course challenge
   blurbs, duplicated navigation crumbs) and missed the real lesson tree.

The new approach drives the authenticated Chrome (CDP 9333) to each course
landing page, intercepts the `ContentForPath` GraphQL response, and extracts
the authoritative course → unit → lesson → [exercise / video / article] tree
with stable IDs, slugs, and titles. This is the same response Khan's own UI
uses to render the course.

Run::

    cd backend
    source .venv/bin/activate
    python app/scripts/build_course_catalog.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9333"
BASE_URL = "https://www.khanacademy.org"

# Course slug -> (khan url path, display name, khan subtitle on activity rows)
COURSES: dict[str, dict[str, str]] = {
    "cc-sixth-grade-math": {
        "path": "math/cc-sixth-grade-math",
        "display_name": "6th Grade Math",
        "khan_subtitle": "6th grade math",
    },
    "ms-physics": {
        "path": "science/ms-physics",
        "display_name": "Middle School Physics",
        "khan_subtitle": "Middle school physics",
    },
    "oer-project-big-history": {
        "path": "humanities/oer-project-big-history",
        "display_name": "OER Project: Big History",
        "khan_subtitle": "OER Project: Big History",
    },
    "new-6th-grade-reading-and-vocabulary": {
        "path": "ela/new-6th-grade-reading-and-vocabulary",
        "display_name": "6th Grade Reading & Vocab",
        "khan_subtitle": "6th grade reading and vocab",
    },
}

CATALOG_PATH = Path(__file__).resolve().parents[3] / "research" / "khan" / "course_catalog.json"
LEARNABLE_KINDS = {"Exercise", "Video", "Article", "TopicQuiz", "TopicUnitTest"}


def _capture_content_for_path(page, course_path: str) -> dict[str, Any]:
    """Navigate to the course page and return the ContentForPath response JSON."""
    captured: dict[str, Any] = {}

    def handle_response(resp):
        if "/api/internal/graphql/ContentForPath" not in resp.url:
            return
        if captured.get("data") is not None:
            return
        try:
            captured["data"] = resp.json()
            captured["url"] = resp.url
        except Exception as exc:
            captured["error"] = str(exc)

    page.on("response", handle_response)
    try:
        page.goto(f"{BASE_URL}/{course_path}", wait_until="networkidle", timeout=60000)
    finally:
        page.remove_listener("response", handle_response)

    # Apollo occasionally reuses an in-memory cached response without re-issuing
    # the request. Fall back to forcing a hard reload if nothing was captured.
    if "data" not in captured:
        def handle_retry(resp):
            if "/api/internal/graphql/ContentForPath" not in resp.url:
                return
            if captured.get("data") is not None:
                return
            try:
                captured["data"] = resp.json()
                captured["url"] = resp.url
            except Exception as exc:
                captured["error"] = str(exc)

        page.on("response", handle_retry)
        try:
            page.reload(wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
        finally:
            page.remove_listener("response", handle_retry)

    if "data" not in captured:
        raise RuntimeError(f"Did not observe ContentForPath response for {course_path}")
    return captured["data"]


def _build_item(node: dict[str, Any], unit_title: str, lesson_title: str | None) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "kind": node.get("__typename"),
        "slug": node.get("slug"),
        "title": node.get("translatedTitle") or node.get("title"),
        "url": node.get("urlWithinCurationNode") or node.get("relativeUrl") or node.get("canonicalUrl"),
        "unit": unit_title,
        "lesson": lesson_title,
        "progress_key": node.get("progressKey"),
        "exercise_length": node.get("exerciseLength"),
    }


def _flatten_course(course: dict[str, Any]) -> dict[str, Any]:
    """Flatten a ContentForPath course object into units, lessons, and items."""
    units: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    total_exercises = total_videos = total_articles = total_quizzes = 0

    for unit in course.get("unitChildren") or []:
        unit_title = unit.get("translatedTitle") or unit.get("title") or "(unit)"
        unit_record = {
            "id": unit.get("id"),
            "slug": unit.get("slug"),
            "title": unit_title,
            "url": unit.get("relativeUrl"),
            "lessons": [],
        }
        for child in unit.get("allOrderedChildren") or []:
            kind = child.get("__typename")
            if kind == "Lesson":
                lesson_title = child.get("translatedTitle") or child.get("title")
                lesson_record = {
                    "id": child.get("id"),
                    "slug": child.get("slug"),
                    "title": lesson_title,
                    "url": child.get("relativeUrl"),
                    "items": [],
                }
                for gc in child.get("curatedChildren") or []:
                    gc_kind = gc.get("__typename")
                    if gc_kind not in LEARNABLE_KINDS:
                        continue
                    item = _build_item(gc, unit_title, lesson_title)
                    lesson_record["items"].append(item)
                    items.append(item)
                    if gc_kind == "Exercise":
                        total_exercises += 1
                    elif gc_kind == "Video":
                        total_videos += 1
                    elif gc_kind == "Article":
                        total_articles += 1
                unit_record["lessons"].append(lesson_record)
            elif kind in LEARNABLE_KINDS:
                item = _build_item(child, unit_title, None)
                unit_record["lessons"].append({
                    "id": item["id"],
                    "slug": item["slug"],
                    "title": item["title"],
                    "url": item["url"],
                    "items": [item],
                    "standalone_kind": kind,
                })
                items.append(item)
                if kind in ("TopicQuiz", "TopicUnitTest"):
                    total_quizzes += 1
        units.append(unit_record)

    course_challenge = course.get("courseChallenge")
    if course_challenge:
        items.append(
            _build_item(course_challenge, "Course Challenge", None)
        )

    return {
        "units": units,
        "items": items,
        "totals": {
            "units": len(units),
            "lessons": sum(len(u["lessons"]) for u in units),
            "exercises": total_exercises,
            "videos": total_videos,
            "articles": total_articles,
            "quizzes": total_quizzes,
            "items_total": len(items),
        },
    }


def main() -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    catalog: dict[str, Any] = {}

    from app.services.khan_cdp_progress import open_khan_context

    with sync_playwright() as p:
        ctx, cleanup = open_khan_context(p, headless=True)
        page = ctx.new_page()

        try:
            for slug, meta in COURSES.items():
                print(f"\nCataloguing: {meta['display_name']} ({slug})")
                try:
                    payload = _capture_content_for_path(page, meta["path"])
                except Exception as exc:
                    print(f"  ERROR: {exc}")
                    catalog[slug] = {**meta, "slug": slug, "error": str(exc)}
                    continue

                content_route = (((payload.get("data") or {}).get("contentRoute")) or {})
                course = (content_route.get("listedPathData") or {}).get("course") or {}
                if not course:
                    catalog[slug] = {**meta, "slug": slug, "error": "no course in ContentForPath response"}
                    print("  no course in response")
                    continue

                flat = _flatten_course(course)
                catalog[slug] = {
                    **meta,
                    "slug": slug,
                    "course_id": course.get("id"),
                    "course_title": course.get("translatedTitle"),
                    "url": f"{BASE_URL}/{meta['path']}",
                    "totals": flat["totals"],
                    "units": flat["units"],
                    "items": flat["items"],
                }
                t = flat["totals"]
                print(
                    f"  units={t['units']}  lessons={t['lessons']}  "
                    f"exercises={t['exercises']}  videos={t['videos']}  articles={t['articles']}  "
                    f"items_total={t['items_total']}"
                )
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                cleanup()
            except Exception:
                pass

    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False))
    print(f"\nCatalog written to {CATALOG_PATH}")


if __name__ == "__main__":
    main()
