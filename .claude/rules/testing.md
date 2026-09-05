# Testing

## Framework

- `pytest` for test runner
- `pytest-asyncio` for async test support
- `pytest-cov` for coverage

## Running Tests

```bash
pytest tests/ -v
pytest tests/ -v --cov=schedule_manager
```

## Test Database

Tests require a PostgreSQL database. Connection settings are loaded from the `.env` file at the project root (git-ignored). Copy `.env.example` to `.env` and fill in your local values.

## Test Patterns

- Each test drops and recreates the `schedules` table (full isolation)
- Async tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Validation tests cover invalid/malicious input (SQL injection, XSS, boundary values)
- CLI tests invoke the real subprocess and verify JSON output + exit codes
