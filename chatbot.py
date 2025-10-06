from flask import Flask, render_template, request, jsonify
import os
import re
import pyodbc
import google.generativeai as genai
from datetime import datetime

# ============================================================
#  SQL SERVER CONNECTION (Render + Azure SQL ready)
# ============================================================

DB_SERVER   = os.getenv("DB_SERVER")      # e.g. myserver.database.windows.net
DB_DATABASE = os.getenv("TestDB")    # e.g. TestDB
DB_USERNAME = os.getenv("DB_USERNAME")    # e.g. wastebot_user@myserver
DB_PASSWORD = os.getenv("DB_PASSWORD")    # your SQL password

conn_str = (
    "Driver={ODBC Driver 18 for SQL Server};"
    f"Server=tcp:{DB_SERVER},1433;"
    f"Database={DB_DATABASE};"
    f"Uid={DB_USERNAME};"
    f"Pwd={DB_PASSWORD};"
    "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
)

conn = pyodbc.connect(conn_str, autocommit=True)
cursor = conn.cursor()

# ============================================================
#  GEMINI CONFIG (from environment variables)
# ============================================================

GEMINI_API_KEY = os.getenv("AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

genai.configure(api_key="AIzaSyD60_pRh-tHnvSii1SSvG0DKDAe7r0dW0k")
model = genai.GenerativeModel("gemini-2.5-flash")

# ============================================================
#  FLASK APP
# ============================================================

app = Flask(__name__)

# ============================================================
#  HELPERS
# ============================================================

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
    s = FENCE_RE.sub("", sql).strip().rstrip(";")
    if BAD_SQL.search(s) or not s.upper().startswith("SELECT"):
        raise ValueError("Unsafe or non-SELECT SQL proposed by the model.")
    if re.match(r"^SELECT\s+TOP\s+\d+", s, re.IGNORECASE) or \
       re.match(r"^SELECT\s+DISTINCT\s+TOP\s+\d+", s, re.IGNORECASE):
        return s
    s = re.sub(r"^SELECT\s+DISTINCT\s+", "SELECT DISTINCT TOP 100 ", s, flags=re.IGNORECASE)
    if s.upper().startswith("SELECT ") and " TOP " not in s[:40].upper():
        s = s.replace("SELECT ", "SELECT TOP 100 ", 1)
    return s

# ============================================================
#  ROUTES
# ============================================================

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
You are a SQL Server expert. The ONLY table is dbo.WasteData with columns:
City NVARCHAR(100), Year INT, WasteCollected INT, RecycledWaste INT.

Rules:
- Write ONE valid T-SQL SELECT statement ONLY against dbo.WasteData.
- Qualify the table name as dbo.WasteData.
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
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        cols = [c[0] for c in cursor.description]
        results = [dict(zip(cols, row)) for row in rows]

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


# ============================================================
#  ENTRY POINT
# ============================================================

if __name__ == "__main__":
    print("✅ Flask server running on http://127.0.0.1:5000")
    app.run(debug=True)
