"""Khan Academy live progress fetcher.

Two back-ends are supported:

1. **Profile mode (default)** — the backend launches its *own* persistent
   Chromium via Playwright's `launch_persistent_context`. Auth cookies live
   inside a dedicated user-data directory (default
   `~/.cache/khan-homeschool/chromium-profile`), so the dashboard keeps
   working after you close your everyday browser. A one-time headed login
   seeds the profile (see `app/scripts/khan_login.py`); after that the
   scraper runs fully headless.

2. **CDP mode (legacy fallback)** — attach to a Chrome you already started
   with `--remote-debugging-port=9333`. Only active while that Chrome is
   open.

Mode is chosen via `KHAN_FETCH_MODE`:

* `profile` (default if the profile dir exists) — use the backend-owned
  persistent Chromium.
* `cdp` — attach to CDP at `KHAN_CDP_URL` (default `http://127.0.0.1:9333`).
* `auto` — try profile first, then CDP.

Both back-ends ultimately drive the same query: load the learner's progress
page, grab the compiled `ActivitySessionsV2Query` GraphQL document from the
live Apollo client, and paginate through every `nextCursor` for a wide
custom date range.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

DEFAULT_CDP_URL = os.getenv("KHAN_CDP_URL", "http://127.0.0.1:9333")
DEFAULT_TARGET = os.getenv("KHAN_TARGET_URL", "https://www.khanacademy.org/profile/gustywarrior/progress")
DEFAULT_USERNAME = os.getenv("KHAN_USERNAME", "gustywarrior")
DEFAULT_KAID = os.getenv("KHAN_KAID", "kaid_579616963303309841356397")

DEFAULT_PROFILE_DIR = Path(
    os.getenv("KHAN_PROFILE_DIR")
    or (Path.home() / ".cache" / "khan-homeschool" / "chromium-profile")
).expanduser()

# How far back to pull activity by default. The US school year starts in
# August/September, so we default to August 1 of the school year that
# contains `today`. This is overridable via env or function args.
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200


def _default_start_date(today: date) -> date:
    """School year starts Aug 1 of the most recent prior Aug 1."""
    if today.month >= 8:
        return date(today.year, 8, 1)
    return date(today.year - 1, 8, 1)


_LEVEL_ORDER = ["unfamiliar", "attempted", "familiar", "proficient", "mastered"]


def _normalize_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a Khan Apollo session object into the dashboard's expected shape."""
    title = raw.get("title") or ""
    course = raw.get("subtitle") or ""
    kind = raw.get("activityKind")
    if isinstance(kind, dict):
        kind = kind.get("id")

    skill_levels = raw.get("skillLevels") or []
    level = None
    change = None
    exercise_id = None
    exercise_title = None
    if skill_levels:
        last = skill_levels[-1] or {}
        before = (last.get("before") or "").lower() or None
        after = (last.get("after") or "").lower() or None
        level = after or level
        exercise_id = last.get("exerciseId")
        exercise_title = last.get("exerciseTitle")
        if before in _LEVEL_ORDER and after in _LEVEL_ORDER:
            diff = _LEVEL_ORDER.index(after) - _LEVEL_ORDER.index(before)
            if diff > 0:
                change = f"+{diff}"

    correct_total = None
    if raw.get("problemCount") is not None and raw.get("correctCount") is not None:
        correct_total = f"{raw['correctCount']}/{raw['problemCount']}"

    return {
        "id": raw.get("id"),
        "title": title,
        "course": course,
        "date": raw.get("eventTimestamp"),
        "kind": kind,
        "skill_type": raw.get("skillType"),
        "level": level,
        "change": change,
        "correct_total": correct_total,
        "time_min": raw.get("durationMinutes"),
        "raw_typename": raw.get("__typename"),
        "exercise_id": exercise_id,
        "exercise_title": exercise_title,
    }


