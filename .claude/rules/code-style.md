# Code Style

## Python

- Python 3.12+ required
- Use `from __future__ import annotations` for forward references
- Type hints on all public functions
- f-strings for string formatting
- No comments unless explicitly asked
- Follow existing code patterns in the project

## Linting

- `ruff` for linting and formatting
- `black` for code formatting
- `isort` for import sorting
- Run `ruff check src/` before committing

## Naming

- `snake_case` for functions, methods, variables
- `PascalCase` for classes
- `UPPER_SNAKE_CASE` for constants
- Descriptive names; avoid single-letter variables except in loops
