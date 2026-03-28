from __future__ import annotations

import json
import re
from typing import Any

from playwright.sync_api import sync_playwright

DEFAULT_CDP_URL = "http://127.0.0.1:9333"
DEFAULT_TARGET = "https://www.khanacademy.org/profile/gustywarrior/progress"
DEFAULT_USERNAME = "gustywarrior"
DEFAULT_KAID = "kaid_579616963303309841356397"


def _parse_activity_from_text(text: str) -> list[dict[str, Any]]:
    marker = "ACTIVITY\tDATE\tLEVEL\tCHANGE\tCORRECT/TOTAL PROBLEMS\tTIME (MIN)"
    if marker not in text:
        return []

    section = text.split(marker, 1)[1]
    section = section.split("Previous", 1)[0]
    lines = [line.strip() for line in section.splitlines()]
    lines = [line for line in lines if line]

    def looks_like_date(value: str) -> bool:
        return bool(re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", value))

    def looks_like_metric(value: str) -> bool:
        return value == "–" or value == "Details" or bool(re.fullmatch(r"\+\s*\d+", value)) or bool(re.fullmatch(r"\d+/\d+", value)) or value.isdigit() or value in {"Proficient", "Familiar", "Attempted", "Mastered"}

    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        title = lines[i]
        if title in {"Previous", "|", "Next"} or title.startswith("Our mission is to provide"):
            break
        if i + 2 >= len(lines):
            break

        course = lines[i + 1]
        date_text = lines[i + 2]
        if not looks_like_date(date_text):
            i += 1
            continue
        i += 3

        note = None
        if i < len(lines) and not looks_like_metric(lines[i]):
            note = lines[i]
            i += 1

        metrics = []
        while i < len(lines) and len(metrics) < 4:
            value = lines[i]
            if not looks_like_metric(value):
                break
            metrics.append(value)
            i += 1
        while len(metrics) < 4:
            metrics.append(None)

        level, change, correct_total, time_min = metrics
        if level == "Details" and change and re.fullmatch(r"\+\s*\d+", change or ""):
            level = None

        if correct_total and correct_total.isdigit() and time_min is None:
            time_min = correct_total
            correct_total = None
        if change and change.isdigit() and correct_total is None and time_min is None:
            time_min = change
            change = None

        rows.append(
            {
                "title": title,
                "course": course,
                "date": date_text,
                "note": note,
                "level": None if level in {None, "–", "Details"} else level,
                "change": None if change in {None, "–"} else change,
                "correct_total": None if correct_total in {None, "–"} else correct_total,
                "time_min": int(time_min) if isinstance(time_min, str) and time_min.isdigit() else None,
            }
        )

    return rows


def fetch_progress_via_cdp(
    cdp_url: str = DEFAULT_CDP_URL,
    target_url: str = DEFAULT_TARGET,
    username: str = DEFAULT_USERNAME,
    kaid: str = DEFAULT_KAID,
) -> dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No browser contexts available over CDP")

        page = None
        ctx = None
        for context in browser.contexts:
            for candidate in context.pages:
                if candidate.url.startswith(target_url):
                    page = candidate
                    ctx = context
                    break
            if page:
                break

        if page is None:
            for context in browser.contexts:
                for candidate in context.pages:
                    if 'khanacademy.org' in candidate.url and 'accounts.google.com' not in candidate.url:
                        page = candidate
                        ctx = context
                        break
                if page:
                    break

        if page is None or ctx is None:
            raise RuntimeError(f"Target page not found over CDP: {target_url}")

        page.bring_to_front()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        raw = page.evaluate(
            """([username, kaid]) => {
                const data = (window.__APOLLO_CLIENT__ && window.__APOLLO_CLIENT__.cache && window.__APOLLO_CLIENT__.cache.data && window.__APOLLO_CLIENT__.cache.data.data) || {};
                const root = data['ROOT_QUERY'] || {};
                const learnerRef = root[`user({\"username\":\"${username}\"})`] || root[`user({\"kaid\":\"${kaid}\"})`];
                const learnerId = learnerRef && learnerRef.id;
                const learner = learnerId ? data[learnerId] || {} : {};

                const activityLogKey = Object.keys(data).find(k => k.startsWith('$User:') && k.includes('.activityLogV2('));
                const activityLog = activityLogKey ? (data[activityLogKey] || {}) : {};
                const timeRef = activityLog.time && activityLog.time.id;
                const timeData = timeRef ? (data[timeRef] || {}) : {};
                const sessionsPageRef = activityLog['activitySessions({\"after\":null,\"pageSize\":10})'];
                const sessionsPage = sessionsPageRef && sessionsPageRef.id ? (data[sessionsPageRef.id] || {}) : {};
                const sessions = (sessionsPage.sessions || []).map(ref => {
                    const item = data[ref.id] || {};
                    const levels = (item.skillLevels || []).map(lref => data[lref.id] || {});
                    const levelDetails = levels.map(level => {
                        const exercise = level.exercise && level.exercise.id ? data[level.exercise.id] || {} : {};
                        return {
                            before: level.before || null,
                            after: level.after || null,
                            exerciseTitle: exercise.translatedTitle || null,
                            exerciseId: exercise.id || null,
                        };
                    });
                    return {
                        id: item.id || null,
                        title: item.title || null,
                        subtitle: item.subtitle || null,
                        kind: item.activityKind && item.activityKind.id ? ((data[item.activityKind.id] || {}).id || item.activityKind.id) : null,
                        durationMinutes: item.durationMinutes ?? null,
                        eventTimestamp: item.eventTimestamp || null,
                        skillType: item.skillType || null,
                        correctCount: item.correctCount ?? null,
                        problemCount: item.problemCount ?? null,
                        restarted: item.task && item.task.id ? ((data[item.task.id] || {}).isRestarted ?? null) : null,
                        skillLevelChanges: levelDetails,
                    };
                });

                const text = document.body.innerText || '';
                return {
                    ok: true,
                    url: location.href,
                    title: document.title,
                    learner: {
                        id: learner.id || null,
                        kaid: learner.kaid || null,
                        username: learner.username || null,
                        nickname: learner.nickname || null,
                        points: learner.points || null,
                        countVideosCompleted: learner.countVideosCompleted || null,
                        badgeCounts: learner.badgeCounts || null,
                    },
                    totals: {
                        exerciseMinutes: timeData.exerciseMinutes ?? null,
                        totalMinutes: timeData.totalMinutes ?? null,
                    },
                    activity: sessions,
                    activityCount: sessions.length,
                    textSample: text.slice(0, 4000),
                };
            }""",
            [username, kaid],
        )

        text_sample = raw.get("textSample") or ""
        if raw.get("totals", {}).get("exerciseMinutes") is None:
            m = re.search(r"\b(\d+)\s+exercise min\b", text_sample, flags=re.I | re.S)
            if m:
                raw["totals"]["exerciseMinutes"] = int(m.group(1))
        if raw.get("totals", {}).get("totalMinutes") is None:
            m = re.search(r"\b(\d+)\s+total learning min\b", text_sample, flags=re.I | re.S)
            if m:
                raw["totals"]["totalMinutes"] = int(m.group(1))

        if not raw.get("activity"):
            raw["activity"] = _parse_activity_from_text(text_sample)
            raw["activityCount"] = len(raw["activity"])

        return raw


def main() -> None:
    print(json.dumps(fetch_progress_via_cdp(), indent=2))


if __name__ == "__main__":
    main()