# JavaScript run inside the page: locates the compiled GraphQL document
# and paginates through all sessions for the supplied date range. Resolves
# SkillLevelChange references on MasteryActivitySession entries via the
# Apollo normalized cache so we get exercise IDs + titles without a
# separate query.
_PAGE_JS = r"""
async ({ studentKaid, startDate, endDate, pageSize, maxPages }) => {
    const client = window.__APOLLO_CLIENT__;
    if (!client) return { ok: false, error: "apollo client not on page" };

    let doc = null;
    client.queryManager.queries.forEach((qi) => {
        const d = qi.document;
        const name =
            d &&
            d.definitions &&
            d.definitions[0] &&
            d.definitions[0].name &&
            d.definitions[0].name.value;
        if (name === "ActivitySessionsV2Query") doc = d;
    });
    if (!doc) return { ok: false, error: "ActivitySessionsV2Query document not loaded on this page" };

    let after = null;
    const sessions = [];
    let time = null;
    let pageCount = 0;

    const cacheRoot =
        (client.cache && client.cache.data && client.cache.data.data) || {};
    const resolveRef = (ref) => {
        if (!ref) return null;
        if (ref.id && cacheRoot[ref.id]) return cacheRoot[ref.id];
        return ref;
    };

    for (let i = 0; i < maxPages; i++) {
        const res = await client.query({
            query: doc,
            variables: {
                studentKaid,
                startDate,
                endDate,
                courseType: null,
                activityKind: null,
                after,
                pageSize,
            },
            fetchPolicy: "network-only",
        });
        pageCount++;
        const user = (res.data || {}).user || {};
        const log = user.activityLogV2 || {};
        if (time === null && log.time) {
            time = {
                exerciseMinutes: log.time.exerciseMinutes ?? null,
                totalMinutes: log.time.totalMinutes ?? null,
            };
        }
        const sp = log.activitySessions || {};
        for (const s of sp.sessions || []) {
            // Pull SkillLevelChange rows out of the cache (the query returns
            // them as references; client.query resolves them, but also fall
            // back to a manual lookup if something looks un-resolved).
            let skillLevels = [];
            const raw = s && s.skillLevels;
            if (Array.isArray(raw)) {
                for (const r of raw) {
                    const node = resolveRef(r) || r || {};
                    const ex = node.exercise ? resolveRef(node.exercise) || node.exercise : null;
                    skillLevels.push({
                        before: node.before ?? null,
                        after: node.after ?? null,
                        exerciseId: ex ? ex.id || null : null,
                        exerciseTitle: ex ? ex.translatedTitle || null : null,
                    });
                }
            }
            sessions.push({
                id: s.id,
                title: s.title || null,
                subtitle: s.subtitle || null,
                activityKind: s.activityKind && s.activityKind.id ? s.activityKind.id : null,
                durationMinutes: s.durationMinutes ?? null,
                eventTimestamp: s.eventTimestamp || null,
                skillType: s.skillType || null,
                __typename: s.__typename || null,
                correctCount: s.correctCount ?? null,
                problemCount: s.problemCount ?? null,
                skillLevels,
            });
        }
        const next = sp.pageInfo && sp.pageInfo.nextCursor;
        if (!next) break;
        after = next;
    }

    const learnerId = `User:${studentKaid}`;
    const learner = cacheRoot[learnerId] || {};

    return {
        ok: true,
        url: location.href,
        title: document.title,
        pageCount,
        sessionCount: sessions.length,
        totals: time || { exerciseMinutes: null, totalMinutes: null },
        learner: {
            id: learner.id || null,
            kaid: learner.kaid || null,
            username: learner.username || null,
            nickname: learner.nickname || null,
            points: learner.points || null,
            countVideosCompleted: learner.countVideosCompleted || null,
            badgeCounts: learner.badgeCounts || null,
        },
        sessions,
        dateRange: { startDate, endDate },
    };
}
"""


def _pick_khan_page(ctx: BrowserContext, target_url: str):
    """Return a page in `ctx` pointed at Khan Academy, opening one if needed."""
    for candidate in ctx.pages:
        if candidate.url.startswith(target_url):
            return candidate
    for candidate in ctx.pages:
        if "khanacademy.org" in candidate.url and "accounts.google.com" not in candidate.url:
            return candidate
    return None


