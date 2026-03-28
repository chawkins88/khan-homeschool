# Khan GraphQL Research

Goal: replace UI scraping with direct authenticated GraphQL calls.

## Known facts
- We already captured structured activity data into `live-activity-feed.json`.
- Prior research identified likely internal operations such as:
  - `getFullUserProfile`
  - `courseProgressQuery`
  - `unitProgressForSubject`
  - `ContentForPath`
  - `getCourse`
  - `homepageQueryV4`
- Unauthenticated requests return `user: null`.
- Browser automation is still needed for session bootstrap / cookie refresh.

## Phase 1 deliverables
- A local harness for recording Khan network traffic from an authenticated browser session.
- Storage for captured cookies, headers, and GraphQL request metadata.
- A direct-query Python client that can replay captured GraphQL operations.

## Phase 2 deliverables
- Use Playwright to log in / reuse session.
- Capture real GraphQL operations from the live site.
- Implement direct API fetches using persisted cookies.

## Expected artifacts
- `capture_session.py` — browser network capture
- `graphql_client.py` — direct GraphQL replay helper
- `sample_operations.json` — redacted captured request examples
- `session/` — local ignored cookie/session artifacts
