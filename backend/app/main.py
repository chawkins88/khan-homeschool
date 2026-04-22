from datetime import date, timedelta
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from app.services.khan_cdp_progress import DEFAULT_PROFILE_DIR, fetch_progress

load_dotenv()

app = FastAPI(title='Khan Homeschool Dashboard')

BASE_DIR = Path(__file__).resolve().parents[2]
TARGET_DATE_PATH = BASE_DIR / 'research' / 'khan' / 'target_date.json'
LESSON_MINUTES_PATH = BASE_DIR / 'research' / 'khan' / 'lesson_minutes.json'
DATE_EXCLUSIONS_PATH = BASE_DIR / 'research' / 'khan' / 'date_exclusions.json'
DEFAULT_TARGET_DATE = date(2026, 5, 8)
DEFAULT_LESSON_MINUTES = 20


def _today() -> date:
    """Current date, with an optional env override (KHAN_DASHBOARD_TODAY=YYYY-MM-DD)."""
    override = os.getenv('KHAN_DASHBOARD_TODAY')
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError:
            pass
    return date.today()


def load_target_date() -> date:
    """Read the persisted target date; fall back to env var or the default."""
    try:
        raw = json.loads(TARGET_DATE_PATH.read_text())
        val = raw.get('target_date')
        if val:
            return date.fromisoformat(val)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        pass
    env = os.getenv('KHAN_TARGET_DATE')
    if env:
        try:
            return date.fromisoformat(env)
        except ValueError:
            pass
    return DEFAULT_TARGET_DATE


def save_target_date(target: date) -> None:
    TARGET_DATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TARGET_DATE_PATH.write_text(json.dumps({'target_date': target.isoformat()}, indent=2))


def load_lesson_minutes() -> int:
    """Read the persisted average minutes-per-lesson (UI-editable)."""
    try:
        raw = json.loads(LESSON_MINUTES_PATH.read_text())
        val = int(raw.get('minutes_per_lesson') or 0)
        if val > 0:
            return val
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        pass
    env = os.getenv('KHAN_LESSON_MINUTES')
    if env:
        try:
            val = int(env)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_LESSON_MINUTES


def save_lesson_minutes(minutes: int) -> None:
    LESSON_MINUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    LESSON_MINUTES_PATH.write_text(json.dumps({'minutes_per_lesson': int(minutes)}, indent=2))


def next_school_days(count: int = 5, start: date | None = None) -> list[date]:
    """Return the next `count` Mon–Fri dates, starting today (if weekday) or later.

    Dates inside any persisted exclusion range are skipped. We cap the search
    to a sensible horizon so a pathological exclusion list can't loop forever.
    """
    if start is None:
        start = _today()
    ranges = load_date_exclusions()
    out: list[date] = []
    d = start
    # Horizon: up to 180 days of calendar slop, which is plenty for 5 school days.
    max_d = start + timedelta(days=180)
    while len(out) < count and d <= max_d:
        if d.weekday() < 5 and not _is_date_excluded(d, ranges):
            out.append(d)
        d += timedelta(days=1)
    return out


def load_date_exclusions() -> list[dict]:
    """Read the list of persisted excluded date ranges (vacations, holidays).

    Each entry is ``{id, start: YYYY-MM-DD, end: YYYY-MM-DD, label}``.
    Entries with invalid/missing data are filtered out.
    """
    try:
        raw = json.loads(DATE_EXCLUSIONS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get('start')
        end = item.get('end')
        if not start or not end:
            continue
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            continue
        out.append({
            'id': item.get('id') or f"ex_{start}_{end}",
            'start': start,
            'end': end,
            'label': item.get('label') or '',
        })
    return out


def save_date_exclusions(ranges: list[dict]) -> None:
    DATE_EXCLUSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATE_EXCLUSIONS_PATH.write_text(json.dumps(ranges, indent=2))


def _is_date_excluded(d: date, ranges: list[dict] | None = None) -> bool:
    if ranges is None:
        ranges = load_date_exclusions()
    for r in ranges:
        try:
            if date.fromisoformat(r['start']) <= d <= date.fromisoformat(r['end']):
                return True
        except (KeyError, ValueError, TypeError):
            continue
    return False


def count_school_days(
    start: date,
    end: date,
    *,
    ranges: list[dict] | None = None,
    include_excluded: bool = False,
) -> int:
    """Count Mon-Fri dates in ``[start, end]`` inclusive. By default skip
    user-excluded ranges; pass ``include_excluded=True`` for the raw count."""
    if end < start:
        return 0
    if ranges is None and not include_excluded:
        ranges = load_date_exclusions()
    d = start
    n = 0
    while d <= end:
        if d.weekday() < 5 and (include_excluded or not _is_date_excluded(d, ranges or [])):
            n += 1
        d += timedelta(days=1)
    return n


def _days_left() -> int:
    return (load_target_date() - _today()).days


def available_school_days_left() -> int:
    """School days between today and the target (inclusive), minus excluded ranges."""
    today = _today()
    target = load_target_date()
    if target < today:
        return 0
    return count_school_days(today, target)


def _weeks_left() -> float:
    """Remaining 5-day school weeks after removing user-excluded ranges."""
    days = available_school_days_left()
    return max(1.0, days / 5)

# Configure via environment variables — see .env.example
RYAN_CALENDAR_ID = os.getenv('RYAN_CALENDAR_ID', '')
GOG_ACCOUNT = os.getenv('GOG_ACCOUNT', '')

COURSE_CATALOG_PATH = BASE_DIR / 'research' / 'khan' / 'course_catalog.json'
LESSON_EXCLUSIONS_PATH = BASE_DIR / 'research' / 'khan' / 'lesson_exclusions.json'
COURSE_BLUEPRINTS = [
    {
        'slug': 'cc-sixth-grade-math',
        'name': '6th Grade Math',
        'khan_subtitle': '6th grade math',
        'status': 'well underway',
    },
    {
        'slug': 'ms-physics',
        'name': 'Middle School Physics',
        'khan_subtitle': 'Middle school physics',
        'status': 'underway',
    },
    {
        'slug': 'oer-project-big-history',
        'name': 'OER Project: Big History',
        'khan_subtitle': 'OER Project: Big History',
        'status': 'underway',
    },
    {
        'slug': 'new-6th-grade-reading-and-vocabulary',
        'name': '6th Grade Reading & Vocab',
        'khan_subtitle': '6th grade reading and vocab',
        'status': 'underway',
    },
]


def normalize_title(value: str) -> str:
    value = (value or '').lower().strip()
    value = value.replace('’', "'")
    value = re.sub(r'up next for you!?', '', value, flags=re.I)
    value = re.sub(r'\b(unit mastery|mastery unavailable)\b.*$', '', value, flags=re.I)
    value = re.sub(r'\b(unfamiliar|attempted|familiar|proficient|mastered)\b', '', value, flags=re.I)
    value = re.sub(r'\b(details|start course challenge|course challenge)\b', '', value, flags=re.I)
    value = re.sub(r'\s+', ' ', value)
    value = re.sub(r'\s*[:\-–—]+\s*$', '', value)
    value = re.sub(r'^[^a-z0-9]+|[^a-z0-9]+$', '', value)
    return value.strip()


def title_token_set(value: str) -> set[str]:
    norm = normalize_title(value)
    return {token for token in re.split(r'[^a-z0-9]+', norm) if token and token not in {'the', 'a', 'an', 'and', 'or', 'for', 'to', 'of', 'in'}}


def title_match_score(activity_title: str, catalog_title: str) -> int:
    a = normalize_title(activity_title)
    b = normalize_title(catalog_title)
    if not a or not b:
        return 0
    if a == b:
        return 100

    a_tokens = title_token_set(a)
    b_tokens = title_token_set(b)
    if not a_tokens or not b_tokens:
        return 0

    overlap_tokens = a_tokens & b_tokens
    overlap = len(overlap_tokens)
    if overlap == 0:
        return 0

    score = overlap * 10
    if a in b or b in a:
        score += 15
    if overlap == min(len(a_tokens), len(b_tokens)):
        score += 25
    score -= abs(len(a_tokens) - len(b_tokens)) * 2
    return max(score, 0)


def load_course_catalog() -> dict:
    try:
        return json.loads(COURSE_CATALOG_PATH.read_text())
    except Exception:
        return {}


MASTERY_ORDER = ['unfamiliar', 'attempted', 'familiar', 'proficient', 'mastered']
MASTERY_WEIGHT = {'unfamiliar': 0.0, 'attempted': 0.25, 'familiar': 0.5, 'proficient': 0.85, 'mastered': 1.0}
MEDIA_KINDS = {'Video', 'Article'}
EXERCISE_KINDS = {'Exercise', 'TopicQuiz', 'TopicUnitTest'}
FUZZY_MIN_SCORE = 50


def _best_mastery(existing: str | None, new: str | None) -> str | None:
    if not new:
        return existing
    if not existing:
        return new
    if MASTERY_ORDER.index(new) > MASTERY_ORDER.index(existing):
        return new
    return existing


def load_lesson_exclusions() -> dict[str, dict[str, bool]]:
    """Return `{course_slug: {lesson_id_or_slug: True}}`.

    Exclusions are keyed by the catalog lesson id (preferred) and lesson slug as
    a backup — either value in the incoming toggle POST will match a lesson.
    """
    try:
        data = json.loads(LESSON_EXCLUSIONS_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, bool]] = {}
    for slug, entries in data.items():
        if not isinstance(entries, dict):
            continue
        out[slug] = {str(k): bool(v) for k, v in entries.items() if v}
    return out


