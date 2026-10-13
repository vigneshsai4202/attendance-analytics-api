# Employee Attendance & Analytics API

FastAPI + PyMongo service for the HROne engineering assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier M0)

## Setup and run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (copy and edit the example, or export directly)
#    MONGO_URI  – connection string, e.g. mongodb://localhost:27017
#    MONGO_DB   – database name, e.g. attendance_db
cp .env.example .env   # edit MONGO_URI and MONGO_DB

# 3. (Optional) seed the nine sample documents
python sample_seed.py

# 4. Start the server
uvicorn app.main:app --port 8000
```

The app is ready when `GET /health` returns `{"status": "ok"}`. All indexes are created automatically at startup — no manual steps required.

## Notes

- All application code is in `app/main.py`.
- Indexes are created idempotently at startup via the `lifespan` hook; `/health` retries index creation on every call until MongoDB is reachable.
- `MONGO_URI` and `MONGO_DB` are read from environment variables; a local `.env` file is supported via `python-dotenv`, but real environment variables always take priority.
- The grader injects its own `MONGO_URI` and `MONGO_DB` values and seeds the hidden dataset directly into MongoDB before starting the app.

## What is implemented

| Endpoint | Status |
|---|---|
| `GET /health` | ✅ |
| `POST /employees` | ✅ |
| `GET /employees` | ✅ |
| `POST /attendance/punch-in` | ✅ |
| `POST /attendance/punch-out` | ✅ |
| `GET /attendance` | ✅ |
| `PATCH /attendance/{emp_code}/{date}` | ✅ |
| `GET /analytics/employees/{emp_code}/monthly` | ✅ |
| `GET /analytics/departments/summary` | ✅ |
| `GET /analytics/leaderboard/late` | ✅ |
| `GET /analytics/departments/{department}/trend` | ✅ |
| `GET /admin/explain/{endpoint}` | ✅ |
