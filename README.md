# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization with an audit trail, and analytics.

## Technology Stack
- Python 3.11+
- FastAPI
- MongoDB
- PyMongo

## Features
- Create and list employees.
- Record attendance punch-in and punch-out.
- Calculate late arrivals and overtime.
- Regularize attendance with reasons and audit history.
- Generate monthly employee attendance analytics.
- Generate department summaries, late-arrival leaderboards, and department trends.
- MongoDB indexes and query explain functionality.

## Setup Instructions

1. Install Python 3.11+ and configure a MongoDB database.
2. Install dependencies:

   `python -m pip install -r requirements.txt`

3. Set `MONGO_URI` and `MONGO_DB` as environment variables. Never commit database credentials or `.env` files.
4. Start the API from the repository root:

   `uvicorn app.main:app --port 8000`

5. Open API documentation at `http://127.0.0.1:8000/docs`.
6. Check API health at `http://127.0.0.1:8000/health`.

## Documentation
- `REVIEW.md` contains code review findings.
- `DECISIONS.md` explains design decisions.
