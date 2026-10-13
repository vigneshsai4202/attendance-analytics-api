# Employee Attendance & Analytics API

FastAPI + PyMongo service for the HROne engineering assignment. All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier)

## Run

```bash
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

uvicorn app.main:app --port 8000
```

`GET /health` returns `{"status": "ok"}` once MongoDB answers. Interactive docs are at `/docs`.

## Notes

- `MONGO_URI` and `MONGO_DB` come from environment variables. A local `.env` is also read (python-dotenv), but real environment variables take priority. Nothing is hard-coded except a localhost default.
- Indexes are created idempotently at startup. If MongoDB is down at that moment, `/health` retries until the indexes exist.
- Instants are stored as BSON datetimes (UTC) and converted to epoch milliseconds at the API boundary. The client is `tz_aware`.
- Analytics use MongoDB aggregation pipelines. Half-up rounding for averages and rates is done on exact decimal values, not floats.

## Endpoints implemented

`/health`, `POST/GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`, the four `/analytics/...` endpoints and `/admin/explain/{endpoint}`.

