from datetime import date, timedelta
import json
import os
import re
import subprocess
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from app.services.khan_cdp_progress import fetch_progress_via_cdp

load_dotenv()

app = FastAPI(title='Khan Homeschool Dashboard')

BASE_DIR = Path(__file__).resolve().parents[2]
TARGET_DATE = date(2026, 5, 8)
TODAY = date(2026, 3, 18)
DAYS_LEFT = (TARGET_DATE - TODAY).days
WEEKS_LEFT = max(1, DAYS_LEFT / 7)

# Configure via environment variables — see .env.example
RYAN_CALENDAR_ID = os.getenv('RYAN_CALENDAR_ID', '')
GOG_ACCOUNT = os.getenv('GOG_ACCOUNT', '')

COURSE_CATALOG_PATH = BASE_DIR / 'research' / 'khan' / 'course_catalog.json'
COURSE_BLUEPRINTS = [
    {
        'slug': 'cc-sixth-grade-math',
        'name': '6th Grade Math',
        'status': 'well underway',
        'next_focus': 'Finish current volume/geometry work, then continue next unit',
    },
    {
        'slug': 'ms-physics',
        'name': 'Middle School Physics',
        'status': 'underway',
        'next_focus': 'Continue gravity/energy work, then move to the next physics topic',
    },
    {
        'slug': 'oer-project-big-history',
        'name': 'OER Project: Big History',
        'status': 'underway',
        'next_focus': 'Continue current Big History sequence and keep steady weekly pace',
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


def iter_catalog_objects(course_entry: dict):
    seen = set()
    for section in course_entry.get('units_from_text', []):
        unit_name = section.get('unit', '')
        if unit_name.strip().lower() == 'course challenge':
            continue
        for lesson in section.get('lessons', []):
            lesson = (lesson or '').strip()
            normalized = normalize_title(lesson)
            if not lesson or not normalized:
                continue
            if normalized in seen:
                continue
            if len(title_token_set(normalized)) < 2:
                continue
            seen.add(normalized)
            yield {
                'unit': unit_name,
                'title': lesson,
                'normalized': normalized,
            }


def build_courses_from_live_data(live_data: dict) -> list[dict]:
    catalog = load_course_catalog()
    activity = live_data.get('activity', []) if isinstance(live_data, dict) else []

    activity_by_course = {}
    for item in activity:
        course_name = (item.get('course') or '').strip().lower()
        if course_name:
            activity_by_course.setdefault(course_name, []).append(item)

    courses = []
    for blueprint in COURSE_BLUEPRINTS:
        catalog_entry = catalog.get(blueprint['slug'], {})
        objects = list(iter_catalog_objects(catalog_entry))

        completed_titles = set()
        recent_titles = []
        matched_pairs = []

        for item in activity_by_course.get(blueprint['name'].lower(), []):
            raw_title = (item.get('title') or '').strip()
            if raw_title and raw_title not in recent_titles:
                recent_titles.append(raw_title)

            best_obj = None
            best_score = 0
            for obj in objects:
                score = title_match_score(raw_title, obj['title'])
                if score > best_score:
                    best_score = score
                    best_obj = obj

            if best_obj and best_score >= 20:
                completed_titles.add(best_obj['normalized'])
                matched_pairs.append({'activity': raw_title, 'catalog': best_obj['title'], 'unit': best_obj['unit'], 'score': best_score})

        estimated_total_items = len(objects)
        estimated_done_items = len(completed_titles)
        remaining = max(0, estimated_total_items - estimated_done_items)
        progress_percent = round((estimated_done_items / estimated_total_items) * 100) if estimated_total_items else 0

        courses.append({
            'slug': blueprint['slug'],
            'name': blueprint['name'],
            'status': blueprint['status'],
            'recent': recent_titles[:5],
            'estimated_total_items': estimated_total_items,
            'estimated_done_items': estimated_done_items,
            'remaining_items': remaining,
            'progress_percent': progress_percent,
            'per_week_needed': round(remaining / WEEKS_LEFT, 1) if estimated_total_items else 0,
            'next_focus': blueprint['next_focus'],
            'matched_titles': matched_pairs[:10],
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


def build_schedule(start_date: date = TODAY, days: int = 14):
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


def get_updated_activity_data():
    """Return the latest structured Khan activity feed available to the backend."""
    try:
        payload = fetch_progress_via_cdp()
        payload['source'] = 'khan-cdp-live'
        payload['item_count'] = len(payload.get('activity', []))
        return payload
    except Exception as exc:
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
def dashboard():
    live_progress = get_updated_activity_data()
    courses = build_courses_from_live_data(live_progress)
    total_remaining = sum(c['remaining_items'] for c in courses)
    return {
        'learner': 'Ryan',
        'username': 'gustywarrior',
        'target_date': TARGET_DATE.isoformat(),
        'today': TODAY.isoformat(),
        'days_left': DAYS_LEFT,
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


@app.get('/', response_class=HTMLResponse)
def home():
    live = get_updated_activity_data()
    courses = build_courses_from_live_data(live)
    cards = []
    for c in courses:
        cards.append(f"""
        <div class='card'>
          <h2>{c['name']}</h2>
          <div class='muted'>Status: {c['status']}</div>
          <div class='bar'><div class='fill' style='width:{c['progress_percent']}%'></div></div>
          <div><strong>{c['progress_percent']}%</strong> estimated complete · {c['remaining_items']} items left</div>
          <div class='muted'>Need about <strong>{c['per_week_needed']}</strong> items/week</div>
          <p><strong>Recent:</strong> {', '.join(c['recent'])}</p>
          <p><strong>Next focus:</strong> {c['next_focus']}</p>
        </div>
        """)

    cal = build_schedule()
    rows = []
    for d in cal['suggested_days'][:10]:
        blocks = ', '.join([f"{b['time']} {b['subject']} {b['minutes']}m" for b in d['blocks']])
        busy = '; '.join(d['busy']) if d['busy'] else 'No existing calendar events'
        rows.append(f"<tr><td>{d['date']}</td><td>{d['busy_count']}</td><td>{busy}</td><td>{blocks}</td></tr>")

    activity_rows = []
    for item in live.get('activity', [])[:10]:
        activity_rows.append(
            f"<tr><td>{item.get('date','')}</td><td>{item.get('title','')}</td><td>{item.get('course','')}</td><td>{item.get('level','') or ''}</td><td>{item.get('change','') or ''}</td><td>{item.get('correct_total','') or ''}</td><td>{item.get('time_min','') if item.get('time_min') is not None else ''}</td></tr>"
        )

    empty_note = "Ryan's calendar is currently empty in the next two weeks, so the planner is assigning fuller school days everywhere." if cal['calendar_is_empty'] else "Calendar events detected — heavier work is being pushed toward emptier days."
    live_status = f"Loaded {live.get('item_count', 0)} live activity items from backend source {live.get('source', 'unknown')}. Exercise min: {live.get('totals', {}).get('exerciseMinutes', 'n/a')} · Total learning min: {live.get('totals', {}).get('totalMinutes', 'n/a')}" if live.get('ok') else f"Live activity feed unavailable: {live.get('error', 'unknown error')}"
    initial_live_json = json.dumps(live).replace('</', '<\\/')

    return f"""
    <html><head><title>Ryan Khan Dashboard</title>
    <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; background: #111827; color: #f3f4f6; }}
    .top {{ display:grid; grid-template-columns: 1.2fr .8fr; gap:20px; }}
    .card {{ background:#1f2937; border-radius:14px; padding:20px; margin-bottom:18px; box-shadow:0 2px 10px rgba(0,0,0,.25); }}
    .bar {{ height:12px; background:#374151; border-radius:999px; overflow:hidden; margin:12px 0; }}
    .fill {{ height:100%; background:#60a5fa; }}
    .muted {{ color:#cbd5e1; }}
    ul {{ line-height:1.6; }}
    a {{ color:#93c5fd; }}
    table {{ width:100%; border-collapse: collapse; }}
    td, th {{ text-align:left; border-bottom:1px solid #374151; padding:10px 8px; vertical-align:top; }}
    .button, button {{ display:inline-block; background:#2563eb; color:#fff; padding:10px 14px; border-radius:10px; text-decoration:none; font-weight:600; border:0; cursor:pointer; }}
    button[disabled] {{ opacity:.65; cursor:wait; }}
    .toolbar {{ display:flex; gap:12px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }}
    </style></head><body>
    <h1>Ryan Khan Dashboard</h1>
    <div class='top'>
      <div class='card'>
        <h2>Overview</h2>
        <p>Target finish date: <strong>{TARGET_DATE.isoformat()}</strong></p>
        <p>Days left: <strong>{DAYS_LEFT}</strong></p>
        <p>This is still not the final refresh-button flow, but the backend now has a real method for returning the latest structured live activity data artifact.</p>
      </div>
      <div class='card'>
        <h2>Recommended cadence</h2>
        <ul>
          <li>Math every weekday</li>
          <li>Physics 3x/week</li>
          <li>Big History 3-4x/week</li>
          <li>Friday = review / catch-up</li>
        </ul>
      </div>
    </div>
    {''.join(cards)}
    <div class='card'>
      <h2>Live Khan progress</h2>
      <div class='toolbar'>
        <button id='refresh-data-button' type='button'>Refresh Data</button>
        <a class='button' href='/api/khan/progress/live'>View live Khan JSON</a>
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
    <div class='card'>
      <h2>Calendar-aware suggested schedule</h2>
      <p>{empty_note}</p>
      <table>
        <tr><th>Date</th><th>Busy events</th><th>Existing calendar</th><th>Suggested school blocks</th></tr>
        {''.join(rows)}
      </table>
      <p class='muted'>API: <a href='/api/dashboard'>/api/dashboard</a> · <a href='/api/calendar-plan'>/api/calendar-plan</a> · <a href='/api/khan/progress/live'>/api/khan/progress/live</a></p>
    </div>
    <script>
    const initialLiveData = {initial_live_json};

    function escapeHtml(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
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
        const response = await fetch(`/api/khan/progress/live?_=${{Date.now()}}`, {{'cache': 'no-store'}});
        const data = await response.json();
        renderActivityFeed(data);
      }} catch (err) {{
        statusEl.textContent = `Refresh failed: ${{err?.message || err}}`;
      }} finally {{
        button.disabled = false;
        button.textContent = oldText;
      }}
    }}

    document.getElementById('refresh-data-button').addEventListener('click', refreshActivityFeed);
    renderActivityFeed(initialLiveData);
    </script>
    </body></html>
    """
