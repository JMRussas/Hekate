# Load & Stress Tests

These tests are **not run in CI** — they are too slow or require a running server.

## Prerequisites

```bash
pip install -r requirements-dev.txt   # includes locust
```

## Locust HTTP Load Test

Runs against a live server. Use a throwaway database.

```bash
# Start the server with a fresh DB
python run.py

# In another terminal
locust -f tests/load/locustfile.py --host http://localhost:5200

# Then open http://localhost:8089 to configure and start the test
```

**Environment variables:**
- `LOAD_TEST_ADMIN_EMAIL` — admin email (default: `admin_load@test.com`)
- `LOAD_TEST_ADMIN_PASSWORD` — admin password (default: `AdminLoad1234!`)

**Note:** The default rate limit is 60/minute. For meaningful load tests, increase `server.rate_limit` in your config.json (e.g., `"600/minute"`).

## Budget Stress Test

Tests budget concurrency without a running server (uses in-process DB).

```bash
python -m pytest tests/load/budget_stress.py -m slow -v
```

## SSE Stress Test

Tests SSE broadcaster under concurrent subscriber load.

```bash
python -m pytest tests/load/sse_stress.py -m slow -v
```

## Running All Load Tests

```bash
python -m pytest tests/load/ -m slow -v
```
