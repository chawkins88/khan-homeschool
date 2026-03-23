# Khan Homeschool Dashboard

A homeschool progress tracker for Ryan built on Khan Academy data.

Answers the questions Khan Academy doesn't surface directly:
- How far along is he, really?
- What's left?
- What pace is needed to finish by a target date?
- What should we do next?

## Stack

- **Backend:** FastAPI (Python)
- **Frontend:** React (planned)
- **Database:** MongoDB (local)
- **Automation:** Python + Playwright (for Khan session bootstrap)

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env
# Edit .env with your calendar ID and Google account
```

## Running

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8008
```

Open http://localhost:8008

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /health` | Health check |
| `GET /api/dashboard` | Full dashboard data (JSON) |
| `GET /api/calendar-plan` | Calendar-aware schedule |
| `GET /api/activity-feed/live` | Latest activity feed |
| `GET /api/activity-feed/refresh` | Refresh activity feed |

## Configuration

Copy `.env.example` to `.env` and set:

- `RYAN_CALENDAR_ID` — Google Calendar ID for the learner's calendar
- `GOG_ACCOUNT` — Google Workspace account email (used with the `gog` CLI)

The `gog` CLI is required for calendar integration. Without it, calendar events
will be skipped and the schedule planner will assume open days.

## Notes

- `live-activity-feed.json` is a runtime artifact (gitignored) — populated by the
  Playwright-based Khan session scraper (in development)
- MongoDB local binaries are included but data dir is gitignored
