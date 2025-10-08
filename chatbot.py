from flask import Flask, render_template, request, jsonify, session
import os, re, json, sqlite3
from datetime import datetime

# Try Postgres when DATABASE_URL is set (Railway), else use SQLite locally
PG_AVAILABLE = True
try:
    import psycopg2
    import psycopg2.extras
except Exception:
    PG_AVAILABLE = False

# --- LLM (Gemini) ---
import google.generativeai as genai
from dotenv import load_dotenv

# =========================================================
# Env & LLM setup
# =========================================================
load_dotenv()

GEMINI_API_KEY = os.getenv("AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k") or os.getenv("AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
if not GEMINI_API_KEY:
    raise SystemExit("Missing GEMINI_API_KEY environment variable.")
genai.configure(api_key="AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
model = genai.GenerativeModel(MODEL_NAME)

# =========================================================
# Flask
# =========================================================
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "dev-secret")  # for session memory

# =========================================================
# DB selection (Postgres on Railway, SQLite locally)
# =========================================================
DB_PATH = "local.db"
DATABASE_URL = os.getenv("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL and PG_AVAILABLE)

def connect_sqlite():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def connect_postgres():
    # Railway DATABASE_URL may need sslmode=require
    dsn = DATABASE_URL if "sslmode=" in (DATABASE_URL or "") else f"{DATABASE_URL}?sslmode=require"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn

if USE_POSTGRES:
    pg_conn = connect_postgres()
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    DB_KIND = "postgres"
else:
    sl_conn = connect_sqlite()
    cur = sl_conn.cursor()
    DB_KIND = "sqlite"

def ensure_table():
    if DB_KIND == "postgres":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.wastedata (
                city            TEXT NOT NULL,
                year            INT  NOT NULL,
                wastecollected  INT  NOT NULL,
                recycledwaste   INT  NOT NULL
            );
        """)
        cur.execute("SELECT COUNT(*) AS n FROM public.wastedata;")
        n = cur.fetchone()["n"]
        if n == 0:
            cur.execute("""
                INSERT INTO public.wastedata (city, year, wastecollected, recycledwaste) VALUES
                ('Amman', 2023, 12000, 3200),
                ('Amman', 2024, 13500, 4100),
                ('Zarqa',  2023,  6800, 1500),
                ('Zarqa',  2024,  7200, 1700),
                ('Irbid',  2023,  5400, 1100),
                ('Irbid',  2024,  5900, 1300);
            """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wastedata (
                city TEXT NOT NULL,
                year INTEGER NOT NULL,
                wastecollected INTEGER NOT NULL,
                recycledwaste INTEGER NOT NULL
            );
        """)
        cur.execute("SELECT COUNT(*) FROM wastedata")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO wastedata (city, year, wastecollected, recycledwaste) VALUES (?,?,?,?)",
                [
                    ("Amman", 2023, 12000, 3200),
                    ("Amman", 2024, 13500, 4100),
                    ("Zarqa",  2023,  6800, 1500),
                    ("Zarqa",  2024,  7200, 1700),
                    ("Irbid",  2023,  5400, 1100),
                    ("Irbid",  2024,  5900, 1300),
                ],
            )
            sl_conn.commit()

ensure_table()

# =========================================================
# Helpers
# =========================================================
# Strip ```sql fences if the model adds them
FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

BAD_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE|EXEC|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)

def extract_text(resp) -> str:
    if getattr(resp, "text", None):
        return (resp.text or "").strip()
    return (resp.candidates[0].content.parts[0].text or "").strip()

def sanitize_sql(sql: str) -> str:
    """
    Allow only SELECT against wastedata. Add LIMIT if missing.
    Prevent system tables.
    """
    s = FENCE_RE.sub("", sql).strip().rstrip(";")

    if BAD_SQL.search(s) or not s.upper().startswith("SELECT"):
        raise ValueError("Unsafe or invalid SQL.")

    # table must be wastedata, avoid system catalogs
    if "sqlite_master" in s.lower() or ("pg_catalog" in s.lower()):
        raise ValueError("Query tried to access system tables.")

    if "wastedata" not in s.lower():
        if " from " not in s.lower():
            s = "SELECT * FROM wastedata"
        else:
            raise ValueError("Query must target 'wastedata' table only.")

    # add limit if missing
    if not re.search(r"\bLIMIT\s+\d+\b", s, re.IGNORECASE):
        s += " LIMIT 100"
    return s

def make_text_filters_nocase(sql: str) -> str:
    """
    Make common text comparisons case-insensitive for city.
    - SQLite: add COLLATE NOCASE
    - Postgres: use ILIKE / LOWER(...)
    """
    s = sql
    if DB_KIND == "sqlite":
        # city = 'value' -> COLLATE NOCASE
        s = re.sub(
            r"(?i)\b(city)\s*=\s*('([^']*)')(?!\s+collate\s+nocase)",
            r"\1 = \2 COLLATE NOCASE",
            s,
        )
        # city LIKE 'value' -> ensure NOCASE
        s = re.sub(
            r"(?i)\b(city)\s+like\s+('([^']*)')(?!\s+collate\s+nocase)",
            r"\1 LIKE \2 COLLATE NOCASE",
            s,
        )
        # city IN (...) -> (city COLLATE NOCASE) IN (...)
        s = re.sub(r"(?i)\b(city)\s+in\s*\(", r"(city COLLATE NOCASE) IN (", s)
    else:
        # Postgres: = -> LOWER(city)=LOWER('..')
        s = re.sub(
            r"(?i)\bcity\s*=\s*'([^']*)'",
            lambda m: f"LOWER(city) = LOWER('{m.group(1)}')",
            s,
        )
        # LIKE -> ILIKE
        s = re.sub(r"(?i)\bcity\s+like\s+", "city ILIKE ", s)
        # IN ('a','b') -> LOWER(city) IN (LOWER('a'), LOWER('b'))
        def _lower_in(match):
            inside = match.group(1)
            parts = [p.strip() for p in inside.split(",")]
            lowered = ", ".join([f"LOWER({p})" for p in parts])
            return f"LOWER(city) IN ({lowered})"
        s = re.sub(r"(?is)\bcity\s+in\s*\(\s*(.*?)\s*\)", _lower_in, s)
    return s