def _run_apollo_query(
    ctx: BrowserContext,
    target_url: str,
    kaid: str,
    start_str: str,
    end_str: str,
    page_size: int,
    max_pages: int,
) -> dict[str, Any]:
    """Drive an already-opened browser context to issue the Apollo query."""
    page = _pick_khan_page(ctx, target_url)
    if page is None:
        page = ctx.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

    try:
        page.bring_to_front()
    except Exception:
        pass

    if not page.url.startswith(target_url):
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
    else:
        page.wait_for_timeout(500)

    if "accounts.google.com" in page.url or "/login" in page.url:
        raise RuntimeError(
            "Khan Academy is not logged in for this browser profile. "
            "Run `python -m app.scripts.khan_login` (with venv active) to sign in once."
        )

    for _ in range(10):
        has_doc = page.evaluate(
            """() => {
                const c = window.__APOLLO_CLIENT__;
                if (!c) return false;
                let found = false;
                c.queryManager.queries.forEach((qi) => {
                    const d = qi.document;
                    const name = d && d.definitions && d.definitions[0] && d.definitions[0].name && d.definitions[0].name.value;
                    if (name === 'ActivitySessionsV2Query') found = true;
                });
                return found;
            }"""
        )
        if has_doc:
            break
        page.wait_for_timeout(1000)

    raw = page.evaluate(
        _PAGE_JS,
        {
            "studentKaid": kaid,
            "startDate": start_str,
            "endDate": end_str,
            "pageSize": page_size,
            "maxPages": max_pages,
        },
    )
    if not raw.get("ok"):
        raise RuntimeError(f"Khan Apollo query failed: {raw.get('error')}")
    return raw


def _format_result(raw: dict[str, Any], source: str, start_str: str, end_str: str) -> dict[str, Any]:
    sessions = raw.get("sessions") or []
    normalized = [_normalize_session(s) for s in sessions]

    by_course: dict[str, int] = {}
    minutes_by_course: dict[str, int] = {}
    for item in normalized:
        c = item.get("course") or "(unknown)"
        by_course[c] = by_course.get(c, 0) + 1
        minutes_by_course[c] = minutes_by_course.get(c, 0) + (item.get("time_min") or 0)

    return {
        "ok": True,
        "source": source,
        "url": raw.get("url"),
        "title": raw.get("title"),
        "learner": raw.get("learner") or {},
        "totals": raw.get("totals") or {},
        "date_range": raw.get("dateRange") or {"startDate": start_str, "endDate": end_str},
        "page_count": raw.get("pageCount"),
        "activity": normalized,
        "activity_count": len(normalized),
        "activityCount": len(normalized),  # back-compat
        "by_course": by_course,
        "minutes_by_course": minutes_by_course,
    }


def _resolve_date_range(
    start_date: date | str | None,
    end_date: date | str | None,
) -> tuple[str, str]:
    today = date.today()
    if start_date is None:
        start_date = os.getenv("KHAN_START_DATE") or _default_start_date(today)
    if end_date is None:
        end_date = os.getenv("KHAN_END_DATE") or today
    start_str = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
    end_str = end_date.isoformat() if isinstance(end_date, date) else str(end_date)
    return start_str, end_str


def open_khan_context(
    p: Playwright,
    *,
    mode: str | None = None,
    headless: bool = True,
    profile_dir: Path | str = DEFAULT_PROFILE_DIR,
    cdp_url: str = DEFAULT_CDP_URL,
):
    """Open a Playwright context using the configured back-end.

    Returns ``(context, cleanup_callable)``. Caller must invoke the cleanup
    callable exactly once when done (it closes either the persistent context
    or the CDP-attached browser).
    """
    if mode is None:
        mode = (os.getenv("KHAN_FETCH_MODE") or "").strip().lower()
    if not mode:
        mode = "auto" if Path(profile_dir).expanduser().exists() else "cdp"

    if mode in ("profile", "auto"):
        try:
            ctx = _launch_persistent_context(p, Path(profile_dir).expanduser(), headless=headless)
            return ctx, ctx.close
        except Exception:
            if mode == "profile":
                raise

    browser = p.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        browser.close()
        raise RuntimeError(
            f"No browser contexts available over CDP at {cdp_url}. "
            "Either start Chrome with --remote-debugging-port=9333 or run "
            "`python -m app.scripts.khan_login` to seed the backend profile."
        )
    return browser.contexts[0], browser.close


