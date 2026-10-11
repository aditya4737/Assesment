# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the attendance contract in `openapi.yaml`.
Business rules are documented there; MongoDB document shapes are in `DATA_MODEL.md`.

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer (MongoDB 7.0 recommended)
- Dependencies from `requirements.txt`

## Run on Windows (PowerShell)

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Start MongoDB locally. If using Docker Desktop:

```powershell
docker run -d --name attendance-mongodb -p 27017:27017 -v attendance_mongodb_data:/data/db mongo:7
```

The app defaults to `mongodb://localhost:27017` and database `attendance_db`. To use a different MongoDB instance,
set `MONGO_URI` and `MONGO_DB` in the environment before starting the app. A local `.env` file is also loaded, but
should never be committed.

```powershell
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
python -m uvicorn app.main:app --port 8000 --reload
```

Open <http://127.0.0.1:8000/docs> for the API explorer. `/health` checks MongoDB connectivity and returns 503 when
MongoDB is unavailable.

## Sample data

`sample_data/` contains sample MongoDB Extended JSON documents. `sample_seed.py` loads them, **deleting all existing
documents** in the `employees` and `attendance_logs` collections first. Run it only against a disposable database:

```powershell
python sample_seed.py
```

## Submission

Submit a public Git repository containing `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this
README. Do not include `.env`, credentials, or a Dockerfile.
