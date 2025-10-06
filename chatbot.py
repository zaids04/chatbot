from flask import Flask, render_template, request, jsonify
import os, re
import psycopg2
import psycopg2.extras
import google.generativeai as genai
from datetime import datetime

# ===================== Postgres connection (Railway) =====================
DATABASE_URL = os.getenv("DATABASE_URL")  # e.g. postgres://user:pass@host:port/dbname
if not DATABASE_URL:
    raise SystemExit("Missing DATABASE_URL environment variable.")

# sslmode=require is common on Railway; if DATABASE_URL already has it, fine.
conn = psycopg2.connect(DATABASE_URL, sslmode=os.getenv("PG_SSLMODE", "require"))
conn.autocommit = True
cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

# Create table + seed once (idempotent)
def ensure_table():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.wastedata (
            city            TEXT NOT NULL,
            year            INT  NOT NULL,
            wastecollected  INT  NOT NULL,
            recycledwaste   INT  NOT NULL
        );
    """)
    # Seed a few rows if empty
    cursor.execute("SELECT COUNT(*) AS n FROM public.wastedata;")
    n = cursor.fetchone()["n"]
    if n == 0:
        cursor.execute("""
            INSERT INTO public.wastedata (city, year, wastecollected, recycledwaste) VALUES
            ('Amman', 2023, 12000, 3200),
            ('Amman', 2024, 13500, 4100),
            ('Zarqa', 2023,  6800, 1500),
            ('Zarqa', 2024,  7200, 1700),
            ('Irbid', 2023,  5400, 1100),
            ('Irbid', 2024,  5900, 1300);
        """)
ensure_table()

# ===================== Gemini config =====================
GEMINI_API_KEY = os.getenv("AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
if not GEMINI_API_KEY:
    raise SystemExit("Missing GEMINI_API_KEY environment variable.")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

genai.configure(api_key="AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
model = genai.GenerativeModel("gemini-2.5-flash")

# ===================== Flask =====================
app = Flask(__name__)

# ===================== Helpers =====================
FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
BAD_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE|EXEC|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)

def extract_text(resp) -> str:
    if getattr(resp, "text", None):
        txt = resp.text
    else:
        txt = resp.candidates[0].content.parts[0].text
    return (txt or "").strip()

def sanitize_sql(sql: str) -> str:
    """
    Postgres-safe:
      - must start with SELECT
      - strip code fences
      - append LIMIT 100 if none present (and not already limited)
    """
    s = FENCE_RE.sub("", sql).strip().rstrip(";")
    if BAD_SQL.search(s) or not s.upper().startswith("SELECT"):
        raise ValueError("Unsafe or non-SELECT SQL proposed by the model.")

    # If the query already has LIMIT, leave it; otherwise add LIMIT 100
    if re.search(r"\bLIMIT\s+\d+\b", s, re.IGNORECASE):
        return s
    return s + " LIMIT 100"

# ===================== Routes =====================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_prompt = request.json.get("message", "").strip()
        if not user_prompt:
            return jsonify({"ok": False, "message": "Please type a question.", "sql": None})

        prompt = f"""
You are a PostgreSQL expert. The ONLY table is public.wastedata with columns:
city TEXT, year INT, wastecollected INT, recycledwaste INT.

Rules:
- Write ONE valid SQL SELECT statement ONLY against public.wastedata.
- Qualify the table name as public.wastedata.
- Use PostgreSQL syntax (e.g., LIMIT, ILIKE).
- Do not add explanations, comments, or code fences.
- Never write DDL or DML.
- If the request cannot be answered from these columns, return exactly:
SELECT 'CANNOT_ANSWER' AS msg;
User: {user_prompt}
""".strip()

        resp = model.generate_content(prompt)
        sql_query = extract_text(resp)
        if sql_query.startswith("```"):
            sql_query = FENCE_RE.sub("", sql_query).strip()

        if not sql_query or "CANNOT_ANSWER" in sql_query.upper():
            return jsonify({"ok": False, "message": "Gemini could not generate a valid query.", "sql": sql_query, "ts": datetime.utcnow().isoformat()})

        sql_query = sanitize_sql(sql_query)

        # Execute
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        cols = [desc.name for desc in cursor.description]
        results = [dict(r) for r in rows]

        return jsonify({
            "ok": True,
            "rows": results,
            "columns": cols,
            "sql": sql_query,
            "ts": datetime.utcnow().isoformat()
        })

    except Exception as e:
        print("🔥 Error:", e)
        return jsonify({
            "ok": False,
            "message": f"❌ Could not process your request. {e}",
            "sql": None,
            "ts": datetime.utcnow().isoformat()
        }), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
