# Khan Homeschool Dashboard

A homeschool progress tracker built on Khan Academy data. It answers the
questions Khan's own UI doesn't surface cleanly:

- How far along is he, really?
- What's actually left?
- What pace is needed to finish by a target date?
- What should we work on next?

The dashboard walks each tracked course unit-by-unit, lesson-by-lesson, joins
it against live activity from the student's Khan profile, and turns that into
a single page with per-course progress bars, a unit grid (one box per lesson),
a dynamic recommended cadence, and a rolling 5-day suggested schedule.

## Stack

- **Backend:** FastAPI (Python)
- **Automation:** Python + Playwright (Chromium) for the Khan session
- **Storage:** Flat JSON files in `research/khan/` for catalog + user config
- **Frontend:** Server-rendered HTML with vanilla JS on top (React planned)

## What the dashboard shows

Per tracked course:

- Overall progress percent and `done / total / remaining` counts
- Each **Unit** rendered as a grid of boxes — one box per **lesson** (a lesson
  is a Khan group with a Learn + Practice section, not a standalone quiz)
  - Filled green if the lesson has been attempted
  - Empty if not yet attempted
  - Dimmed if excluded from progress tracking
- A **Next focus** line computed from the catalog: the first non-excluded,
  not-yet-attempted lesson in unit order (e.g. `Continue Unit 3 (Energy) —
  Activity: Why doesn't a basketball bounce forever?`)
- An **Exclude** toggle on each lesson — lessons marked excluded (e.g. "Khan
  for families" parent resources, redundant review units) drop out of the
  denominator so 100% means "all the lessons that matter are done"

Across the whole dashboard:

- **Overview** card with the learner's target finish date, average lesson
  length (editable, default 20 min), and a list of **Excluded dates** (any
  number of vacation / holiday ranges that should not count as school days)
- **Recommended cadence** — recomputed live from remaining lessons, available
  school days (M–F, minus excluded date ranges), and target date
- **Suggested schedule** — the next 5 school days with concrete blocks whose
  length matches the recommended cadence and the average-minutes-per-lesson
  setting
- **Connect to Khan Academy** button (top-right) — launches the one-time
  headed login flow when the backend's session expires
- **Refresh Data** button — forces a live Chromium fetch, bypassing the
  in-memory activity cache

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp ../.env.example ../.env
# Edit .env with your calendar ID and Google account (optional)
```

## Running

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8008
```

Open http://localhost:8008

## Khan Academy authentication (headless)

The backend owns its own Chromium profile, so the data feed keeps working
whether or not your everyday Chrome is open.

One-time login (opens a visible Chromium window):

```bash
cd backend
source .venv/bin/activate
python -m app.scripts.khan_login
```

Sign in to Khan Academy in that window (Google SSO or email/password), then
close the window — the script exits on its own. You can also launch the same
flow from the dashboard's **Connect to Khan Academy** button in the top
right; the server tails `/tmp/khan-connect.log` if you need to debug it.

Cookies are persisted to `~/.cache/khan-homeschool/chromium-profile` (override
with `KHAN_PROFILE_DIR`). After that, FastAPI fetches progress headlessly
from a short-lived Chromium it launches itself. Refresh your login by
re-running `python -m app.scripts.khan_login` when Khan's cookies expire
(typically weeks–months).

Fetch-backend selection via `KHAN_FETCH_MODE`:

| Value | Behavior |
|---|---|
| unset / `auto` | Use the persistent profile if seeded, otherwise fall back to CDP |
| `profile` | Always use the backend-owned persistent Chromium profile |
| `cdp` | Attach to a Chrome started with `--remote-debugging-port=9333` |

## Course catalog

The structured catalog of courses → units → lessons lives at
`research/khan/course_catalog.json`. It is built by walking Khan's
`ContentForPath` GraphQL API through the same authenticated session.

To rebuild it (e.g. after Khan edits a course):

```bash
cd backend
source .venv/bin/activate
python -m app.scripts.build_course_catalog
```

Tracked courses are declared in `COURSE_BLUEPRINTS` inside
`backend/app/main.py`. Today that's:

- 6th Grade Math
- Middle School Physics
- OER Project: Big History
- 6th Grade Reading & Vocab

Adding a new course is a matter of appending a blueprint entry
(`slug`, `name`, `khan_subtitle`, `status`) and rerunning the catalog
builder.

## User configuration (persisted)

All user-editable state is stored as flat JSON under `research/khan/` so it
survives restarts and is easy to edit by hand:

| File | Purpose |
|---|---|
| `target_date.json` | Target finish date used by cadence + schedule |
| `lesson_minutes.json` | Average minutes per lesson (drives schedule block length) |
| `lesson_exclusions.json` | Per-course lesson IDs excluded from progress |
| `date_exclusions.json` | Named date ranges removed from the school-day count |

The dashboard edits all of these in-place via its API (see below) — you
shouldn't normally need to touch the files.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /health` | Health check |
| `GET /api/dashboard` | Full dashboard data (JSON) — uses cached activity unless `?refresh=1` |
| `GET /api/calendar-plan` | Calendar-aware schedule for the next 14 days |
| `GET /api/school-days?count=5` | Next N school days, skipping weekends + excluded ranges |
| `GET /api/target-date` / `POST /api/target-date` | Read / update the target finish date |
| `GET /api/lesson-minutes` / `POST /api/lesson-minutes` | Read / update average minutes per lesson |
| `GET /api/date-exclusions` | List excluded date ranges + schedule summary |
| `POST /api/date-exclusions` | Add a date range (`{start, end, label}`) |
| `DELETE /api/date-exclusions/{id}` | Remove a date range |
| `GET /api/exclusions` / `POST /api/exclusions` | Read / toggle lesson exclusions |
| `GET /api/khan/connect` | Poll the state of the headed login subprocess |
| `POST /api/khan/connect` | Launch the headed Khan login (non-blocking) |

## Configuration

Copy `.env.example` to `.env` and set:

- `RYAN_CALENDAR_ID` — Google Calendar ID for the learner's calendar (optional)
- `GOG_ACCOUNT` — Google Workspace account used with the `gog` CLI (optional)
- `KHAN_FETCH_MODE` — `auto` (default) / `profile` / `cdp`
- `KHAN_PROFILE_DIR` — override the Chromium profile location

The `gog` CLI is only needed if you want calendar-aware scheduling. Without
it, the schedule planner treats every weekday as open.

## Notes

- `live-activity-feed.json` is a runtime artifact (gitignored) — populated
  from the authenticated Khan session.
- The Chromium profile directory (`~/.cache/khan-homeschool/chromium-profile`
  by default) contains login cookies. It is *not* tracked by git, and
  generic profile/cookie paths are blocked in `.gitignore` in case anyone
  overrides `KHAN_PROFILE_DIR` to point inside the repo.
