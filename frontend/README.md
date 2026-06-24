# Frontend

Vite + React + TypeScript app. Talks to the FastAPI backend in `../backend/`.

## Run modes

The frontend has two modes, switched by the `VITE_BACKEND_URL` env var:

| Mode | When | Behavior |
|------|------|----------|
| **Mock** (default) | `VITE_BACKEND_URL` unset | MSW intercepts every API call and serves the `pr_small` fixture from `src/mocks/`. No backend needed. |
| **Live** | `VITE_BACKEND_URL` set | MSW disables itself; all `fetch`s go to that URL. |

## Local dev — frontend only (mock)

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

## Local dev — full stack (M2)

Terminal 1 — backend (uses in-process fakes by default; no API keys needed):

```bash
cd backend
pip install -e .[dev]
PYTHONPATH=.. uvicorn pr_walkthrough.main:app --reload --port 8000
```

Terminal 2 — frontend pointed at the backend:

```bash
cd frontend
VITE_BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

Open <http://localhost:5173>. The app loads the fixture session; navigate
chunks, hear audio (silent WAV from the fake TTS), submit follow-ups,
flag concerns, and post them (the fake PRSource returns a synthetic URL).

To exercise real Claude + real `gh`, see the M1 CLI in `backend/pr_walkthrough/m1.py`
(separate from the browser UI for now).