def save_lesson_exclusions(exclusions: dict[str, dict[str, bool]]) -> None:
    LESSON_EXCLUSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cleaned: dict[str, dict[str, bool]] = {}
    for slug, entries in exclusions.items():
        kept = {k: True for k, v in (entries or {}).items() if v}
        if kept:
            cleaned[slug] = kept
    LESSON_EXCLUSIONS_PATH.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + '\n')


def _lesson_is_excluded(lesson: dict, excluded_map: dict[str, bool]) -> bool:
    if not excluded_map:
        return False
    for key in (lesson.get('id'), lesson.get('slug')):
        if key and excluded_map.get(str(key)):
            return True
    return False


def _activity_attempt_signals(activity: list[dict]) -> tuple[set[str], dict[str, set[str]]]:
    """Build fast lookup structures from the activity feed.

    Returns
    -------
    exercise_ids : set of exercise IDs that were touched (any level above nothing).
    titles_by_course : {course_subtitle_lower: set of normalized activity titles}
    """
    exercise_ids: set[str] = set()
    titles_by_course: dict[str, set[str]] = {}
    for item in activity:
        ex_id = item.get('exercise_id')
        if ex_id:
            exercise_ids.add(ex_id)
        course_key = (item.get('course') or '').strip().lower()
        norm = normalize_title(item.get('title') or '')
        if course_key and norm:
            titles_by_course.setdefault(course_key, set()).add(norm)
    return exercise_ids, titles_by_course


def _lesson_attempted(
    lesson: dict,
    touched_exercise_ids: set[str],
    course_titles: set[str],
) -> bool:
    """A lesson is attempted if any item in it has been touched by Ryan."""
    for item in lesson.get('items', []) or []:
        item_id = item.get('id')
        if item_id and item_id in touched_exercise_ids:
            return True
        norm = normalize_title(item.get('title') or '')
        if norm and norm in course_titles:
            return True
    if course_titles:
        # Fuzzy fallback: lesson title itself matches an activity title (covers
        # cases where the activity row is the lesson header rather than an item).
        lesson_norm = normalize_title(lesson.get('title') or '')
        if lesson_norm and lesson_norm in course_titles:
            return True
    return False


def _derive_next_focus(units_out: list[dict], remaining: int, counted_total: int) -> str:
    """Return a short, data-driven description of what the student should work on
    next. Walks units/lessons in catalog order and picks the first non-excluded,
    not-yet-attempted lesson. Falls back to summary text when everything is done
    or nothing counts."""
    if not counted_total:
        return 'No counted lessons in this course yet'
    if remaining <= 0:
        return 'All counted lessons attempted — keep polishing mastery'

    for unit_index, unit in enumerate(units_out, start=1):
        lessons = unit.get('lessons') or []
        any_counted = any(not l.get('excluded') for l in lessons)
        if not any_counted:
            continue
        any_attempted = any(l.get('attempted') and not l.get('excluded') for l in lessons)
        for lesson in lessons:
            if lesson.get('excluded') or lesson.get('attempted'):
                continue
            verb = 'Continue' if any_attempted else 'Start'
            unit_title = (unit.get('title') or '').strip() or f'Unit {unit_index}'
            lesson_title = (lesson.get('title') or '').strip() or 'next lesson'
            return f'{verb} Unit {unit_index} ({unit_title}) — {lesson_title}'

    return 'All counted lessons attempted — keep polishing mastery'


def build_courses_from_live_data(live_data: dict) -> list[dict]:
    """Compute per-course progress by joining the structured ContentForPath catalog
    with Ryan's recent activity sessions.

    Matching strategy, in priority order:
      1. Exercise ID match (MasteryActivitySession -> Exercise in catalog).
      2. Exact title match within the same course (case-insensitive).
      3. Fuzzy token-overlap match within the same course.
    """
    catalog = load_course_catalog()
    exclusions = load_lesson_exclusions()
    activity = live_data.get('activity', []) if isinstance(live_data, dict) else []
    touched_exercise_ids, titles_by_course = _activity_attempt_signals(activity)

    courses = []
    for blueprint in COURSE_BLUEPRINTS:
        catalog_entry = catalog.get(blueprint['slug'], {})
        course_excl = exclusions.get(blueprint['slug'], {})

        course_titles: set[str] = set()
        for key in {blueprint['name'].lower(), (blueprint.get('khan_subtitle') or '').lower()}:
            if key:
                course_titles |= titles_by_course.get(key, set())

        units_out = []
        total_lessons = 0
        counted_total = 0
        attempted_counted = 0
        attempted_excluded = 0

        for unit in catalog_entry.get('units', []) or []:
            unit_lessons = []
            for lesson in unit.get('lessons', []) or []:
                # Skip standalone TopicQuiz / TopicUnitTest entries — they aren't
                # "Learn + Practice" lessons in the user's definition.
                if lesson.get('standalone_kind'):
                    continue

                lesson_id = lesson.get('id')
                lesson_slug = lesson.get('slug')
                lesson_title = lesson.get('title') or '(lesson)'
                attempted = _lesson_attempted(lesson, touched_exercise_ids, course_titles)
                excluded = _lesson_is_excluded(lesson, course_excl)

                unit_lessons.append({
                    'id': lesson_id,
                    'slug': lesson_slug,
                    'title': lesson_title,
                    'item_count': len(lesson.get('items') or []),
                    'attempted': attempted,
                    'excluded': excluded,
                    'url': lesson.get('url'),
                })

                total_lessons += 1
                if excluded:
                    if attempted:
                        attempted_excluded += 1
                else:
                    counted_total += 1
                    if attempted:
                        attempted_counted += 1

            if unit_lessons:
                units_out.append({
                    'id': unit.get('id'),
                    'slug': unit.get('slug'),
                    'title': unit.get('title'),
                    'url': unit.get('url'),
                    'lessons': unit_lessons,
                })

        progress_percent = round((attempted_counted / counted_total) * 100) if counted_total else 0
        remaining = max(0, counted_total - attempted_counted)

        next_focus = _derive_next_focus(units_out, remaining, counted_total)

        recent_titles = [t for t in ((a.get('title') or '').strip() for a in activity
                                     if (a.get('course') or '').strip().lower()
                                     in {blueprint['name'].lower(), (blueprint.get('khan_subtitle') or '').lower()})
                         if t]
        seen = set()
        deduped_recent = []
        for t in recent_titles:
            if t not in seen:
                seen.add(t)
                deduped_recent.append(t)
            if len(deduped_recent) >= 5:
                break

        courses.append({
            'slug': blueprint['slug'],
            'name': blueprint['name'],
            'status': blueprint['status'],
            'recent': deduped_recent,
            'estimated_total_items': counted_total,
            'estimated_done_items': attempted_counted,
            'remaining_items': remaining,
            'progress_percent': progress_percent,
            'per_week_needed': round(remaining / _weeks_left(), 1) if counted_total else 0,
            'next_focus': next_focus,
            'units': units_out,
            'breakdown': {
                'lessons_total_in_catalog': total_lessons,
                'lessons_counted': counted_total,
                'lessons_attempted': attempted_counted,
                'lessons_excluded': total_lessons - counted_total,
                'lessons_attempted_but_excluded': attempted_excluded,
            },
        })

    return courses


def load_calendar_events(start_date: date, end_date: date):
    if not RYAN_CALENDAR_ID or not GOG_ACCOUNT:
        return []
    try:
        cmd = [
            'gog', 'calendar', 'events', RYAN_CALENDAR_ID,
            '--from', f'{start_date.isoformat()}T00:00:00-04:00',
            '--to', f'{end_date.isoformat()}T23:59:59-04:00',
            '--json'
        ]
        env = dict(os.environ)
        env['GOG_ACCOUNT'] = GOG_ACCOUNT
        out = subprocess.check_output(cmd, text=True, env=env)
        data = json.loads(out)
        return data.get('events', [])
    except Exception:
        return []


