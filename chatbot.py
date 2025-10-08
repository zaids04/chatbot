from flask import Flask, render_template, request, jsonify, session
import os, re, json, sqlite3
from datetime import datetime

# --- LLM (Gemini) ---
import google.generativeai as genai
from dotenv import load_dotenv

# =========================================================
# Env & LLM setup
# =========================================================
load_dotenv()

genai.configure(api_key="AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
model = genai.GenerativeModel("gemini-2.5-flash")

# =========================================================
# Flask
# =========================================================
app = Flask(_name_)
app.secret_key = os.getenv("APP_SECRET_KEY", "dev-secret")  # for session memory

# =========================================================
# SQLite (local)
# =========================================================
DB_PATH = "local.db"

def connect_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

conn = connect_db()
cur = conn.cursor()

def ensure_table():
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
                ("Zarqa", 2023,  6800, 1500),
                ("Zarqa", 2024,  7200, 1700),
                ("Irbid", 2023,  5400, 1100),
                ("Irbid", 2024,  5900, 1300),
            ],
        )
        conn.commit()

ensure_table()

# =========================================================
# Helpers
# =========================================================
FENCE_RE = re.compile(r"^(?:sql)?\s*|\s*$", re.IGNORECASE | re.MULTILINE)
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
    Reject sqlite_master or other tables.
    """
    s = FENCE_RE.sub("", sql).strip().rstrip(";")

    if BAD_SQL.search(s) or not s.upper().startswith("SELECT"):
        raise ValueError("Unsafe or invalid SQL.")

    # Must reference wastedata and not sqlite_master
    if "sqlite_master" in s.lower():
        raise ValueError("Query tried to access system tables.")
    if "wastedata" not in s.lower():
        # If model forgot, force table name:
        # naive fix: append FROM wastedata if no FROM is present
        if " from " not in s.lower():
            s = f"SELECT * FROM wastedata"
        else:
            raise ValueError("Query must target 'wastedata' table only.")

    if not re.search(r"\bLIMIT\s+\d+\b", s, re.IGNORECASE):
        s += " LIMIT 100"
    return s

# --------- NEW: make text comparisons case-insensitive (Option B) ---------
def make_text_filters_nocase(sql: str) -> str:
    """
    Post-process common patterns to ensure case-insensitive comparisons on text fields.
    We explicitly handle 'city' = ..., LIKE ..., and IN (...).
    """
    s = sql

    # city = 'value'  ->  city = 'value' COLLATE NOCASE   (if not already collated)
    s = re.sub(
        r"(?i)\b(city)\s*=\s*('([^']*)')(?!\s+collate\s+nocase)",
        r"\1 = \2 COLLATE NOCASE",
        s,
    )

    # city LIKE 'value'  -> ensure NOCASE
    s = re.sub(
        r"(?i)\b(city)\s+like\s+('([^']*)')(?!\s+collate\s+nocase)",
        r"\1 LIKE \2 COLLATE NOCASE",
        s,
    )

    # city IN ('a','b')  ->  (city COLLATE NOCASE) IN ('a','b')
    s = re.sub(
        r"(?i)\b(city)\s+in\s*\(",
        r"(city COLLATE NOCASE) IN (",
        s,
    )

    return s
# -------------------------------------------------------------------------

def classify(user_prompt: str) -> dict:
    """
    Decide whether we need DB data; if yes, propose a single SELECT.
    Return JSON: { need_sql: bool, sql: str, reason: str }
    """
    system = """
You are an assistant for a SQLite-backed app.
You may query a single table: wastedata(city TEXT, year INT, wastecollected INT, recycledwaste INT).

Return ONLY JSON with keys:
- need_sql: true|false
- sql: SELECT statement if needed, else ""
- reason: short string

Rules:
- Use SQLite syntax.
- Only SELECT from 'wastedata'.
- For any TEXT comparisons (e.g., city), make them case-insensitive using either
  COLLATE NOCASE (preferred) or LOWER(col) = LOWER(value).
- Never use sqlite_master or any other table.
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
    """
    Ask the LLM to summarize/interpret the result set.
    """
    packet = {
        "columns": columns,
        "rows": rows[:200],
        "row_count": len(rows),
        "derived": {
        "recycling_rate_note": "recycling rate = recycledwaste / wastecollected"
        }
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
    """Fetch last results from session memory (for 'explain the rows')."""
    rows = session.get("last_rows") or []
    cols = session.get("last_cols") or []
    sql = session.get("last_sql") or ""
    return rows, cols, sql

def save_last(rows, cols, sql):
    session["last_rows"] = rows
    session["last_cols"] = cols
    session["last_sql"] = sql

def needs_followup_sql(user_prompt: str) -> bool:
    """Heuristics: user refers to previous rows."""
    keywords = ["explain", "summarize", "that result", "those rows", "the rows", "previous result"]
    up = user_prompt.lower()
    return any(k in up for k in keywords)

# =========================================================
# Routes
# =========================================================
@app.route("/")
def index():
    return render_template("index.html")  # your existing UI works; show analysis on response

@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_prompt = request.json.get("message", "").strip()
        if not user_prompt:
            return jsonify({"ok": False, "message": "Please type a question."})

        rows, cols, sql_query, analysis_text = [], [], "", ""

        # If user asks to "explain/summarize the rows", use last result if available.
        if needs_followup_sql(user_prompt):
            lr, lc, lsql = last_result()
            if lr and lc:
                analysis_text = analyze(user_prompt, lr, lc)
                return jsonify({
                    "ok": True,
                    "mode": "followup-analysis",
                    "sql": lsql,
                    "columns": lc,
                    "rows": lr,
                    "analysis": analysis_text,
                    "ts": datetime.utcnow().isoformat(),
                })

        # Otherwise plan fresh
        plan = classify(user_prompt)
        if plan.get("need_sql", True):
            sql_query = plan.get("sql") or extract_text(
                model.generate_content(
                    f"Write ONE SQLite SELECT against wastedata for: {user_prompt}. No comments."
                )
            )
            sql_query = sanitize_sql(sql_query)
            # --------- NEW: enforce case-insensitive filters on text fields ---------
            sql_query = make_text_filters_nocase(sql_query)
            # -----------------------------------------------------------------------

            # Execute
            cur.execute(sql_query)
            fetched = cur.fetchall()
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in fetched]

            # Save last result for follow-ups
            save_last(rows, cols, sql_query)

            # Analyze
            analysis_text = analyze(user_prompt, rows, cols)
            mode = "sql+analysis"
        else:
            # General chat—no SQL
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

if _name_ == "_main_":
    print("✅ Local server: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)