# Decisions

## Assignment-specific behavior is pending

No attendance model, endpoint, field name, response format, punch rule,
correction policy, or analytics formula is being inferred. Those choices must
come from the official assignment kit.

## Backend technology

The application backend uses Python FastAPI and PyMongo. PyMongo's synchronous
driver is used because it is suitable for this scope and keeps database code
straightforward to explain. The existing `/api` service mount and generic
`/api/healthz` starter contract are retained; Swagger UI is exposed at
`/api/docs`.

## MongoDB configuration

MongoDB is external to this environment. The app reads `MONGODB_URI` from the
environment and never stores credentials in source files. No MongoDB database,
collection, or document structure is selected until the assignment kit
specifies it.

## No substitute data

The dashboard must not present fabricated attendance records or calculations.
Until the assignment kit defines the real API contract, the frontend may show
only setup/API availability information.
