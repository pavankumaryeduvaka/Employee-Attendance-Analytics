# Review checklist

## Current scaffold

- [ ] Confirm no attendance-specific behavior is presented as complete before
  the official assignment kit is available.
- [ ] Confirm the frontend uses only real API responses and does not contain
  sample attendance records.
- [ ] Confirm MongoDB credentials are supplied through environment secrets,
  never committed to source control.

## After the official kit is provided

- [ ] Cross-check every route, field name, request body, response format,
  validation rule, and error response against the kit.
- [ ] Verify punch-in and punch-out behavior against each stated business rule.
- [ ] Verify manual corrections preserve the complete audit trail required by
  the kit.
- [ ] Verify analytics are computed with MongoDB aggregation pipelines and
  match the kit's stated calculations.
- [ ] Verify the dashboard reads attendance records and analytics from the
  FastAPI backend rather than local fixtures or hardcoded data.
- [ ] Check Swagger UI documents the implemented API contract.
- [ ] Check missing or invalid MongoDB configuration fails clearly for
  database-dependent behavior.
- [ ] Check setup instructions and required project files remain current.
