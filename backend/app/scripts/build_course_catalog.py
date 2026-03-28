"""
Build a local Khan course catalog by visiting each course page in the
authenticated CDP browser and extracting the unit/content tree.

Run once:
  cd backend
  . .venv/bin/activate
  python app/scripts/build_course_catalog.py
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9333"

# Courses to catalog (slug -> display name)
COURSES = {
    "cc-sixth-grade-math": "6th Grade Math",
    "ms-physics": "Middle School Physics",
    "oer-project-big-history": "OER Project: Big History",
}

BASE_URL = "https://www.khanacademy.org"
CATALOG_PATH = Path(__file__).resolve().parents[3] / "research" / "khan" / "course_catalog.json"


def extract_course_tree(page, slug: str) -> dict:
    url = f"{BASE_URL}/{slug}"
    print(f"  → navigating to {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    return page.evaluate("""(slug) => {
        const data = (window.__APOLLO_CLIENT__ && window.__APOLLO_CLIENT__.cache &&
            window.__APOLLO_CLIENT__.cache.data && window.__APOLLO_CLIENT__.cache.data.data) || {};

        // Collect all Topic and Exercise nodes
        const nodes = {};
        for (const [k, v] of Object.entries(data)) {
            if (!v || typeof v !== 'object') continue;
            if (v.__typename === 'Topic' && v.slug) {
                nodes[k] = {
                    id: v.id || null,
                    slug: v.slug || null,
                    title: v.translatedTitle || v.translatedStandaloneTitle || null,
                    description: v.translatedDescription || null,
                    kind: 'Topic',
                    childKeys: [],
                };
            }
            if (v.__typename === 'Exercise' && (v.translatedTitle || v.id)) {
                nodes[k] = {
                    id: v.id || null,
                    slug: v.slug || null,
                    title: v.translatedTitle || v.translatedStandaloneTitle || null,
                    kind: 'Exercise',
                };
            }
            if (v.__typename === 'Video' && (v.translatedTitle || v.id)) {
                nodes[k] = {
                    id: v.id || null,
                    slug: v.slug || null,
                    title: v.translatedTitle || v.translatedStandaloneTitle || null,
                    kind: 'Video',
                };
            }
            if (v.__typename === 'Article' && (v.translatedTitle || v.id)) {
                nodes[k] = {
                    id: v.id || null,
                    slug: v.slug || null,
                    title: v.translatedTitle || v.translatedStandaloneTitle || null,
                    kind: 'Article',
                };
            }
        }

        // Wire parent→child
        for (const [k, v] of Object.entries(data)) {
            if (!v || typeof v !== 'object') continue;
            if (!nodes[k]) continue;
            const childrenField = v.children || v.unitChildren || v.unitItems || [];
            const childRefs = Array.isArray(childrenField) ? childrenField : [];
            for (const ref of childRefs) {
                if (ref && ref.id && nodes[ref.id]) {
                    nodes[k].childKeys = nodes[k].childKeys || [];
                    if (!nodes[k].childKeys.includes(ref.id)) {
                        nodes[k].childKeys.push(ref.id);
                    }
                }
            }
        }

        // Find root topic matching the slug
        const rootKey = Object.keys(data).find(k => {
            const v = data[k];
            return v && v.__typename === 'Topic' && v.slug === slug;
        });

        // Flatten all topics for this course
        const pageText = document.body.innerText || '';

        return {
            slug,
            url: location.href,
            rootKey: rootKey || null,
            nodeCount: Object.keys(nodes).length,
            nodes,
            pageText: pageText.slice(0, 12000),
        };
    }""", slug)


def extract_units_from_text(text: str) -> list[dict]:
    """Parse unit/lesson titles from rendered page text as a fallback."""
    import re
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    units = []
    current_unit = None
    for line in lines:
        if re.match(r'^(Unit\s+\d+|Course challenge)', line, re.I):
            current_unit = {"unit": line, "lessons": []}
            units.append(current_unit)
        elif current_unit and len(line) > 4 and not re.match(r'^\d+$', line):
            current_unit["lessons"].append(line)
    return units


def main() -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    catalog = {}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise SystemExit("No browser contexts over CDP")
        ctx = browser.contexts[0]

        # Find an existing Khan page to navigate from
        page = None
        for pg in ctx.pages:
            if "khanacademy.org" in pg.url and "accounts" not in pg.url:
                page = pg
                break
        if not page:
            raise SystemExit("No authenticated Khan page found via CDP")

        for slug, display_name in COURSES.items():
            print(f"\nCataloguing: {display_name} ({slug})")
            try:
                result = extract_course_tree(page, slug)
                units_from_text = extract_units_from_text(result.get("pageText", ""))
                catalog[slug] = {
                    "display_name": display_name,
                    "slug": slug,
                    "url": result.get("url"),
                    "node_count": result.get("nodeCount", 0),
                    "nodes": result.get("nodes", {}),
                    "units_from_text": units_from_text,
                    "root_key": result.get("rootKey"),
                }
                print(f"     nodes: {result.get('nodeCount', 0)}, text units: {len(units_from_text)}")
            except Exception as exc:
                print(f"     ERROR: {exc}")
                catalog[slug] = {
                    "display_name": display_name,
                    "slug": slug,
                    "error": str(exc),
                }

    CATALOG_PATH.write_text(json.dumps(catalog, indent=2))
    print(f"\nCatalog written to {CATALOG_PATH}")


if __name__ == "__main__":
    main()
