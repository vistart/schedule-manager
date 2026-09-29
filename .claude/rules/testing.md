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

## Test Files

| File | Needs a database | Covers |
|---|---|---|
| `test_offline.py` | no | identity context, generated SQL, DDL, validation, server config |
| `test_scopes.py` | no | per-tool scope gate, exercised over ASGI directly |
| `test_live.py` | yes | accounts, credentials, CRUD, queries, the isolation matrix |
| `test_cli.py` | yes | real subprocess: envelope, exit codes, cross-user refusal |

## Test Database

Tests require a PostgreSQL database. Connection settings are loaded from the
`.env` file at the project root (git-ignored). Copy `.env.example` to `.env` and
fill in your local values. `conftest.db_config` **skips** the live suites when
the host is unreachable, so the two offline files still run anywhere.

## Test Patterns

- The `tables` fixture drops *before* creating, not only after. A table left
  over from an older schema survives `CREATE TABLE IF NOT EXISTS` and then
  breaks the `user_id` index.
- Use `db.create_pool()` / `db.connection()`, never `Model.configure()` per class.
  The pool is a per-process resource, so the `pool` fixture is session-scoped and
  only the schema lifecycle is per test; building a pool per test costs a full
  connect plus introspection every time.
- When a test needs to stub a classmethod, use `monkeypatch.setattr`. Assigning
  `Model.method = ...` outlives the test and silently disables real behaviour for
  everything that runs after it — which looks like a flaky failure in a
  completely unrelated class.
- Bind identity with the `as_user` fixture — `Schedule.query()` raises
  `Unauthenticated` with no context, which is itself worth asserting.
- **Assert isolation through generated SQL where possible** (`to_sql()`), which
  needs no database and checks the contract directly rather than by observing
  results.
- Cross-user cases must assert both halves: that the call fails *and* that the
  target row is unchanged.
- Async tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Validation tests cover invalid/malicious input (SQL injection, XSS, boundary values)
- CLI tests invoke the real subprocess and verify JSON output + exit codes.
  They are slow (~14s each) because every call pays the cold-start import cost.