def build_schedule(start_date: date | None = None, days: int = 14):
    if start_date is None:
        start_date = _today()
    end_date = start_date + timedelta(days=days - 1)
    events = load_calendar_events(start_date, end_date)
    busy_by_day = {}
    for ev in events:
        start = ev.get('start', {})
        dt = start.get('dateTime') or start.get('date') or ''
        if not dt:
            continue
        day = dt[:10]
        busy_by_day.setdefault(day, []).append(ev.get('summary', 'Busy'))

    suggested_days = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        day = d.isoformat()
        busy = busy_by_day.get(day, [])
        score = len(busy)
        if score == 0:
            blocks = [
                {'subject': 'Math', 'minutes': 45, 'time': '09:00'},
                {'subject': 'Physics', 'minutes': 30, 'time': '10:15'},
                {'subject': 'Big History', 'minutes': 30, 'time': '11:00'},
            ]
        elif score == 1:
            blocks = [
                {'subject': 'Math', 'minutes': 40, 'time': '09:00'},
                {'subject': 'Physics', 'minutes': 25, 'time': '10:15'},
                {'subject': 'Big History', 'minutes': 20, 'time': '13:00'},
            ]
        else:
            blocks = [
                {'subject': 'Math', 'minutes': 35, 'time': '09:00'},
                {'subject': 'Physics', 'minutes': 20, 'time': '13:00'},
            ]
        suggested_days.append({
            'date': day,
            'busy_count': score,
            'busy': busy,
            'blocks': blocks,
        })

    return {
        'calendar_events': events,
        'suggested_days': suggested_days,
        'calendar_is_empty': len(events) == 0,
    }


_LIVE_ACTIVITY_CACHE: dict = {'payload': None, 'fetched_at': 0.0}


def get_updated_activity_data(*, force: bool = False, max_age_seconds: int = 300):
    """Return the latest structured Khan activity feed available to the backend.

    The actual Khan fetch is relatively expensive (spins up a short-lived
    Chromium), so we cache the successful payload in-process. Pure exclusion
    toggles don't need a new Khan round-trip — they just re-filter the cached
    activity. Pass ``force=True`` (or expire the TTL) to hit Khan again.
    """
    import time

    cached = _LIVE_ACTIVITY_CACHE.get('payload')
    age = time.time() - _LIVE_ACTIVITY_CACHE.get('fetched_at', 0.0)
    if cached and cached.get('ok') and not force and age < max_age_seconds:
        return {**cached, 'cache_age_seconds': round(age, 1)}

    try:
        payload = fetch_progress()
        payload['source'] = 'khan-cdp-live'
        payload['item_count'] = len(payload.get('activity', []))
        _LIVE_ACTIVITY_CACHE['payload'] = payload
        _LIVE_ACTIVITY_CACHE['fetched_at'] = time.time()
        return payload
    except Exception as exc:
        if cached:
            return {**cached, 'stale': True, 'stale_error': str(exc)}
        return {
            'ok': False,
            'source': 'khan-cdp-live',
            'error': str(exc),
            'activity': [],
            'item_count': 0,
        }


@app.get('/health')
def health():
    return {'ok': True}


# --- Connect to Khan Academy (one-time login) ------------------------------

_connect_proc: subprocess.Popen | None = None


def _profile_is_seeded() -> bool:
    """Heuristic: a Chromium persistent profile with cookies has a Cookies db."""
    profile_dir = Path(DEFAULT_PROFILE_DIR)
    if not profile_dir.exists():
        return False
    for candidate in (profile_dir / 'Default' / 'Cookies', profile_dir / 'Cookies'):
        if candidate.exists():
            return True
    return False


def _connect_running() -> bool:
    global _connect_proc
    if _connect_proc is None:
        return False
    if _connect_proc.poll() is None:
        return True
    _connect_proc = None
    return False


@app.get('/api/khan/connect')
def khan_connect_status():
    return {
        'ok': True,
        'running': _connect_running(),
        'seeded': _profile_is_seeded(),
        'profile_dir': str(DEFAULT_PROFILE_DIR),
    }


@app.post('/api/khan/connect')
def khan_connect_launch():
    """Spawn a headed Chromium window so the user can sign in to Khan Academy.

    The subprocess detaches from our process group (new session) and exits on
    its own when the user closes the Chromium window.
    """
    global _connect_proc
    if _connect_running():
        return {
            'ok': True,
            'already_running': True,
            'pid': _connect_proc.pid if _connect_proc else None,
        }

    cmd = [sys.executable, '-m', 'app.scripts.khan_login']
    log_path = Path('/tmp/khan-connect.log')
    try:
        log_fh = open(log_path, 'a', buffering=1)
        log_fh.write(f"\n=== {date.today().isoformat()} launching {cmd} ===\n")
        _connect_proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent.parent)},
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'failed to launch login window: {exc}')

    return {
        'ok': True,
        'started': True,
        'pid': _connect_proc.pid,
        'profile_dir': str(DEFAULT_PROFILE_DIR),
    }


@app.get('/api/khan/progress/live')
def khan_progress_live():
    try:
        return fetch_progress_via_cdp()
    except Exception as exc:
        return {
            'ok': False,
            'error': str(exc),
        }


@app.get('/api/dashboard')
def dashboard(refresh: bool = False):
    live_progress = get_updated_activity_data(force=refresh)
    courses = build_courses_from_live_data(live_progress)
    total_remaining = sum(c['remaining_items'] for c in courses)
    return {
        'learner': 'Ryan',
        'username': 'gustywarrior',
        'target_date': load_target_date().isoformat(),
        'today': _today().isoformat(),
        'days_left': _days_left(),
        'courses': courses,
        'summary': {
            'total_remaining_items': total_remaining,
            'math_priority': True,
            'recommended_schedule': [
                'Math every weekday',
                'Physics 3x/week',
                'Big History 3-4x/week',
            ],
        },
        'calendar': build_schedule(),
        'live_progress': live_progress,
    }


@app.get('/api/calendar-plan')
def calendar_plan():
    return build_schedule()


class LessonExclusionToggle(BaseModel):
    course_slug: str
    lesson_id: str | None = None
    lesson_slug: str | None = None
    excluded: bool


class TargetDatePayload(BaseModel):
    target_date: str


class LessonMinutesPayload(BaseModel):
    minutes_per_lesson: int


@app.get('/api/lesson-minutes')
def get_lesson_minutes():
    return {'minutes_per_lesson': load_lesson_minutes()}


@app.post('/api/lesson-minutes')
def set_lesson_minutes(payload: LessonMinutesPayload):
    if payload.minutes_per_lesson <= 0 or payload.minutes_per_lesson > 600:
        raise HTTPException(status_code=400, detail='minutes_per_lesson must be between 1 and 600')
    save_lesson_minutes(payload.minutes_per_lesson)
    return {'ok': True, 'minutes_per_lesson': payload.minutes_per_lesson}


class DateExclusionPayload(BaseModel):
    start: str
    end: str
    label: str | None = None


@app.get('/api/date-exclusions')
def get_date_exclusions():
    return {'exclusions': load_date_exclusions(), **_schedule_summary()}


@app.post('/api/date-exclusions')
def add_date_exclusion(payload: DateExclusionPayload):
    try:
        s = date.fromisoformat(payload.start)
        e = date.fromisoformat(payload.end)
    except ValueError:
        raise HTTPException(status_code=400, detail='start/end must be YYYY-MM-DD')
    if e < s:
        raise HTTPException(status_code=400, detail='end must not be before start')
    ranges = load_date_exclusions()
    # Cheap stable-ish ID: start-end plus a collision suffix if needed.
    base_id = f"ex_{s.isoformat()}_{e.isoformat()}"
    new_id = base_id
    i = 1
    existing_ids = {r.get('id') for r in ranges}
    while new_id in existing_ids:
        i += 1
        new_id = f"{base_id}_{i}"
    ranges.append({
        'id': new_id,
        'start': s.isoformat(),
        'end': e.isoformat(),
        'label': (payload.label or '').strip(),
    })
    # Keep sorted by start date for stable display.
    ranges.sort(key=lambda r: (r.get('start') or '', r.get('end') or ''))
    save_date_exclusions(ranges)
    return {'ok': True, 'id': new_id, 'exclusions': ranges, **_schedule_summary()}


@app.delete('/api/date-exclusions/{exclusion_id}')
def delete_date_exclusion(exclusion_id: str):
    ranges = load_date_exclusions()
    filtered = [r for r in ranges if r.get('id') != exclusion_id]
    if len(filtered) == len(ranges):
        raise HTTPException(status_code=404, detail='exclusion not found')
    save_date_exclusions(filtered)
    return {'ok': True, 'exclusions': filtered, **_schedule_summary()}


