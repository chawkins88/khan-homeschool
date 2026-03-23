from datetime import date, timedelta
import json
import os
import subprocess
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title='Khan Homeschool Dashboard')

BASE_DIR = Path(__file__).resolve().parents[2]
LIVE_ACTIVITY_FEED_PATH = BASE_DIR / 'live-activity-feed.json'
TARGET_DATE = date(2026, 5, 8)
TODAY = date(2026, 3, 18)
DAYS_LEFT = (TARGET_DATE - TODAY).days
WEEKS_LEFT = max(1, DAYS_LEFT / 7)

# Configure via environment variables — see .env.example
RYAN_CALENDAR_ID = os.getenv('RYAN_CALENDAR_ID', '')
GOG_ACCOUNT = os.getenv('GOG_ACCOUNT', '')

courses = [
    {
        'slug': 'math-6',
        'name': '6th Grade Math',
        'status': 'well underway',
        'recent': [
            'Volume with fractions',
            'How volume changes from changing dimensions',
            'Volume of a rectangular prism: fractional dimensions',
        ],
        'estimated_total_items': 148,
        'estimated_done_items': 92,
        'next_focus': 'Finish current volume/geometry work, then continue next unit',
    },
    {
        'slug': 'physics',
        'name': 'Middle School Physics',
        'status': 'underway',
        'recent': ['Gravitational force', 'Understand: gravitational force'],
        'estimated_total_items': 22,
        'estimated_done_items': 7,
        'next_focus': 'Continue gravity unit, then move to next physics topic',
    },
    {
        'slug': 'big-history',
        'name': 'OER Project: Big History',
        'status': 'underway',
        'recent': ['Claim Warm-Up', '1.3 practice', '1.4 practice'],
        'estimated_total_items': 34,
        'estimated_done_items': 8,
        'next_focus': 'Continue current Big History sequence and keep steady weekly pace',
    },
]

for c in courses:
    remaining = c['estimated_total_items'] - c['estimated_done_items']
    c['remaining_items'] = remaining
    c['progress_percent'] = round((c['estimated_done_items'] / c['estimated_total_items']) * 100)
    c['per_week_needed'] = round(remaining / WEEKS_LEFT, 1)


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
    if not LIVE_ACTIVITY_FEED_PATH.exists():
        return {
            'ok': False,
            'source': 'live-activity-feed.json',
            'error': f'missing file: {LIVE_ACTIVITY_FEED_PATH}',
            'items': [],
        }

    with LIVE_ACTIVITY_FEED_PATH.open() as f:
        payload = json.load(f)

    stat = LIVE_ACTIVITY_FEED_PATH.stat()
    payload['ok'] = True
    payload['source'] = 'live-activity-feed.json'
    payload['file_mtime'] = stat.st_mtime
    payload['item_count'] = len(payload.get('items', []))
    return payload


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/api/activity-feed/live')
def activity_feed_live():
    return get_updated_activity_data()


@app.get('/api/activity-feed/refresh')
def activity_feed_refresh():
    return get_updated_activity_data()


@app.get('/api/dashboard')
def dashboard():
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
        'live_activity_feed': get_updated_activity_data(),
    }


@app.get('/api/calendar-plan')
def calendar_plan():
    return build_schedule()


@app.get('/', response_class=HTMLResponse)
def home():
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
    live = get_updated_activity_data()
    rows = []
    for d in cal['suggested_days'][:10]:
        blocks = ', '.join([f"{b['time']} {b['subject']} {b['minutes']}m" for b in d['blocks']])
        busy = '; '.join(d['busy']) if d['busy'] else 'No existing calendar events'
        rows.append(f"<tr><td>{d['date']}</td><td>{d['busy_count']}</td><td>{busy}</td><td>{blocks}</td></tr>")

    activity_rows = []
    for item in live.get('items', [])[:10]:
        score = ''
        if item.get('correctCount') is not None and item.get('problemCount') is not None:
            score = f"{item['correctCount']}/{item['problemCount']}"
        activity_rows.append(
            f"<tr><td>{item.get('timestamp','')}</td><td>{item.get('kind','')}</td><td>{item.get('title','')}</td><td>{item.get('subtitle','')}</td><td>{item.get('durationMinutes','')}</td><td>{score}</td></tr>"
        )

    empty_note = "Ryan's calendar is currently empty in the next two weeks, so the planner is assigning fuller school days everywhere." if cal['calendar_is_empty'] else "Calendar events detected — heavier work is being pushed toward emptier days."
    live_status = f"Loaded {live.get('item_count', 0)} live activity items from backend source {live.get('source', 'unknown')}." if live.get('ok') else f"Live activity feed unavailable: {live.get('error', 'unknown error')}"
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
      <h2>Live activity feed</h2>
      <div class='toolbar'>
        <button id='refresh-data-button' type='button'>Refresh Data</button>
        <a class='button' href='/api/activity-feed/live'>View live activity JSON</a>
        <span id='live-status'>{live_status}</span>
      </div>
      <table>
        <thead>
          <tr><th>Timestamp</th><th>Kind</th><th>Title</th><th>Course</th><th>Minutes</th><th>Score</th></tr>
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
      <p class='muted'>API: <a href='/api/dashboard'>/api/dashboard</a> · <a href='/api/calendar-plan'>/api/calendar-plan</a> · <a href='/api/activity-feed/live'>/api/activity-feed/live</a> · <a href='/api/activity-feed/refresh'>/api/activity-feed/refresh</a></p>
    </div>
    <script>
    const initialLiveData = {initial_live_json};

    function escapeHtml(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }}

    function buildStatusText(data) {{
      if (data.ok) {{
        return `Loaded ${{data.item_count || 0}} live activity items from backend source ${{data.source || 'unknown'}}.`;
      }}
      return `Live activity feed unavailable: ${{data.error || 'unknown error'}}`;
    }}

    function renderActivityFeed(data) {{
      const statusEl = document.getElementById('live-status');
      const bodyEl = document.getElementById('activity-feed-body');
      statusEl.textContent = buildStatusText(data);

      const items = Array.isArray(data.items) ? data.items.slice(0, 10) : [];
      if (!items.length) {{
        bodyEl.innerHTML = '<tr><td colspan="6">No live activity items loaded</td></tr>';
        return;
      }}

      bodyEl.innerHTML = items.map((item) => {{
        const score = item.correctCount != null && item.problemCount != null ? `${{item.correctCount}}/${{item.problemCount}}` : '';
        return `<tr>
          <td>${{escapeHtml(item.timestamp)}}</td>
          <td>${{escapeHtml(item.kind)}}</td>
          <td>${{escapeHtml(item.title)}}</td>
          <td>${{escapeHtml(item.subtitle)}}</td>
          <td>${{escapeHtml(item.durationMinutes)}}</td>
          <td>${{escapeHtml(score)}}</td>
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
        const response = await fetch(`/api/activity-feed/refresh?_=${{Date.now()}}`, {{ cache: 'no-store' }});
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
