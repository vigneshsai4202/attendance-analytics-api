# Employee Attendance & Analytics API

FastAPI + PyMongo service for the HROne engineering assignment. All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier)

## Run

```bash
pip install -r requirements.txt
```

Set the two environment variables, then start the server from the repository root.

Mac / Linux:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Windows (Command Prompt):

```bat
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```

Windows (PowerShell):

```powershell
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`GET /health` returns `{"status": "ok"}` once MongoDB answers. Interactive docs are at `/docs`.

## Configuration

| Variable | Meaning | Default if not set |
|---|---|---|
| `MONGO_URI` | MongoDB connection string, e.g. `mongodb://localhost:27017` or an Atlas `mongodb+srv://...` URI | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

- To use a different MongoDB (for example Atlas), set `MONGO_URI` to its connection string before starting the app. No code change is needed.
- A local `.env` file with the same two variables is also read (python-dotenv), but real environment variables always take priority. No `.env` is included in this repository.

## Notes

- Indexes are created idempotently at startup. If MongoDB is down at that moment, `/health` retries until the indexes exist.
- Instants are stored as BSON datetimes (UTC) and converted to epoch milliseconds at the API boundary. The client is `tz_aware`.
- Analytics use MongoDB aggregation pipelines. Half-up rounding for averages and rates is done on exact decimal values, not floats.

## Endpoints implemented

`/health`, `POST/GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`, the four `/analytics/...` endpoints and `/admin/explain/{endpoint}`.
