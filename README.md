# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the contract in `openapi.yaml`
(business rules R1–R10), with storage as described in `DATA_MODEL.md`.
All application code lives in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set MONGO_URI / MONGO_DB (real environment variables win)
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required (developed against 7/8). For a local database:
`docker run -p 27017:27017 mongo:7`, or use a free MongoDB Atlas cluster.

Optional sample data: `python sample_seed.py` loads the six sample employees
and nine sample attendance records from `sample_data/`.

## What's implemented

- Employees: create (unique `emp_code`, enforced by a database unique index)
  and paginated, filterable listing.
- Punch-in / punch-out with R1–R5 (IST attendance dates incl. overnight
  shifts, 10-minute grace, 30-minute overtime minimum, half-up work hours,
  half days), race-safe via unique indexes and atomic updates.
- `PATCH /attendance/{emp_code}/{date}` regularizations: derived fields are
  recomputed and exactly one audit entry is appended to `history`;
  concurrent edits lose with 409 instead of overwriting each other's history.
- Analytics as MongoDB aggregation pipelines: employee monthly summary,
  department summary (headcount includes employees with zero logs),
  late leaderboard (standard competition ranking via `$rank`; ties at the
  cutoff are all returned), and department trend (`$densify` gap filling,
  7-day moving average of attendance rates).
- `GET /admin/explain/{endpoint}` returns the `executionStats` explain of the
  same query/pipeline the real endpoint runs; all of them use index scans.
- Indexes are created at startup, idempotently. No `.env` or secrets are
  committed.

See `REVIEW.md` for the review of the starter code and `DECISIONS.md` for
design decisions.
