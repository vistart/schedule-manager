# Project Overview

## Purpose

A schedule/task management system that exposes its functionality through:
1. **MCP v2.0 tools** — for LLM tool calling via the official `mcp` Python SDK
2. **CLI commands** — for direct command-line usage

## Core Concepts

- **Schedule** — A task or event with title, description, status, priority, timing, and recurrence
- Uses PostgreSQL with JSONB for tags/rdate/exdate fields
- Soft delete via `deleted_at` timestamp (never hard-deletes)
- Auto-managed `created_at`/`updated_at` timestamps

## Dependencies

- `rhosocial-activerecord` — ORM (ActiveRecord pattern, Python)
- `rhosocial-activerecord-postgres` — PostgreSQL backend (psycopg3)
- `mcp[cli]` — Official MCP Python SDK v2 with FastMCP
- `python-dateutil` — RFC 5545 RRULE parsing
