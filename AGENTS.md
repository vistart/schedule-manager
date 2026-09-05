# Schedule Manager

Schedule management system exposing MCP tools and CLI commands for LLM tool calling.

## Tech Stack

- Python 3.12+
- `rhosocial-activerecord` ORM (ActiveRecord pattern)
- `rhosocial-activerecord-postgres` (PostgreSQL backend via psycopg3)
- `mcp` SDK v2 (FastMCP) for MCP server
- `python-dateutil` for RFC 5545 RRULE parsing

## Architecture

```
src/schedule_manager/
├── config.py       # DB connection config (env vars)
├── model.py        # Schedule ActiveRecord model
├── mcp_server.py   # FastMCP server with tools
└── cli.py          # CLI entry point
```

## Rules

See `.claude/rules/` for detailed conventions:
- `project-overview.md` — architecture and purpose
- `code-style.md` — Python coding conventions
- `database-conventions.md` — model and DDL patterns
- `testing.md` — test framework and patterns