def _launch_persistent_context(
    p: Playwright,
    profile_dir: Path,
    headless: bool,
) -> BrowserContext:
    """Launch the backend-owned Chromium with persistent auth cookies.

    We prefer the system-installed Chrome (`channel="chrome"`) because Google's
    sign-in occasionally rejects vanilla Playwright Chromium. Falls back to the
    bundled Chromium if Chrome isn't available.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
        ],
    }
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
    except Exception:
        return p.chromium.launch_persistent_context(**launch_kwargs)


def fetch_progress_via_profile(
    target_url: str = DEFAULT_TARGET,
    kaid: str = DEFAULT_KAID,
    profile_dir: Path | str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Fetch via the backend-owned persistent Chromium profile."""
    start_str, end_str = _resolve_date_range(start_date, end_date)
    profile_dir = Path(profile_dir).expanduser()

    with sync_playwright() as p:
        ctx = _launch_persistent_context(p, profile_dir, headless=headless)
        try:
            raw = _run_apollo_query(ctx, target_url, kaid, start_str, end_str, page_size, max_pages)
        finally:
            ctx.close()

    return _format_result(raw, "khan-profile-apollo", start_str, end_str)


def fetch_progress_via_cdp(
    cdp_url: str = DEFAULT_CDP_URL,
    target_url: str = DEFAULT_TARGET,
    username: str = DEFAULT_USERNAME,
    kaid: str = DEFAULT_KAID,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Fetch via an existing Chrome instance started with --remote-debugging-port."""
    start_str, end_str = _resolve_date_range(start_date, end_date)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError(
                "No browser contexts available over CDP "
                f"(is Chrome running with --remote-debugging-port matching {cdp_url}?)"
            )

        ctx = None
        for context in browser.contexts:
            for candidate in context.pages:
                if candidate.url.startswith(target_url) or (
                    "khanacademy.org" in candidate.url and "accounts.google.com" not in candidate.url
                ):
                    ctx = context
                    break
            if ctx:
                break
        if ctx is None:
            ctx = browser.contexts[0]
        raw = _run_apollo_query(ctx, target_url, kaid, start_str, end_str, page_size, max_pages)

    return _format_result(raw, "khan-cdp-apollo", start_str, end_str)


def fetch_progress(
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Dispatch between the profile and CDP back-ends based on env config.

    `KHAN_FETCH_MODE`:
      * ``profile`` — always use the backend-owned persistent profile.
      * ``cdp``     — always attach to a user-started Chrome over CDP.
      * ``auto``    — try profile first; fall back to CDP if the profile
                      hasn't been seeded yet or the profile launch fails.

    Default: ``auto`` if the profile directory exists, else ``cdp``.
    """
    mode = (os.getenv("KHAN_FETCH_MODE") or "").strip().lower()
    if not mode:
        mode = "auto" if DEFAULT_PROFILE_DIR.exists() else "cdp"

    errors: list[str] = []
    if mode in ("profile", "auto"):
        try:
            return fetch_progress_via_profile(
                start_date=start_date, end_date=end_date,
                page_size=page_size, max_pages=max_pages,
            )
        except Exception as exc:
            if mode == "profile":
                raise
            errors.append(f"profile: {exc}")

    if mode in ("cdp", "auto"):
        try:
            return fetch_progress_via_cdp(
                start_date=start_date, end_date=end_date,
                page_size=page_size, max_pages=max_pages,
            )
        except Exception as exc:
            errors.append(f"cdp: {exc}")
            raise RuntimeError(" | ".join(errors)) from exc

    raise RuntimeError(f"Unknown KHAN_FETCH_MODE={mode!r}")


def main() -> None:
    print(json.dumps(fetch_progress(), indent=2, default=str))


if __name__ == "__main__":
    main()
