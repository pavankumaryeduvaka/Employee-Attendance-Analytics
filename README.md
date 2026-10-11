# Employee Attendance Analytics

A React dashboard with a Python FastAPI backend and MongoDB connectivity. This
project is being prepared for a software engineering assignment. The official
assignment kit has not been supplied yet, so attendance rules, domain API
contracts, data fields, audit-trail details, and analytics calculations are
intentionally not implemented.

## Current status

- FastAPI Swagger UI: `/api/docs`
- Generic API health check retained from the workspace starter: `/api/healthz`
- MongoDB driver: PyMongo
- Attendance screens and API operations: pending the official assignment kit
- No hardcoded or sample attendance records are used

## Requirements

- Python 3.11 or newer
- Node.js and pnpm for the React dashboard
- MongoDB 6.0+ reachable over the network when MongoDB-backed features are
  implemented

This Replit environment has Python 3.13, but it does not include a local
`mongod` server. Use an external MongoDB 6.0+ deployment. Do not commit its URI
or credentials to source control.

## Configuration

Set `MONGODB_URI` in Replit Secrets (or the environment where the app runs).
The API verifies the connection at startup when the variable is present. If it
is absent, the API logs that MongoDB is not configured; no in-memory or sample
data store is substituted.

The database name, collections, indexes, and document shapes must come from the
official assignment kit and are not selected by this scaffold.

## Run in Replit

The project has separate managed workflows for the React app and API:

- React app: `pnpm --filter @workspace/employee-attendance run dev`
- FastAPI backend: the API Server workflow runs `uvicorn app.main:app` on port
  `8080` and serves under `/api`

Open the app preview at `/`. Open FastAPI Swagger UI at `/api/docs`.

## Run locally

Install Python dependencies with `python3 -m pip install -r requirements.txt`,
then start FastAPI from the repository root:

```sh
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

In another terminal, start the React app:

```sh
pnpm install
pnpm --filter @workspace/employee-attendance run dev
```

The API paths include the `/api` prefix so they match the existing workspace
service mount.

## Assignment implementation

Provide the official assignment kit before adding attendance-specific
functionality. Implement its exact endpoint paths, field names, request and
response formats, validation, business rules, correction audit trail, and
analytics calculations. Analytics must be implemented with MongoDB aggregation
pipelines as required by the assignment. Do not infer unspecified behavior.

See [DECISIONS.md](DECISIONS.md) for current constraints and [REVIEW.md](REVIEW.md)
for the acceptance checklist.
