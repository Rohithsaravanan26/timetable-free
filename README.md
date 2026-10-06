# Timetable Predictor — Free & Open

AI-powered clash-free timetable generator. **Completely free, no login required.**
Stack: FastAPI (Python) · Supabase (PostgreSQL) · Vercel (hosting)

---

## Project Structure

```
app/
├── api/
│   ├── index.py          ← FastAPI backend (all logic)
│   └── requirements.txt  ← Python dependencies
├── public/
│   └── index.html        ← Frontend (pure HTML/CSS/JS)
├── vercel.json           ← Vercel routing config
└── package.json          ← Vercel project metadata
```

---

## What was removed from the original

| Original feature        | Status        |
|-------------------------|---------------|
| Google OAuth login      | Removed       |
| Razorpay payments       | Removed       |
| Credit system           | Removed       |
| One-device restriction  | Removed       |
| Trial limits            | Removed       |
| JWT auth on all routes  | Removed       |

Everything is now **free and open** — no sign-in, no credit, no paywall.

---

## Deploy to Vercel + Supabase

### Step 1 — Create Supabase database

1. Go to https://supabase.com → New project
2. After it's ready: Project Settings → Database → Connection string → URI
3. Copy the connection string. It looks like:
   ```
   postgresql://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
   ```

### Step 2 — Push code to GitHub

```bash
cd "d:\Timetable free\app"
git init
git add .
git commit -m "Initial commit — free timetable predictor"
git remote add origin https://github.com/YOUR_USERNAME/timetable-free.git
git push -u origin main
```

### Step 3 — Deploy on Vercel

1. Go to https://vercel.com → New Project → Import your GitHub repo
2. Framework Preset: Other
3. Root Directory: leave as / (or set to app/ if you pushed only the app folder)
4. Add Environment Variable:
   - Key: DATABASE_URL
   - Value: your Supabase connection string from Step 1
5. Click Deploy

### Step 4 — Tables are auto-created

On first request, FastAPI's startup event runs Base.metadata.create_all() which creates:
- faculties table
- reviews table

No manual SQL needed.

---

## Run locally

```bash
cd "d:\Timetable free\app"
pip install -r api/requirements.txt

# Set env variable (PowerShell)
$env:DATABASE_URL = "postgresql://postgres:PASSWORD@db.XYZ.supabase.co:5432/postgres"

# Start server
uvicorn api.index:app --reload --port 8000
```

Then open: http://localhost:8000

NOTE: When running locally, temporarily change `const API_BASE = ""` in
`public/index.html` to `const API_BASE = "http://localhost:8000"`.

---

## API Endpoints

| Method | Path                               | Description                        |
|--------|------------------------------------|------------------------------------|
| GET    | /api/health                        | Health check                       |
| POST   | /api/courses                       | Parse timetable to list of courses |
| POST   | /api/generate                      | Generate ranked clash-free options |
| GET    | /api/faculty/search?q=NAME         | Autocomplete faculty names         |
| POST   | /api/review                        | Submit anonymous faculty review    |
| GET    | /api/faculty/{name}/reviews        | Get faculty review summary         |
| GET    | /api/faculty/{id}/courses          | Get courses a faculty has taught   |

---

## Algorithm (unchanged from original)

1. Parser — Extracts courses, sections, faculty, and time slots from pasted text
2. Backtracking — Tries all combinations of chosen courses, prunes clashes early
3. Scorer — Ranks each valid timetable by:
   - Faculty rating (from Supabase reviews)
   - Free days (Saturdays get a bonus)
   - Time-slot penalties (user preferences)
   - Faculty preference/avoidance bonuses
4. Returns top-K results sorted by score descending
