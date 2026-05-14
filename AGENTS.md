# Volgio Monitor

Web page change monitoring service built with FastAPI + SQLite.

## Cursor Cloud specific instructions

### Running the application

```bash
source /workspace/.venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs at `http://localhost:8000/docs` (Swagger UI).

### Key gotchas

- **bcrypt compatibility**: `passlib==1.7.4` is incompatible with `bcrypt>=4.1`. The update script pins `bcrypt<4.1` to avoid `ValueError: password cannot be longer than 72 bytes` on any hashing operation.
- **email-validator required**: `pydantic[email]` must be installed because `EmailStr` is used in `UserCreate`. This is not listed in `requirements.txt` but is required at import time.
- **Static HTML not mounted**: The HTML files in the repo root are not served by FastAPI (no `StaticFiles` mount). Only the API endpoints at `/auth/*`, `/monitors/*`, `/payments/*`, `/me`, and `/docs` are available.
- **SQLite auto-created**: The database (`app.db`) is created automatically on first startup via `Base.metadata.create_all()`. No migrations needed.
- **APScheduler in-process**: The background scheduler starts automatically with the app and checks monitors every 1 minute. No separate worker process needed.
- **No existing tests**: The repository has no test files. If adding tests, use `pytest` with `httpx` (for async test client) or FastAPI's `TestClient`.

### Environment variables

All optional for local dev. See `README.md` for the full list. The app runs with sensible defaults (SQLite DB, dummy JWT secret).

### Linting

No linting config exists in the repo. For ad-hoc checks:
```bash
source /workspace/.venv/bin/activate
pip install ruff
ruff check app/
```
