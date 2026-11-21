# MindCare Backend — AI Risk Engine v2.0 Guide

## 1. Quickstart on Windows (Local Python 3.11+ without Docker)

Virtualenv `.venv` and SQLite migrations are already initialized.

```powershell
cd e:\Project\MindCare\mindcare-backend

# Optional: copy .env.example to .env and add your GEMINI_API_KEY
# (If GEMINI_API_KEY is empty, Dual-Mode automatically uses the high-precision offline Vietnamese engine)
Copy-Item .env.example .env -ErrorAction SilentlyContinue

# Start Backend API server on http://127.0.0.1:8000
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

- **Interactive Swagger API Docs:** http://127.0.0.1:8000/api/v1/docs
- **Health Check:** http://127.0.0.1:8000/api/v1/health

---

## 2. Start Frontend PWA (in a second terminal)

```powershell
cd e:\Project\MindCare\Project-cuoi-ki-_-frontend-
npm run dev
```

- **App URL:** http://localhost:3000 (Vite automatically proxies `/api` to `http://127.0.0.1:8000`)

---

## 3. Run Automated Tests, Watchers & Thesis Benchmark (`VN-MindSafe-Bench`)

```powershell
cd e:\Project\MindCare\mindcare-backend

# 1. Run 17 Unit & Integration Tests
.\.venv\Scripts\python.exe manage.py test -v 2

# 2. Run 4-Configuration Ablation Study & 14-Day Longitudinal Journeys
.\.venv\Scripts\python.exe benchmarks\run_ablation_eval.py
# -> Outputs full Markdown report at: benchmarks\ABLATION_RESULTS.md

# 3. Run Periodic Silent-Dropout (MNAR) & Cliffhanger Watcher
.\.venv\Scripts\python.exe manage.py run_risk_watchers
```

---

## 4. Run with Docker (Optional)

```bash
docker compose up --build
docker compose exec web python manage.py migrate
```