@app.get('/api/school-days')
def school_days(count: int = 5):
    """Return the next `count` Mon–Fri dates, with calendar busy events for each."""
    if count < 1 or count > 30:
        raise HTTPException(status_code=400, detail='count must be between 1 and 30')
    days = next_school_days(count=count)
    if not days:
        return {'days': []}

    events = load_calendar_events(days[0], days[-1])
    busy_by_day: dict[str, list[str]] = {}
    for ev in events:
        start = ev.get('start', {})
        dt = start.get('dateTime') or start.get('date') or ''
        if not dt:
            continue
        busy_by_day.setdefault(dt[:10], []).append(ev.get('summary', 'Busy'))

    out = []
    WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for d in days:
        iso = d.isoformat()
        out.append({
            'date': iso,
            'weekday': WEEKDAYS[d.weekday()],
            'busy': busy_by_day.get(iso, []),
            'busy_count': len(busy_by_day.get(iso, [])),
        })
    return {'days': out}


def _schedule_summary() -> dict:
    today = _today()
    target = load_target_date()
    ranges = load_date_exclusions()
    raw_school_days = count_school_days(today, target, ranges=ranges, include_excluded=True)
    available = count_school_days(today, target, ranges=ranges)
    return {
        'target_date': target.isoformat(),
        'today': today.isoformat(),
        'days_left': max(0, (target - today).days),
        'school_days_total': raw_school_days,
        'school_days_excluded': max(0, raw_school_days - available),
        'available_school_days': available,
        'available_weeks': round(max(1.0, available / 5), 2),
        'exclusions': ranges,
    }


@app.get('/api/target-date')
def get_target_date():
    return _schedule_summary()


@app.post('/api/target-date')
def set_target_date(payload: TargetDatePayload):
    try:
        new_date = date.fromisoformat(payload.target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail='target_date must be YYYY-MM-DD')
    today = _today()
    if new_date < today:
        raise HTTPException(status_code=400, detail='target_date cannot be in the past')
    save_target_date(new_date)
    return {'ok': True, **_schedule_summary()}


@app.get('/api/exclusions')
def get_exclusions():
    return load_lesson_exclusions()


@app.post('/api/exclusions')
def set_exclusion(payload: LessonExclusionToggle):
    if not payload.course_slug:
        raise HTTPException(status_code=400, detail='course_slug required')
    if not any(x for x in (payload.lesson_id, payload.lesson_slug)):
        raise HTTPException(status_code=400, detail='lesson_id or lesson_slug required')

    # Validate that the lesson exists in the catalog before persisting.
    catalog = load_course_catalog()
    catalog_entry = catalog.get(payload.course_slug)
    if not catalog_entry:
        raise HTTPException(status_code=404, detail=f'unknown course_slug: {payload.course_slug}')

    resolved_id = None
    for unit in catalog_entry.get('units', []) or []:
        for lesson in unit.get('lessons', []) or []:
            if lesson.get('standalone_kind'):
                continue
            if payload.lesson_id and lesson.get('id') == payload.lesson_id:
                resolved_id = lesson.get('id')
                break
            if payload.lesson_slug and lesson.get('slug') == payload.lesson_slug:
                resolved_id = lesson.get('id') or lesson.get('slug')
                break
        if resolved_id:
            break
    if not resolved_id:
        raise HTTPException(status_code=404, detail='lesson not found in catalog')

    exclusions = load_lesson_exclusions()
    course_map = dict(exclusions.get(payload.course_slug, {}))
    if payload.excluded:
        course_map[resolved_id] = True
    else:
        course_map.pop(resolved_id, None)
    if course_map:
        exclusions[payload.course_slug] = course_map
    else:
        exclusions.pop(payload.course_slug, None)
    save_lesson_exclusions(exclusions)
    return {'ok': True, 'course_slug': payload.course_slug, 'lesson_id': resolved_id, 'excluded': payload.excluded}