def classify(user_prompt: str) -> dict:
    """
    Decide whether we need DB data; if yes, propose a single SELECT.
    Return JSON: { need_sql: bool, sql: str, reason: str }
    """
    dialect = "PostgreSQL" if DB_KIND == "postgres" else "SQLite"
    ci_hint = (
        "use ILIKE or LOWER(col)=LOWER(value) for text filters"
        if DB_KIND == "postgres"
        else "use COLLATE NOCASE or LOWER(col)=LOWER(value) for text filters"
    )

    system = f"""
You are an assistant for a {dialect}-backed app.
You may query a single table: wastedata(city TEXT, year INT, wastecollected INT, recycledwaste INT).

Return ONLY JSON with keys:
- need_sql: true|false
- sql: SELECT statement if needed, else ""
- reason: short string

Rules:
- Use {dialect} syntax.
- Only SELECT from 'wastedata'.
- For any TEXT comparisons (e.g., city), {ci_hint}.
- Never use system tables.
- No comments, no code fences.
""".strip()

    prompt = f"{system}\nUser: {user_prompt}\nJSON:"
    raw = extract_text(model.generate_content(prompt))
    try:
        start, end = raw.find("{"), raw.rfind("}")
        plan = json.loads(raw[start:end+1])
    except Exception:
        plan = {"need_sql": True, "sql": "", "reason": "fallback"}
    return plan

def analyze(user_prompt: str, rows: list, columns: list) -> str:
    packet = {
        "columns": columns,
        "rows": rows[:200],
        "row_count": len(rows),
        "derived": {"recycling_rate_note": "recycling rate = recycledwaste / wastecollected"},
    }
    aprompt = f"""
You are a data analyst. Using ONLY the provided rows from table wastedata, answer:

User question: {user_prompt}

Data JSON:
{json.dumps(packet)}

Write 4–8 sentences with: key comparisons, totals/averages, best/worst cities,
and % recycled where relevant (recycledwaste / wastecollected). If the data is
insufficient, say so briefly.
"""
    return extract_text(model.generate_content(aprompt))

def last_result():
    rows = session.get("last_rows") or []
    cols = session.get("last_cols") or []
    sql = session.get("last_sql") or ""
    return rows, cols, sql

def save_last(rows, cols, sql):
    session["last_rows"] = rows
    session["last_cols"] = cols
    session["last_sql"] = sql

def needs_followup_sql(user_prompt: str) -> bool:
    keywords = ["explain", "summarize", "that result", "those rows", "the rows", "previous result"]
    up = user_prompt.lower()
    return any(k in up for k in keywords)

# =========================================================
# Routes
# =========================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_prompt = request.json.get("message", "").strip()
        if not user_prompt:
            return jsonify({"ok": False, "message": "Please type a question."})

        rows, cols, sql_query, analysis_text = [], [], "", ""

        # Follow-up analysis of last result
        if needs_followup_sql(user_prompt):
            lr, lc, lsql = last_result()
            if lr and lc:
                analysis_text = analyze(user_prompt, lr, lc)
                return jsonify({
                    "ok": True,
                    "mode": "followup-analysis",
                    "columns": lc,
                    "rows": lr,
                    "analysis": analysis_text,
                    "ts": datetime.utcnow().isoformat(),
                })

        # Fresh plan
        plan = classify(user_prompt)
        if plan.get("need_sql", True):
            sql_query = plan.get("sql") or extract_text(
                model.generate_content(
                    f"Write ONE SELECT against wastedata for: {user_prompt}. No comments."
                )
            )
            sql_query = sanitize_sql(sql_query)
            sql_query = make_text_filters_nocase(sql_query)

            # Execute
            if DB_KIND == "postgres":
                cur.execute(sql_query)
                fetched = cur.fetchall()
                cols = [d.name for d in cur.description]
                rows = [dict(r) for r in fetched]
            else:
                cur.execute(sql_query)
                fetched = cur.fetchall()
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in fetched]

            # Save last result for follow-ups
            save_last(rows, cols, sql_query)

            # Analysis
            analysis_text = analyze(user_prompt, rows, cols)
            mode = "sql+analysis"
        else:
            # Direct chat (no SQL)
            analysis_text = extract_text(
                model.generate_content(
                    f"You are a helpful assistant for a waste-management app. Answer clearly and briefly.\nUser: {user_prompt}"
                )
            )
            mode = "direct"

        return jsonify({
            "ok": True,
            "mode": mode,
            "columns": cols,
            "rows": rows,
            "analysis": analysis_text,
            "ts": datetime.utcnow().isoformat()
        })

    except Exception as e:
        print("🔥 Error:", e)
        return jsonify({"ok": False, "message": str(e)}), 400

# =========================================================
# Entrypoint (Railway uses PORT; bind 0.0.0.0)
# =========================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = "0.0.0.0"
    print(f"✅ Server listening on http://{host}:{port} (DB={DB_KIND})")
    app.run(host=host, port=port, debug=bool(os.getenv("FLASK_DEBUG", "0") == "1"))