@app.get('/', response_class=HTMLResponse)
def home():
    live = get_updated_activity_data()
    courses = build_courses_from_live_data(live)
    courses_json = json.dumps(courses).replace('</', '<\\/')

    activity_rows = []
    for item in live.get('activity', [])[:10]:
        activity_rows.append(
            f"<tr><td>{item.get('date','')}</td><td>{item.get('title','')}</td><td>{item.get('course','')}</td><td>{item.get('level','') or ''}</td><td>{item.get('change','') or ''}</td><td>{item.get('correct_total','') or ''}</td><td>{item.get('time_min','') if item.get('time_min') is not None else ''}</td></tr>"
        )

    live_status = f"Loaded {live.get('item_count', 0)} live activity items from backend source {live.get('source', 'unknown')}. Exercise min: {live.get('totals', {}).get('exerciseMinutes', 'n/a')} · Total learning min: {live.get('totals', {}).get('totalMinutes', 'n/a')}" if live.get('ok') else f"Live activity feed unavailable: {live.get('error', 'unknown error')}"
    initial_live_json = json.dumps(live).replace('</', '<\\/')

    return f"""
    <html><head><title>Ryan Khan Dashboard</title>
    <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; background: #111827; color: #f3f4f6; }}
    .top {{ display:grid; grid-template-columns: 1.2fr .8fr; gap:20px; }}
    .card {{ background:#1f2937; border-radius:14px; padding:20px; margin-bottom:18px; box-shadow:0 2px 10px rgba(0,0,0,.25); }}
    .bar {{ height:12px; background:#374151; border-radius:999px; overflow:hidden; margin:12px 0; }}
    .fill {{ height:100%; background:#22c55e; transition: width .3s ease; }}
    .muted {{ color:#cbd5e1; }}
    ul {{ line-height:1.6; }}
    a {{ color:#93c5fd; }}
    table {{ width:100%; border-collapse: collapse; }}
    td, th {{ text-align:left; border-bottom:1px solid #374151; padding:10px 8px; vertical-align:top; }}
    .button, button {{ display:inline-block; background:#2563eb; color:#fff; padding:10px 14px; border-radius:10px; text-decoration:none; font-weight:600; border:0; cursor:pointer; }}
    button[disabled] {{ opacity:.65; cursor:wait; }}
    .toolbar {{ display:flex; gap:12px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }}
    .unit-row {{ display:grid; grid-template-columns: 220px 1fr; gap:14px; align-items:start; padding:8px 0; border-bottom:1px dashed #374151; }}
    .unit-row:last-child {{ border-bottom:0; }}
    .unit-title {{ font-weight:600; color:#e5e7eb; padding-top:4px; }}
    .unit-title .unit-sub {{ display:block; font-size:12px; color:#9ca3af; font-weight:400; margin-top:2px; }}
    .lesson-boxes {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .lbox {{ width:26px; height:26px; border-radius:6px; background:#374151; border:1px solid #4b5563; position:relative; cursor:help; }}
    .lbox.attempted {{ background:#22c55e; border-color:#16a34a; }}
    .lbox.excluded {{ background:transparent !important; border:1px dashed #6b7280; opacity:.55; }}
    .lbox.excluded.attempted::after {{ content:''; position:absolute; inset:6px; background:#22c55e; border-radius:3px; opacity:.55; }}
    details.manage {{ margin-top:12px; background:#111827; border:1px solid #374151; border-radius:10px; padding:10px 14px; }}
    details.manage summary {{ cursor:pointer; color:#93c5fd; user-select:none; }}
    details.manage .unit-manage {{ margin:10px 0 14px; }}
    details.manage .unit-manage h4 {{ margin:0 0 4px; color:#f3f4f6; font-size:14px; }}
    details.manage label.lesson-toggle {{ display:flex; align-items:center; gap:8px; padding:3px 0; color:#d1d5db; font-size:13px; }}
    details.manage label.lesson-toggle input {{ margin:0; }}
    details.manage label.lesson-toggle.excluded {{ color:#9ca3af; text-decoration:line-through; }}
    .legend {{ display:flex; gap:14px; margin:6px 0 10px; font-size:12px; color:#cbd5e1; }}
    .legend .lbox {{ width:16px; height:16px; border-radius:4px; cursor:default; display:inline-block; vertical-align:middle; margin-right:4px; }}
    .page-header {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:18px; flex-wrap:wrap; }}
    .page-header h1 {{ margin:0; }}
    .connect-wrap {{ display:flex; flex-direction:column; align-items:flex-end; gap:4px; }}
    #connect-khan-btn {{ background:#0ea5e9; }}
    #connect-khan-btn.running {{ background:#f59e0b; }}
    #connect-khan-btn.seeded {{ background:#16a34a; }}
    #connect-status {{ font-size:12px; color:#cbd5e1; }}
    .excl-box {{ margin-top:10px; padding:10px 12px; background:#111827; border:1px solid #374151; border-radius:10px; }}
    .excl-box ul li {{ display:flex; align-items:center; justify-content:space-between; gap:8px; padding:4px 0; border-bottom:1px dashed #374151; font-size:13px; }}
    .excl-box ul li:last-child {{ border-bottom:0; }}
    .excl-box ul li .meta {{ color:#cbd5e1; }}
    .excl-box ul li .del {{ background:transparent; color:#f87171; border:1px solid #4b5563; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:600; cursor:pointer; }}
    .excl-box ul li .del:hover {{ background:#7f1d1d; color:#fff; border-color:#7f1d1d; }}
    .excl-box ul li.empty {{ justify-content:flex-start; color:#9ca3af; font-style:italic; border-bottom:0; }}
    .excl-form {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
    .excl-form input[type="date"], .excl-form input[type="text"] {{ background:#1f2937; color:#f3f4f6; border:1px solid #374151; border-radius:6px; padding:4px 8px; font-size:13px; }}
    .excl-form input[type="text"] {{ flex:1; min-width:140px; }}
    .excl-form button {{ padding:5px 10px; font-size:13px; }}
    </style></head><body>
    <div class='page-header'>
      <h1>Ryan Khan Dashboard</h1>
      <div class='connect-wrap'>
        <button id='connect-khan-btn' type='button' title='Open a Chromium window to sign in to Khan Academy. The session persists for the backend.'>Connect to Khan Academy</button>
        <span id='connect-status'>Checking session…</span>
      </div>
    </div>
    <div class='top'>
      <div class='card'>
        <h2>Overview</h2>
        <p>Target finish date:
          <input id='target-date-input' type='date' value='{load_target_date().isoformat()}' min='{_today().isoformat()}' style='background:#111827;color:#f3f4f6;border:1px solid #374151;border-radius:6px;padding:4px 8px;font-size:14px;' />
          <span id='target-date-status' class='muted' style='margin-left:6px;font-size:12px;'></span>
        </p>
        <p>Today: <strong>{_today().isoformat()}</strong> · Days left: <strong id='days-left'>{_days_left()}</strong> · Available school days: <strong id='available-days'>{available_school_days_left()}</strong></p>
        <div class='excl-box'>
          <div style='display:flex;align-items:center;justify-content:space-between;gap:8px;'>
            <strong>Excluded dates</strong>
            <span class='muted' id='exclusions-summary' style='font-size:12px;'></span>
          </div>
          <ul id='exclusions-list' style='list-style:none;padding:0;margin:8px 0 8px;'></ul>
          <div class='excl-form'>
            <input type='date' id='excl-start' aria-label='Start date' />
            <span class='muted'>→</span>
            <input type='date' id='excl-end' aria-label='End date' />
            <input type='text' id='excl-label' placeholder='Label (optional)' />
            <button id='add-exclusion-btn' type='button'>Add</button>
            <span id='add-exclusion-status' class='muted' style='font-size:12px;'></span>
          </div>
        </div>
        <p style='margin-top:10px;'>Progress bars below show one box per <em>lesson</em> (a Learn + Practice group). Green = attempted, gray = not yet attempted. Open <em>Manage lessons</em> on any course to exclude lessons that aren't part of Ryan's curriculum (e.g. "Khan for families" parent resources).</p>
      </div>
      <div class='card'>
        <h2>Recommended cadence</h2>
        <p class='muted' style='margin-top:0;'>Assuming 5 school days per week until <span id='cadence-target-date'>{load_target_date().isoformat()}</span> (<span id='cadence-weeks-left'>{_weeks_left():.1f}</span> weeks left).</p>
        <ul id='cadence-list'>
          <li class='muted'>Calculating from remaining lessons…</li>
        </ul>
        <p class='muted' id='cadence-summary' style='font-size:12px;'></p>
      </div>
    </div>
    <div class='card'>
      <h2>Calendar-aware suggested schedule</h2>
      <div class='toolbar'>
        <label for='lesson-minutes-input' class='muted' style='font-size:13px;'>Avg minutes per lesson:</label>
        <input id='lesson-minutes-input' type='number' min='1' max='600' step='1'
          value='{load_lesson_minutes()}'
          style='width:70px;background:#111827;color:#f3f4f6;border:1px solid #374151;border-radius:6px;padding:4px 8px;font-size:14px;' />
        <span id='lesson-minutes-status' class='muted' style='font-size:12px;'></span>
      </div>
      <p class='muted' id='schedule-note' style='margin-top:0;'>Next 5 school days (Mon–Fri), allocated from the Recommended cadence above.</p>
      <table>
        <thead>
          <tr><th>Date</th><th>Day</th><th>Calendar</th><th>Suggested plan</th><th style='text-align:right;'>Total</th></tr>
        </thead>
        <tbody id='schedule-body'>
          <tr><td colspan='5' class='muted'>Loading next school days…</td></tr>
        </tbody>
      </table>
      <p class='muted' style='font-size:12px;'>API: <a href='/api/dashboard'>/api/dashboard</a> · <a href='/api/school-days'>/api/school-days</a> · <a href='/api/lesson-minutes'>/api/lesson-minutes</a></p>
    </div>
    <div id='course-cards'></div>
    <div class='card'>
      <h2>Live Khan progress</h2>
      <div class='toolbar'>
        <button id='refresh-data-button' type='button'>Refresh Data</button>
        <span id='live-status'>{live_status}</span>
      </div>
      <table>
        <thead>
          <tr><th>Date</th><th>Title</th><th>Course</th><th>Level</th><th>Change</th><th>Score</th><th>Minutes</th></tr>
        </thead>
        <tbody id='activity-feed-body'>
          {''.join(activity_rows) if activity_rows else '<tr><td colspan="6">No live activity items loaded</td></tr>'}
        </tbody>
      </table>
    </div>
    <script>
    const initialLiveData = {initial_live_json};
    let currentCourses = {courses_json};

    function escapeHtml(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }}

    function renderCourseCard(course) {{
      const unitsHtml = (course.units || []).map((unit, unitIdx) => {{
        const boxes = (unit.lessons || []).map((lesson, lessonIdx) => {{
          const cls = ['lbox'];
          if (lesson.attempted) cls.push('attempted');
          if (lesson.excluded) cls.push('excluded');
          const label = escapeHtml(`${{lesson.title}} — ${{lesson.attempted ? 'attempted' : 'not attempted'}}${{lesson.excluded ? ' (excluded)' : ''}}`);
          return `<div class='${{cls.join(' ')}}' title='${{label}}'
            data-lesson-id='${{escapeHtml(lesson.id || '')}}'
            data-lesson-slug='${{escapeHtml(lesson.slug || '')}}'></div>`;
        }}).join('');
        return `
        <div class='unit-row' data-unit-idx='${{unitIdx}}'>
          <div class='unit-title'>${{escapeHtml(unit.title)}}<span class='unit-sub' data-unit-sub></span></div>
          <div class='lesson-boxes'>${{boxes}}</div>
        </div>`;
      }}).join('');

      const manageHtml = (course.units || []).map((unit) => {{
        const rows = (unit.lessons || []).map((lesson) => {{
          const cls = lesson.excluded ? 'lesson-toggle excluded' : 'lesson-toggle';
          const lid = escapeHtml(lesson.id || '');
          return `<label class='${{cls}}' data-lesson-label='${{lid}}'>
            <input type='checkbox' ${{lesson.excluded ? 'checked' : ''}}
              data-course='${{escapeHtml(course.slug)}}'
              data-lesson-id='${{lid}}'
              data-lesson-slug='${{escapeHtml(lesson.slug || '')}}' />
            <span>${{escapeHtml(lesson.title)}} <span class='muted' style='font-size:11px;'>(${{lesson.item_count}} items${{lesson.attempted ? ', attempted' : ''}})</span></span>
          </label>`;
        }}).join('');
        return `<div class='unit-manage'><h4>${{escapeHtml(unit.title)}}</h4>${{rows}}</div>`;
      }}).join('');

      return `
      <div class='card' data-course='${{escapeHtml(course.slug)}}'>
        <h2>${{escapeHtml(course.name)}}</h2>
        <div class='muted'>Status: ${{escapeHtml(course.status)}}</div>
        <div class='legend'>
          <span><span class='lbox attempted'></span>attempted</span>
          <span><span class='lbox'></span>not yet</span>
          <span><span class='lbox excluded'></span>excluded</span>
        </div>
        <div class='bar'><div class='fill' style='width:${{course.progress_percent}}%'></div></div>
        <div data-stat-line><strong>${{course.progress_percent}}%</strong> complete · ${{course.estimated_done_items}}/${{course.estimated_total_items}} lessons · ${{course.remaining_items}} left</div>
        <div class='muted' data-week-line>Need about <strong>${{course.per_week_needed}}</strong> lessons/week · Next focus: ${{escapeHtml(course.next_focus)}}</div>
        ${{unitsHtml}}
        <details class='manage'>
          <summary>Manage lessons (check to exclude from progress)</summary>
          ${{manageHtml}}
        </details>
      </div>`;
    }}

    function updateUnitCounts(card) {{
      card.querySelectorAll('.unit-row').forEach((unitRow) => {{
        const boxes = Array.from(unitRow.querySelectorAll('.lbox'));
        const total = boxes.length;
        const excluded = boxes.filter((b) => b.classList.contains('excluded')).length;
        const counted = total - excluded;
        const attempted = boxes.filter((b) => b.classList.contains('attempted') && !b.classList.contains('excluded')).length;
        const sub = unitRow.querySelector('[data-unit-sub]');
        if (sub) {{
          sub.textContent = `${{attempted}}/${{counted}} lessons` + (excluded ? ` (${{excluded}} excluded)` : '');
        }}
      }});
    }}

    function updateCardSummary(card, courseData) {{
      // courseData is the latest course object from /api/dashboard; update the
      // stat line + bar without re-rendering the inner lesson grid / panel so
      // open <details> and focus are preserved.
      const fill = card.querySelector('.bar .fill');
      if (fill) fill.style.width = `${{courseData.progress_percent}}%`;
      const statLine = card.querySelector('[data-stat-line]');
      if (statLine) {{
        statLine.innerHTML = `<strong>${{courseData.progress_percent}}%</strong> complete · ${{courseData.estimated_done_items}}/${{courseData.estimated_total_items}} lessons · ${{courseData.remaining_items}} left`;
      }}
      const weekLine = card.querySelector('[data-week-line]');
      if (weekLine) {{
        weekLine.innerHTML = `Need about <strong>${{courseData.per_week_needed}}</strong> lessons/week · Next focus: ${{escapeHtml(courseData.next_focus)}}`;
      }}
    }}

    // --- Recommended cadence -------------------------------------------------
    // Total school days until the target date, assuming 5 days/week.
    // WEEKS_LEFT is mutable — the target-date input rewrites it in place.
    let WEEKS_LEFT = {_weeks_left():.4f};
    const DAYS_PER_WEEK = 5;

    function readCardProgressStats(card) {{
      const boxes = Array.from(card.querySelectorAll('.lesson-boxes .lbox'));
      const excluded = boxes.filter((b) => b.classList.contains('excluded')).length;
      const counted = boxes.length - excluded;
      const attempted = boxes.filter((b) => b.classList.contains('attempted') && !b.classList.contains('excluded')).length;
      return {{
        counted,
        attempted,
        remaining: Math.max(0, counted - attempted),
        name: (card.querySelector('h2')?.textContent || '').trim(),
      }};
    }}

    function cadenceForCourse(lessonsPerWeek) {{
      // Given a per-week lesson rate, return how many of the 5 available
      // school days the course should occupy and how many lessons fall on
      // each of those "active" days.
      if (lessonsPerWeek <= 0) {{
        return {{days_active: 0, per_active_day: 0, text: 'done — no remaining lessons'}};
      }}
      if (lessonsPerWeek <= DAYS_PER_WEEK) {{
        const days = Math.min(DAYS_PER_WEEK, Math.max(1, Math.ceil(lessonsPerWeek)));
        const perDay = Math.max(1, Math.round(lessonsPerWeek / days));
        const dayWord = days === 1 ? 'day' : 'days';
        const text = days >= DAYS_PER_WEEK ? 'every school day' : `${{days}} ${{dayWord}}/week`;
        return {{days_active: days, per_active_day: perDay, text}};
      }}
      const perDay = Math.max(1, Math.round(lessonsPerWeek / DAYS_PER_WEEK));
      return {{
        days_active: DAYS_PER_WEEK,
        per_active_day: perDay,
        text: `every school day (~${{(lessonsPerWeek / DAYS_PER_WEEK).toFixed(1)}}/day)`,
      }};
    }}

    // Latest computed cadence rows, consumed by renderSchedule().
    let lastCadenceRows = [];

    function renderCadence() {{
      const list = document.getElementById('cadence-list');
      const summary = document.getElementById('cadence-summary');
      if (!list) return;
      const cards = Array.from(document.querySelectorAll('#course-cards .card[data-course]'));
      if (!cards.length || WEEKS_LEFT <= 0) {{
        list.innerHTML = '<li class="muted">No courses to schedule.</li>';
        if (summary) summary.textContent = '';
        lastCadenceRows = [];
        renderSchedule();
        return;
      }}

      const rows = cards.map((card) => {{
        const s = readCardProgressStats(card);
        const lpw = s.remaining / WEEKS_LEFT;
        const c = cadenceForCourse(lpw);
        return {{
          slug: card.dataset.course,
          name: s.name,
          remaining: s.remaining,
          lessons_per_week: lpw,
          days_active: c.days_active,
          per_active_day: c.per_active_day,
          cadence_text: c.text,
        }};
      }});
      lastCadenceRows = rows;

      list.innerHTML = rows.map((r) => {{
        if (r.remaining <= 0) {{
          return `<li><strong>${{escapeHtml(r.name)}}:</strong> <span class='muted'>complete</span></li>`;
        }}
        const rate = r.lessons_per_week >= 10 ? r.lessons_per_week.toFixed(0) : r.lessons_per_week.toFixed(1);
        return `<li><strong>${{escapeHtml(r.name)}}:</strong> ${{rate}} lessons/week · ${{escapeHtml(r.cadence_text)}} <span class='muted'>(${{r.remaining}} left)</span></li>`;
      }}).join('');

      if (summary) {{
        const totalRemaining = rows.reduce((a, r) => a + r.remaining, 0);
        const totalLpw = rows.reduce((a, r) => a + r.lessons_per_week, 0);
        const totalPerDay = totalLpw / DAYS_PER_WEEK;
        summary.textContent = `Combined load: ${{totalLpw.toFixed(1)}} lessons/week (~${{totalPerDay.toFixed(1)}}/day) · ${{totalRemaining}} lessons remaining.`;
      }}

      renderSchedule();
    }}

    function updateCardProgressFromDom(card) {{
      // Recompute the progress bar & stat line immediately from the boxes
      // currently on screen, so exclusion toggles reflect in the bar with no
      // round-trip to the server. The eventual /api/dashboard refresh will
      // also update "lessons/week" (which depends on weeks-left).
      const boxes = Array.from(card.querySelectorAll('.lesson-boxes .lbox'));
      const excludedCount = boxes.filter((b) => b.classList.contains('excluded')).length;
      const counted = boxes.length - excludedCount;
      const attempted = boxes.filter((b) => b.classList.contains('attempted') && !b.classList.contains('excluded')).length;
      const remaining = Math.max(0, counted - attempted);
      const percent = counted ? Math.round((attempted / counted) * 100) : 0;
      const fill = card.querySelector('.bar .fill');
      if (fill) fill.style.width = `${{percent}}%`;
      const statLine = card.querySelector('[data-stat-line]');
      if (statLine) {{
        statLine.innerHTML = `<strong>${{percent}}%</strong> complete · ${{attempted}}/${{counted}} lessons · ${{remaining}} left`;
      }}
    }}

    function renderCourses(courses) {{
      currentCourses = courses;
      const container = document.getElementById('course-cards');

      // Preserve open <details> + scroll before re-rendering.
      const openManage = new Set();
      container.querySelectorAll('.card[data-course]').forEach((card) => {{
        const det = card.querySelector('details.manage');
        if (det && det.open) openManage.add(card.dataset.course);
      }});
      const prevScroll = window.scrollY;

      container.innerHTML = (courses || []).map(renderCourseCard).join('');
      container.querySelectorAll('.card[data-course]').forEach((card) => {{
        updateUnitCounts(card);
        if (openManage.has(card.dataset.course)) {{
          const det = card.querySelector('details.manage');
          if (det) det.open = true;
        }}
      }});
      container.querySelectorAll('input[type="checkbox"][data-lesson-id]').forEach((cb) => {{
        cb.addEventListener('change', onToggleExclusion);
      }});
      window.scrollTo(0, prevScroll);
      renderCadence();
    }}

    // Debounced full refresh so that rapid-fire checkbox clicks don't each
    // trigger a separate dashboard rebuild.
    let pendingRefreshTimer = null;
    async function refreshCoursesFromServer() {{
      try {{
        // Use cached Khan activity — exclusions are a pure local filter, no
        // need to spin up Chromium and re-pull from Khan for each toggle.
        const dashRes = await fetch('/api/dashboard?_=' + Date.now(), {{'cache': 'no-store'}});
        const dash = await dashRes.json();
        // Update each existing card in-place instead of wiping the DOM so any
        // open Manage panel stays open and the checkbox you were using keeps
        // focus + scroll position.
        const container = document.getElementById('course-cards');
        (dash.courses || []).forEach((course) => {{
          const card = container.querySelector(`.card[data-course='${{CSS.escape(course.slug)}}']`);
          if (card) updateCardSummary(card, course);
        }});
        currentCourses = dash.courses || currentCourses;
        renderCadence();
      }} catch (err) {{
        console.warn('dashboard refresh failed:', err);
      }}
    }}

    function scheduleDashboardRefresh() {{
      if (pendingRefreshTimer) clearTimeout(pendingRefreshTimer);
      pendingRefreshTimer = setTimeout(() => {{
        pendingRefreshTimer = null;
        refreshCoursesFromServer();
      }}, 400);
    }}

    async function onToggleExclusion(event) {{
      const cb = event.target;
      const courseSlug = cb.dataset.course;
      const lessonId = cb.dataset.lessonId || null;
      const lessonSlug = cb.dataset.lessonSlug || null;
      const excluded = cb.checked;

      cb.disabled = true;
      try {{
        const res = await fetch('/api/exclusions', {{
          method: 'POST',
          headers: {{'content-type': 'application/json'}},
          body: JSON.stringify({{course_slug: courseSlug, lesson_id: lessonId, lesson_slug: lessonSlug, excluded}}),
        }});
        if (!res.ok) throw new Error('server rejected exclusion change');

        // Apply the change locally without re-rendering the whole card so the
        // Manage panel stays open and additional boxes can be checked freely.
        const card = cb.closest('.card[data-course]');
        if (card) {{
          const label = card.querySelector(`label[data-lesson-label='${{CSS.escape(lessonId || '')}}']`);
          if (label) label.classList.toggle('excluded', excluded);
          const box = card.querySelector(`.lbox[data-lesson-id='${{CSS.escape(lessonId || '')}}']`);
          if (box) box.classList.toggle('excluded', excluded);
          updateUnitCounts(card);
          // Move the progress bar immediately from the DOM so the user sees
          // the effect of the toggle without waiting on a server round-trip.
          updateCardProgressFromDom(card);
          renderCadence();
        }}

        // Ask the server for authoritative progress numbers (and lessons/week,
        // which depends on weeks-left), but only after a short debounce so
        // rapid toggles collapse into a single round-trip.
        scheduleDashboardRefresh();
      }} catch (err) {{
        cb.checked = !cb.checked;
        alert('Could not save exclusion: ' + (err?.message || err));
      }} finally {{
        cb.disabled = false;
      }}
    }}

    function buildStatusText(data) {{
      if (data.ok) {{
        return `Loaded ${{data.item_count || 0}} live activity items from ${{data.source || 'unknown'}}. Exercise min: ${{data.totals?.exerciseMinutes ?? 'n/a'}} · Total learning min: ${{data.totals?.totalMinutes ?? 'n/a'}}`;
      }}
      return `Live activity feed unavailable: ${{data.error || 'unknown error'}}`;
    }}

    function renderActivityFeed(data) {{
      const statusEl = document.getElementById('live-status');
      const bodyEl = document.getElementById('activity-feed-body');
      statusEl.textContent = buildStatusText(data);

      const items = Array.isArray(data.activity) ? data.activity.slice(0, 10) : [];
      if (!items.length) {{
        bodyEl.innerHTML = '<tr><td colspan="7">No live activity items loaded</td></tr>';
        return;
      }}

      bodyEl.innerHTML = items.map((item) => {{
        return `<tr>
          <td>${{escapeHtml(item.date)}}</td>
          <td>${{escapeHtml(item.title)}}</td>
          <td>${{escapeHtml(item.course)}}</td>
          <td>${{escapeHtml(item.level || '')}}</td>
          <td>${{escapeHtml(item.change || '')}}</td>
          <td>${{escapeHtml(item.correct_total || '')}}</td>
          <td>${{escapeHtml(item.time_min ?? '')}}</td>
        </tr>`;
      }}).join('');
    }}

    async function refreshActivityFeed() {{
      const button = document.getElementById('refresh-data-button');
      const statusEl = document.getElementById('live-status');
      button.disabled = true;
      const oldText = button.textContent;
      button.textContent = 'Refreshing...';
      statusEl.textContent = 'Refreshing live activity data...';
      try {{
        const dashRes = await fetch('/api/dashboard?refresh=1&_=' + Date.now(), {{'cache': 'no-store'}});
        const dash = await dashRes.json();
        const live = dash.live_progress || {{}};
        renderActivityFeed(live);
        renderCourses(dash.courses || []);
        refreshSchoolDays();
      }} catch (err) {{
        statusEl.textContent = `Refresh failed: ${{err?.message || err}}`;
      }} finally {{
        button.disabled = false;
        button.textContent = oldText;
      }}
    }}

    // --- Suggested schedule state (declared before init so renderCadence ->
    //     renderSchedule doesn't hit the TDZ on initial render) --------------
    let lessonMinutes = parseInt(document.getElementById('lesson-minutes-input').value, 10) || 20;
    let schoolDays = [];  // [{{date, weekday, busy, busy_count}}, ...]

    document.getElementById('refresh-data-button').addEventListener('click', refreshActivityFeed);
    renderCourses(currentCourses);
    renderActivityFeed(initialLiveData);

    function formatMinutes(totalMin) {{
      if (totalMin <= 0) return '0m';
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      if (h && m) return `${{h}}h ${{m}}m`;
      if (h) return `${{h}}h`;
      return `${{m}}m`;
    }}

    function renderSchedule() {{
      const body = document.getElementById('schedule-body');
      if (!body) return;
      if (!schoolDays.length) {{
        body.innerHTML = '<tr><td colspan="5" class="muted">Loading next school days…</td></tr>';
        return;
      }}
      const active = (lastCadenceRows || []).filter((r) => r.remaining > 0 && r.per_active_day > 0);
      if (!active.length) {{
        body.innerHTML = '<tr><td colspan="5" class="muted">No remaining lessons — nothing to schedule.</td></tr>';
        return;
      }}

      const rows = schoolDays.map((day, dayIdx) => {{
        const blocks = active
          .filter((r) => dayIdx < r.days_active)
          .map((r) => {{
            const lessons = r.per_active_day;
            const mins = lessons * lessonMinutes;
            return {{name: r.name, lessons, minutes: mins}};
          }});
        const totalMin = blocks.reduce((a, b) => a + b.minutes, 0);
        const busyHtml = day.busy_count
          ? day.busy.map(escapeHtml).join('; ')
          : '<span class="muted">clear</span>';
        const planHtml = blocks.length
          ? blocks.map((b) => `<strong>${{escapeHtml(b.name)}}:</strong> ${{b.lessons}} lesson${{b.lessons === 1 ? '' : 's'}} (${{b.minutes}}m)`).join('<br>')
          : '<span class="muted">review / catch-up</span>';
        return `<tr>
          <td>${{escapeHtml(day.date)}}</td>
          <td>${{escapeHtml(day.weekday)}}</td>
          <td>${{busyHtml}}</td>
          <td>${{planHtml}}</td>
          <td style='text-align:right;'>${{formatMinutes(totalMin)}}</td>
        </tr>`;
      }}).join('');
      body.innerHTML = rows;
    }}

    async function refreshSchoolDays() {{
      try {{
        const res = await fetch('/api/school-days?count=5&_=' + Date.now(), {{'cache': 'no-store'}});
        if (!res.ok) throw new Error('status ' + res.status);
        const payload = await res.json();
        schoolDays = payload.days || [];
      }} catch (err) {{
        console.warn('school-days fetch failed:', err);
        schoolDays = [];
      }}
      renderSchedule();
    }}

    // --- Lesson-minutes input (persisted) ---
    const lessonMinutesInput = document.getElementById('lesson-minutes-input');
    const lessonMinutesStatus = document.getElementById('lesson-minutes-status');
    let lessonMinutesSaveTimer = null;

    async function saveLessonMinutes(newValue) {{
      lessonMinutesStatus.textContent = 'Saving…';
      try {{
        const res = await fetch('/api/lesson-minutes', {{
          method: 'POST',
          headers: {{'content-type': 'application/json'}},
          body: JSON.stringify({{minutes_per_lesson: newValue}}),
        }});
        if (!res.ok) {{
          const msg = await res.text().catch(() => '');
          throw new Error(msg || ('status ' + res.status));
        }}
        const payload = await res.json();
        lessonMinutes = payload.minutes_per_lesson;
        lessonMinutesStatus.textContent = `Saved (${{lessonMinutes}}m per lesson)`;
        renderSchedule();
      }} catch (err) {{
        lessonMinutesStatus.textContent = 'Save failed: ' + (err?.message || err);
      }}
    }}

    if (lessonMinutesInput) {{
      lessonMinutesInput.addEventListener('input', (ev) => {{
        const v = parseInt(ev.target.value, 10);
        if (!v || v < 1 || v > 600) return;
        lessonMinutes = v;
        renderSchedule();  // instant local update
        if (lessonMinutesSaveTimer) clearTimeout(lessonMinutesSaveTimer);
        lessonMinutesSaveTimer = setTimeout(() => {{
          lessonMinutesSaveTimer = null;
          saveLessonMinutes(v);
        }}, 500);
      }});
    }}

    refreshSchoolDays();

    // --- Connect to Khan Academy button ---
    const connectBtn = document.getElementById('connect-khan-btn');
    const connectStatus = document.getElementById('connect-status');
    let connectPollTimer = null;

    function applyConnectState(state) {{
      const running = !!state.running;
      const seeded = !!state.seeded;
      connectBtn.classList.toggle('running', running);
      connectBtn.classList.toggle('seeded', seeded && !running);
      if (running) {{
        connectBtn.textContent = 'Sign-in window open…';
        connectBtn.disabled = true;
        connectStatus.textContent = 'Close the Chromium window when you are signed in.';
      }} else {{
        connectBtn.disabled = false;
        if (seeded) {{
          connectBtn.textContent = 'Reconnect to Khan Academy';
          connectStatus.textContent = 'Backend session seeded — running headless.';
        }} else {{
          connectBtn.textContent = 'Connect to Khan Academy';
          connectStatus.textContent = 'Not signed in yet — click to open a login window.';
        }}
      }}
    }}

    async function refreshConnectStatus() {{
      try {{
        const res = await fetch('/api/khan/connect');
        if (!res.ok) throw new Error('status ' + res.status);
        const state = await res.json();
        applyConnectState(state);
        if (!state.running && connectPollTimer) {{
          clearInterval(connectPollTimer);
          connectPollTimer = null;
          // A sign-in that just finished: refresh the dashboard data.
          if (state.seeded) {{
            refreshActivityFeed();
          }}
        }}
      }} catch (err) {{
        connectStatus.textContent = 'Status check failed: ' + (err?.message || err);
      }}
    }}

    async function launchConnect() {{
      connectBtn.disabled = true;
      connectBtn.textContent = 'Opening window…';
      connectStatus.textContent = 'Launching Chromium…';
      try {{
        const res = await fetch('/api/khan/connect', {{ method: 'POST' }});
        if (!res.ok) {{
          const txt = await res.text().catch(() => '');
          throw new Error(txt || ('status ' + res.status));
        }}
      }} catch (err) {{
        connectStatus.textContent = 'Failed to launch: ' + (err?.message || err);
        connectBtn.disabled = false;
        connectBtn.textContent = 'Connect to Khan Academy';
        return;
      }}
      refreshConnectStatus();
      if (connectPollTimer) clearInterval(connectPollTimer);
      connectPollTimer = setInterval(refreshConnectStatus, 2000);
    }}

    connectBtn.addEventListener('click', launchConnect);
    refreshConnectStatus();

    // --- Target finish date ---
    const targetDateInput = document.getElementById('target-date-input');
    const targetDateStatus = document.getElementById('target-date-status');
    let lastAcceptedTargetDate = targetDateInput ? targetDateInput.value : null;
    let targetDateSaveTimer = null;

    function applyTargetDateState(payload) {{
      // Cadence math is driven by *available* school days / 5, so vacations
      // and other excluded ranges are subtracted automatically.
      const availableWeeks = payload.available_weeks
        ?? Math.max(1.0, (payload.available_school_days || 0) / 5);
      WEEKS_LEFT = Math.max(1.0, availableWeeks);
      const daysLeftEl = document.getElementById('days-left');
      if (daysLeftEl) daysLeftEl.textContent = payload.days_left;
      const availEl = document.getElementById('available-days');
      if (availEl && payload.available_school_days !== undefined) {{
        availEl.textContent = payload.available_school_days;
      }}
      const cdText = document.getElementById('cadence-target-date');
      if (cdText) cdText.textContent = payload.target_date;
      const wlText = document.getElementById('cadence-weeks-left');
      if (wlText) wlText.textContent = (WEEKS_LEFT).toFixed(1);
      renderCadence();
    }}

    async function saveTargetDate(newValue) {{
      targetDateStatus.textContent = 'Saving…';
      try {{
        const res = await fetch('/api/target-date', {{
          method: 'POST',
          headers: {{'content-type': 'application/json'}},
          body: JSON.stringify({{target_date: newValue}}),
        }});
        if (!res.ok) {{
          const msg = await res.text().catch(() => '');
          throw new Error(msg || ('status ' + res.status));
        }}
        const payload = await res.json();
        lastAcceptedTargetDate = payload.target_date;
        targetDateStatus.textContent = `Saved · ${{payload.days_left}} days left`;
        applyTargetDateState(payload);
        // Pull authoritative per-course per_week_needed (uses new weeks-left).
        scheduleDashboardRefresh();
      }} catch (err) {{
        targetDateStatus.textContent = 'Save failed: ' + (err?.message || err);
        if (lastAcceptedTargetDate) targetDateInput.value = lastAcceptedTargetDate;
      }}
    }}

    if (targetDateInput) {{
      targetDateInput.addEventListener('change', (ev) => {{
        const v = ev.target.value;
        if (!v || v === lastAcceptedTargetDate) return;
        if (targetDateSaveTimer) clearTimeout(targetDateSaveTimer);
        targetDateSaveTimer = setTimeout(() => {{
          targetDateSaveTimer = null;
          saveTargetDate(v);
        }}, 250);
      }});
    }}

    // --- Date exclusions (vacations, holidays) ---
    const exclusionsList = document.getElementById('exclusions-list');
    const exclusionsSummary = document.getElementById('exclusions-summary');
    const exclStart = document.getElementById('excl-start');
    const exclEnd = document.getElementById('excl-end');
    const exclLabel = document.getElementById('excl-label');
    const addExclusionBtn = document.getElementById('add-exclusion-btn');
    const addExclusionStatus = document.getElementById('add-exclusion-status');

    function renderExclusions(payload) {{
      const ranges = payload?.exclusions || [];
      if (!ranges.length) {{
        exclusionsList.innerHTML = '<li class="empty">No excluded ranges — every Mon-Fri counts toward the cadence.</li>';
      }} else {{
        exclusionsList.innerHTML = ranges.map((r) => {{
          const label = r.label ? `<span class='meta'>${{escapeHtml(r.label)}}</span>` : '';
          const range = r.start === r.end ? escapeHtml(r.start) : `${{escapeHtml(r.start)}} → ${{escapeHtml(r.end)}}`;
          return `<li>
            <span><strong>${{range}}</strong> ${{label}}</span>
            <button type='button' class='del' data-excl-id='${{escapeHtml(r.id)}}' title='Remove this range'>×</button>
          </li>`;
        }}).join('');
      }}
      if (exclusionsSummary) {{
        const excluded = payload?.school_days_excluded ?? 0;
        const available = payload?.available_school_days ?? 0;
        exclusionsSummary.textContent = excluded
          ? `${{excluded}} school day${{excluded === 1 ? '' : 's'}} removed · ${{available}} available`
          : (available ? `${{available}} school days available` : '');
      }}
      exclusionsList.querySelectorAll('button.del[data-excl-id]').forEach((btn) => {{
        btn.addEventListener('click', () => removeExclusion(btn.dataset.exclId));
      }});
      // Propagate the new weeks-left to cadence + schedule.
      applyTargetDateState(payload);
      // Also pull authoritative per-course numbers (per_week_needed uses new weeks-left).
      scheduleDashboardRefresh();
      // And the next-5-school-days list, which skips excluded ranges too.
      refreshSchoolDays();
    }}

    async function refreshExclusions() {{
      try {{
        const res = await fetch('/api/date-exclusions?_=' + Date.now(), {{'cache': 'no-store'}});
        if (!res.ok) throw new Error('status ' + res.status);
        const payload = await res.json();
        renderExclusions(payload);
      }} catch (err) {{
        exclusionsSummary.textContent = 'Failed to load: ' + (err?.message || err);
      }}
    }}

    async function addExclusion() {{
      const start = exclStart.value;
      const end = exclEnd.value || start;
      const label = exclLabel.value.trim();
      if (!start) {{
        addExclusionStatus.textContent = 'Pick a start date.';
        return;
      }}
      addExclusionStatus.textContent = 'Saving…';
      addExclusionBtn.disabled = true;
      try {{
        const res = await fetch('/api/date-exclusions', {{
          method: 'POST',
          headers: {{'content-type': 'application/json'}},
          body: JSON.stringify({{start, end, label}}),
        }});
        if (!res.ok) {{
          const msg = await res.text().catch(() => '');
          throw new Error(msg || ('status ' + res.status));
        }}
        const payload = await res.json();
        addExclusionStatus.textContent = 'Added.';
        exclLabel.value = '';
        exclStart.value = '';
        exclEnd.value = '';
        renderExclusions(payload);
      }} catch (err) {{
        addExclusionStatus.textContent = 'Failed: ' + (err?.message || err);
      }} finally {{
        addExclusionBtn.disabled = false;
      }}
    }}

    async function removeExclusion(id) {{
      try {{
        const res = await fetch('/api/date-exclusions/' + encodeURIComponent(id), {{method: 'DELETE'}});
        if (!res.ok) throw new Error('status ' + res.status);
        const payload = await res.json();
        renderExclusions(payload);
      }} catch (err) {{
        exclusionsSummary.textContent = 'Remove failed: ' + (err?.message || err);
      }}
    }}

    addExclusionBtn.addEventListener('click', addExclusion);
    // Autofill end = start when user tabs out of start.
    exclStart.addEventListener('change', () => {{
      if (!exclEnd.value) exclEnd.value = exclStart.value;
    }});
    refreshExclusions();
    </script>
    </body></html>
    """
