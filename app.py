"""
app.py — Gravel Prospect App (multi-user, SQLite-backed)

Run:  python3 -B app.py
Open: http://localhost:5001
"""

import os, json, sqlite3, hashlib, secrets, threading, yaml
from datetime import datetime
from pathlib import Path
from functools import wraps

import pandas as pd
from flask import (Flask, render_template_string, jsonify, request,
                   redirect, url_for, session, g)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gravel-dev-secret-change-in-prod")

DB_PATH  = os.path.join(os.path.dirname(__file__), "data", "gravel.db")
RAW_PATH = os.path.join(os.path.dirname(__file__), "data", "raw_accounts.json")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        # WAL mode is set once at init_db(); skip re-setting it here to avoid
        # blocking on write-lock while migrate.py is running.
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    Path("data").mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            email         TEXT UNIQUE NOT NULL,
            name          TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS prospects (
            id               INTEGER PRIMARY KEY,
            username         TEXT UNIQUE NOT NULL,
            full_name        TEXT DEFAULT '',
            bio              TEXT DEFAULT '',
            followers        INTEGER DEFAULT 0,
            following        INTEGER DEFAULT 0,
            posts_count      INTEGER DEFAULT 0,
            profile_url      TEXT DEFAULT '',
            source_brands_str TEXT DEFAULT '',
            score            REAL DEFAULT 0,
            ml_score         REAL DEFAULT 0,
            recent_images    TEXT DEFAULT '[]',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ratings (
            id          INTEGER PRIMARY KEY,
            prospect_id INTEGER REFERENCES prospects(id),
            user_id     INTEGER REFERENCES users(id),
            rating      INTEGER,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(prospect_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS prospect_meta (
            prospect_id         INTEGER PRIMARY KEY REFERENCES prospects(id),
            notes               TEXT DEFAULT '',
            outreach_status     TEXT DEFAULT '',
            address_collected   TEXT DEFAULT '',
            product_sent        TEXT DEFAULT '',
            posted_about_gravel TEXT DEFAULT '',
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS invitations (
            id          INTEGER PRIMARY KEY,
            email       TEXT NOT NULL,
            token       TEXT UNIQUE NOT NULL,
            invited_by  INTEGER REFERENCES users(id),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP
        );
    """)
    # Migrate old ratings table if it has the old schema (prospect_id UNIQUE only)
    cols = [r[1] for r in db.execute("PRAGMA table_info(ratings)").fetchall()]
    if "user_id" not in cols:
        print("→ Migrating ratings table to per-user schema…")
        db.executescript("""
            ALTER TABLE ratings RENAME TO ratings_old;
            CREATE TABLE ratings (
                id          INTEGER PRIMARY KEY,
                prospect_id INTEGER REFERENCES prospects(id),
                user_id     INTEGER REFERENCES users(id),
                rating      INTEGER,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(prospect_id, user_id)
            );
            INSERT INTO ratings (prospect_id, user_id, rating, updated_at)
            SELECT prospect_id,
                   COALESCE(rated_by, (SELECT id FROM users ORDER BY id LIMIT 1)),
                   rating, updated_at
            FROM ratings_old
            WHERE rating IS NOT NULL;
            INSERT OR IGNORE INTO prospect_meta
                (prospect_id, notes, outreach_status, address_collected,
                 product_sent, posted_about_gravel, updated_at)
            SELECT prospect_id, notes, outreach_status, address_collected,
                   product_sent, posted_about_gravel, updated_at
            FROM ratings_old;
            DROP TABLE ratings_old;
        """)
        print("✓ Migration done.")
    db.commit()
    db.close()


# ── Auth helpers ───────────────────────────────────────────────────────────────

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # If session exists but user was wiped (e.g. DB reset), clear and re-login
        if current_user() is None:
            session.clear()
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapped

def current_user():
    if "user_id" not in session: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", [session["user_id"]]).fetchone()


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        db    = get_db()
        user  = db.execute("SELECT * FROM users WHERE email=?", [email]).fetchone()
        if user and user["password_hash"] == hash_pw(pw):
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        error = "Invalid email or password."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/register", methods=["GET","POST"])
def register():
    # Only allow if no users exist yet (first-run setup)
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    error = ""
    if request.method == "POST":
        name  = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        pw2   = request.form.get("password2","")
        if pw != pw2:
            error = "Passwords don't match."
        elif len(pw) < 6:
            error = "Password must be at least 6 characters."
        else:
            try:
                db.execute("INSERT INTO users (name,email,password_hash) VALUES (?,?,?)",
                           [name, email, hash_pw(pw)])
                db.commit()
                user = db.execute("SELECT * FROM users WHERE email=?", [email]).fetchone()
                session["user_id"] = user["id"]
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                error = "That email is already registered."
    return render_template_string(REGISTER_HTML, error=error, first_run=(count==0))


# ── Main app routes ────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    tab = request.args.get("tab", "discover")
    resp = app.make_response(render_template_string(APP_HTML, tab=tab, user=current_user()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/prospects")
@login_required
def api_prospects():
    db = get_db()
    # Only return prospects the current user hasn't rated yet
    rows = db.execute("""
        SELECT p.*
        FROM prospects p
        WHERE NOT EXISTS (
            SELECT 1 FROM ratings r
            WHERE r.prospect_id = p.id AND r.user_id = ?
        )
        ORDER BY p.score DESC
    """, [session["user_id"]]).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["recent_images"] = json.loads(d.get("recent_images") or "[]")
        d["rating"] = None
        result.append(d)
    return jsonify(result)

@app.route("/api/reviewed")
@login_required
def api_reviewed():
    try:
        db = get_db()
        users = db.execute("SELECT id, name, email FROM users ORDER BY created_at").fetchall()
        rows  = db.execute("""
            SELECT p.username, p.full_name, p.followers, p.profile_url,
                   p.source_brands_str, p.score, p.bio,
                   m.notes, m.outreach_status, m.address_collected,
                   m.product_sent, m.posted_about_gravel,
                   r.user_id, r.rating
            FROM ratings r
            JOIN prospects p ON p.id = r.prospect_id
            LEFT JOIN prospect_meta m ON m.prospect_id = r.prospect_id
            WHERE r.rating IS NOT NULL AND r.rating > 0
            ORDER BY p.username
        """).fetchall()

        # Pivot: one row per prospect, rating columns per user
        # Use string keys so JSON serialises cleanly
        prospects = {}
        for row in rows:
            u = row["username"]
            if u not in prospects:
                prospects[u] = {
                    "username": u,
                    "full_name": row["full_name"] or "",
                    "followers": row["followers"] or 0,
                    "profile_url": row["profile_url"] or "",
                    "source_brands_str": row["source_brands_str"] or "",
                    "score": float(row["score"] or 0),
                    "notes": row["notes"] or "",
                    "outreach_status": row["outreach_status"] or "",
                    "ratings": {}
                }
            # Force string key so JSON round-trip is clean
            prospects[u]["ratings"][str(row["user_id"])] = row["rating"]

        # Compute average and sort by it
        result = []
        for p in prospects.values():
            vals = [v for v in p["ratings"].values() if v]
            p["avg_rating"] = round(sum(vals)/len(vals), 1) if vals else 0
            result.append(p)
        result.sort(key=lambda x: x["avg_rating"], reverse=True)

        return jsonify({"users": [dict(u) for u in users], "prospects": result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "users": [], "prospects": []}), 500

@app.route("/api/rate", methods=["POST"])
@login_required
def api_rate():
    data = request.json
    username = data.get("username")
    rating   = data.get("rating")
    db = get_db()
    prospect = db.execute("SELECT id FROM prospects WHERE username=?", [username]).fetchone()
    if not prospect:
        return jsonify({"ok": False, "error": "unknown prospect"}), 404
    db.execute("""
        INSERT INTO ratings (prospect_id, user_id, rating, updated_at)
        VALUES (?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(prospect_id, user_id) DO UPDATE SET
          rating=excluded.rating, updated_at=CURRENT_TIMESTAMP
    """, [prospect["id"], session["user_id"], rating])
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/update_row", methods=["POST"])
@login_required
def api_update_row():
    """Update outreach fields from the Reviewed tab."""
    data     = request.json
    username = data.get("username")
    field    = data.get("field")
    value    = data.get("value")
    rating_fields = {"rating"}
    meta_fields   = {"notes","outreach_status","address_collected","product_sent","posted_about_gravel"}
    if field not in rating_fields | meta_fields:
        return jsonify({"ok": False}), 400
    db = get_db()
    prospect = db.execute("SELECT id FROM prospects WHERE username=?", [username]).fetchone()
    if not prospect:
        return jsonify({"ok": False}), 404
    if field in rating_fields:
        db.execute(f"""
            INSERT INTO ratings (prospect_id, user_id, {field}, updated_at)
            VALUES (?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(prospect_id, user_id) DO UPDATE SET
              {field}=excluded.{field}, updated_at=CURRENT_TIMESTAMP
        """, [prospect["id"], session["user_id"], value])
    else:
        db.execute(f"""
            INSERT INTO prospect_meta (prospect_id, {field}, updated_at)
            VALUES (?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(prospect_id) DO UPDATE SET
              {field}=excluded.{field}, updated_at=CURRENT_TIMESTAMP
        """, [prospect["id"], value])
    db.commit()
    return jsonify({"ok": True})

scrape_status = {"running": False, "stage": "", "pct": 0, "error": ""}

@app.route("/api/scrape", methods=["POST"])
@login_required
def api_scrape():
    base = os.path.dirname(__file__)

    def run_pipeline():
        scrape_status.update({"running": True, "pct": 0, "stage": "Starting…", "error": ""})
        try:
            import sys
            sys.path.insert(0, base)
            from scrape import run as run_scrape

            def on_progress(stage, pct):
                scrape_status.update({"stage": stage, "pct": pct})

            run_scrape(status_callback=on_progress)
            scrape_status.update({"running": False, "stage": "Done! New creators are ready.", "pct": 100})
            print("✅ Pipeline complete.")
        except Exception as e:
            import traceback; traceback.print_exc()
            scrape_status.update({"running": False, "stage": "Error", "pct": 0, "error": str(e)})
            print(f"❌ Pipeline error: {e}")

    if scrape_status["running"]:
        return jsonify({"ok": False, "error": "Already running"})
    threading.Thread(target=run_pipeline, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/scrape/status")
@login_required
def api_scrape_status():
    return jsonify(scrape_status)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

@app.route("/api/config", methods=["GET"])
@login_required
def api_config_get():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    default_statuses = ["contacted","interested","not interested","address requested","shipped","posted"]
    return jsonify({
        "target_brands":       cfg.get("target_brands", []),
        "min_followers":       cfg.get("min_followers", 300),
        "max_followers":       cfg.get("max_followers", 999999),
        "posts_per_brand":     cfg.get("posts_per_brand", 500),
        "min_engagement_rate": cfg.get("min_engagement_rate", 0.01),
        "outreach_statuses":   cfg.get("outreach_statuses", default_statuses),
    })

@app.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    data = request.json
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["target_brands"]       = data.get("target_brands", cfg.get("target_brands", []))
    cfg["min_followers"]       = int(data.get("min_followers", cfg.get("min_followers", 300)))
    cfg["max_followers"]       = int(data.get("max_followers", cfg.get("max_followers", 999999)))
    cfg["posts_per_brand"]     = int(data.get("posts_per_brand", cfg.get("posts_per_brand", 500)))
    cfg["min_engagement_rate"] = float(data.get("min_engagement_rate", cfg.get("min_engagement_rate", 0.01)))
    cfg["outreach_statuses"]   = data.get("outreach_statuses", cfg.get("outreach_statuses", []))
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    return jsonify({"ok": True})

@app.route("/api/users", methods=["GET"])
@login_required
def api_users():
    db = get_db()
    users = db.execute("SELECT id, name, email, created_at FROM users ORDER BY created_at").fetchall()
    return jsonify([dict(u) for u in users])

@app.route("/api/invite_user", methods=["POST"])
@login_required
def api_invite_user():
    data  = request.json
    email = data.get("email","").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "Email required"}), 400
    db = get_db()
    # Don't invite someone already on the team
    existing = db.execute("SELECT id FROM users WHERE email=?", [email]).fetchone()
    if existing:
        return jsonify({"ok": False, "error": "That email is already on the team"}), 400
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO invitations (email, token, invited_by) VALUES (?,?,?)",
               [email, token, session["user_id"]])
    db.commit()
    base = request.host_url.rstrip("/")
    invite_url = f"{base}/invite/{token}"
    return jsonify({"ok": True, "invite_url": invite_url})

@app.route("/invite/<token>", methods=["GET","POST"])
def accept_invite(token):
    db = get_db()
    invite = db.execute(
        "SELECT * FROM invitations WHERE token=? AND accepted_at IS NULL", [token]
    ).fetchone()
    if not invite:
        return render_template_string(INVITE_INVALID_HTML)
    if request.method == "POST":
        name = request.form.get("name","").strip()
        pw   = request.form.get("password","")
        pw2  = request.form.get("password2","")
        error = None
        if not pw or len(pw) < 6:
            error = "Password must be at least 6 characters."
        elif pw != pw2:
            error = "Passwords don't match."
        if not error:
            try:
                db.execute("INSERT INTO users (name,email,password_hash) VALUES (?,?,?)",
                           [name, invite["email"], hash_pw(pw)])
                db.execute("UPDATE invitations SET accepted_at=CURRENT_TIMESTAMP WHERE token=?", [token])
                db.commit()
                user = db.execute("SELECT * FROM users WHERE email=?", [invite["email"]]).fetchone()
                session["user_id"] = user["id"]
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                error = "That email is already registered."
        return render_template_string(INVITE_HTML, email=invite["email"], error=error)
    return render_template_string(INVITE_HTML, email=invite["email"], error=None)

@app.route("/api/remove_user", methods=["POST"])
@login_required
def api_remove_user():
    data = request.json
    user_id = data.get("id")
    if user_id == session["user_id"]:
        return jsonify({"ok": False, "error": "You can't remove yourself"}), 400
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", [user_id])
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/import-db", methods=["GET","POST"])
def admin_import_db():
    """One-time endpoint to upload local DB to Railway. Protected by IMPORT_SECRET env var."""
    secret = os.environ.get("IMPORT_SECRET", "")
    if not secret:
        return "IMPORT_SECRET not set", 403
    if request.method == "GET":
        return f'''<html><body style="font-family:sans-serif;padding:40px">
            <h2>Upload Database</h2>
            <form method="post" enctype="multipart/form-data">
                <input name="secret" type="password" placeholder="Secret" style="padding:8px;margin-bottom:12px;display:block;width:300px">
                <input name="db" type="file" accept=".db" style="margin-bottom:12px;display:block">
                <button type="submit" style="padding:10px 24px;background:#111;color:white;border:none;border-radius:6px;cursor:pointer">Upload</button>
            </form></body></html>'''
    if request.form.get("secret") != secret:
        return "Wrong secret", 403
    f = request.files.get("db")
    if not f:
        return "No file", 400
    Path("data").mkdir(exist_ok=True)
    f.save(DB_PATH)
    return "<h2 style='font-family:sans-serif;padding:40px'>✅ Database uploaded! <a href='/'>Go to app</a></h2>"


# ── HTML Templates ─────────────────────────────────────────────────────────────

INVITE_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Join the team</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f5f5f5;display:flex;align-items:center;justify-content:center;
     min-height:100vh;padding:24px}
.card{background:white;border-radius:16px;padding:40px;width:100%;max-width:400px;
      box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:22px;font-weight:700;margin-bottom:6px}
.sub{font-size:14px;color:#888;margin-bottom:28px}
.email-badge{background:#f0f4ff;color:#3355aa;font-size:13px;font-weight:600;
             padding:6px 12px;border-radius:6px;display:inline-block;margin-bottom:24px}
label{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;
      letter-spacing:.4px;display:block;margin-bottom:4px}
input{width:100%;padding:11px 14px;border:1.5px solid #e0e0e0;border-radius:8px;
      font-size:15px;outline:none;margin-bottom:14px}
input:focus{border-color:#111}
.btn{width:100%;padding:12px;background:#111;color:white;border:none;
     border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-top:4px}
.btn:hover{background:#333}
.error{color:#dc2626;font-size:13px;margin-bottom:14px}
</style></head>
<body><div class="card">
  <h1>You're invited 🎉</h1>
  <p class="sub">Set up your account to get started.</p>
  <div class="email-badge">{{ email }}</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <label>Your name</label>
    <input name="name" type="text" placeholder="First Last" autocomplete="name">
    <label>Password</label>
    <input name="password" type="password" placeholder="At least 6 characters" autocomplete="new-password">
    <label>Confirm password</label>
    <input name="password2" type="password" placeholder="Repeat password" autocomplete="new-password">
    <button class="btn" type="submit">Create account &amp; sign in</button>
  </form>
</div></body></html>
"""

INVITE_INVALID_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Invalid invite</title>
<style>
body{font-family:-apple-system,sans-serif;display:flex;align-items:center;
     justify-content:center;min-height:100vh;background:#f5f5f5}
.card{background:white;border-radius:16px;padding:40px;text-align:center;
      max-width:380px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:20px;margin-bottom:10px}
p{color:#888;font-size:14px;margin-bottom:20px}
a{color:#111;font-weight:600}
</style></head>
<body><div class="card">
  <h1>Invite not found</h1>
  <p>This invite link has already been used or is invalid.</p>
  <a href="/login">← Go to login</a>
</div></body></html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Product Seeding — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:white;border-radius:16px;padding:40px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.1)}
h1{font-size:22px;font-weight:700;margin-bottom:6px}
.sub{font-size:14px;color:#888;margin-bottom:28px}
label{font-size:13px;font-weight:600;color:#444;display:block;margin-bottom:4px}
input{width:100%;padding:10px 14px;border:1.5px solid #e0e0e0;border-radius:8px;
      font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#111}
button{width:100%;padding:12px;background:#111;color:white;border:none;
       border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-top:4px}
button:hover{background:#333}
.error{background:#fee;color:#c00;padding:10px 14px;border-radius:8px;
       font-size:13px;margin-bottom:16px}
.link{text-align:center;margin-top:16px;font-size:13px;color:#888}
.link a{color:#0066cc;text-decoration:none}
</style></head><body>
<div class="card">
  <img src="/static/logo.png" alt="Product Seeding" style="height:32px;width:auto;display:block;margin-bottom:20px">
  <p class="sub">Sign in to your account</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" required autofocus>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" required>
    <button type="submit">Sign in</button>
  </form>
  <div class="link"><a href="/register">Create account</a></div>
</div>
</body></html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Product Seeding — Create Account</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:white;border-radius:16px;padding:40px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.1)}
h1{font-size:22px;font-weight:700;margin-bottom:6px}
.sub{font-size:14px;color:#888;margin-bottom:28px}
label{font-size:13px;font-weight:600;color:#444;display:block;margin-bottom:4px}
input{width:100%;padding:10px 14px;border:1.5px solid #e0e0e0;border-radius:8px;
      font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#111}
button{width:100%;padding:12px;background:#111;color:white;border:none;
       border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-top:4px}
button:hover{background:#333}
.error{background:#fee;color:#c00;padding:10px 14px;border-radius:8px;
       font-size:13px;margin-bottom:16px}
.link{text-align:center;margin-top:16px;font-size:13px;color:#888}
.link a{color:#0066cc;text-decoration:none}
</style></head><body>
<div class="card">
  <img src="/static/logo.png" alt="Product Seeding" style="height:32px;width:auto;display:block;margin-bottom:20px">
  <p class="sub">{% if first_run %}Set up your workspace{% else %}Create your account{% endif %}</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>Name</label>
    <input type="text" name="name" placeholder="Your name" required autofocus>
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" required>
    <label>Confirm Password</label>
    <input type="password" name="password2" placeholder="••••••••" required>
    <button type="submit">Create account</button>
  </form>
  <div class="link"><a href="/login">Back to sign in</a></div>
</div>
</body></html>"""

APP_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Product Seeding</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;min-height:100vh}

/* Header */
#header{position:fixed;top:0;left:0;right:0;height:56px;background:white;
        border-bottom:1px solid #e8e8e8;display:flex;align-items:center;
        padding:0 24px;gap:24px;z-index:200}
#header .logo{height:28px;width:auto;margin-right:8px;display:block}
.tab-btn{padding:6px 16px;border-radius:20px;border:none;background:none;
         font-size:14px;font-weight:500;cursor:pointer;color:#888}
.tab-btn.active{background:#111;color:white}
.spacer{flex:1}
.user-badge{font-size:13px;color:#888}
.logout-btn{font-size:13px;color:#0066cc;text-decoration:none;margin-left:16px}

/* ── DISCOVER TAB ─────────────────────────────────────────── */
#discover{display:none;flex-direction:column;align-items:center;
          justify-content:center;min-height:100vh;padding-top:56px}
#card-container{position:relative;width:380px;margin-top:24px}
.card{position:relative;width:100%;background:white;border-radius:16px;
      box-shadow:0 4px 20px rgba(0,0,0,.12);padding:24px;display:flex;
      flex-direction:column;cursor:grab;user-select:none;transition:transform .1s}
.card.dragging{cursor:grabbing;transition:none}
.card.fly-left{animation:flyLeft .3s ease forwards}
.card.fly-right{animation:flyRight .3s ease forwards}
@keyframes flyLeft{to{transform:translateX(-150%) rotate(-20deg);opacity:0}}
@keyframes flyRight{to{transform:translateX(150%) rotate(20deg);opacity:0}}
.card-score{position:absolute;top:16px;right:16px;background:#111;color:white;
            font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px}
.card-avatar{width:52px;height:52px;border-radius:50%;
             background:linear-gradient(135deg,#667eea,#764ba2);
             display:flex;align-items:center;justify-content:center;
             font-size:20px;color:white;font-weight:700;margin-bottom:12px;flex-shrink:0}
.card-name{font-size:20px;font-weight:700;color:#111;margin-bottom:2px}
.card-handle a{font-size:13px;color:#0066cc;text-decoration:none}
.card-handle a:hover{text-decoration:underline}
.card-handle{margin-bottom:10px}
.card-stats{display:flex;gap:16px;margin-bottom:10px}
.stat-value{font-size:15px;font-weight:700;color:#111}
.stat-label{font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.5px}
.card-bio{font-size:13px;color:#444;line-height:1.5;overflow:hidden;
          display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;margin-bottom:8px}
.card-brands{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.brand-tag{background:#f0f4ff;color:#3355aa;font-size:11px;padding:3px 8px;
           border-radius:20px;font-weight:500}
.image-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;
            border-radius:8px;overflow:hidden;margin-top:4px}
.image-grid img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.image-grid-placeholder{background:#f0f0f0;aspect-ratio:1;display:flex;
                         align-items:center;justify-content:center;color:#ccc;font-size:18px}
.overlay-label{position:absolute;top:24px;font-size:28px;font-weight:900;
               padding:5px 14px;border-radius:8px;border:4px solid;
               opacity:0;pointer-events:none;transition:opacity .1s}
.overlay-yes{left:20px;color:#22c55e;border-color:#22c55e;transform:rotate(-15deg)}
.overlay-no{right:20px;color:#ef4444;border-color:#ef4444;transform:rotate(15deg)}
#buttons{display:flex;gap:10px;margin-top:20px;align-items:center}
.btn{width:56px;height:56px;border-radius:50%;border:none;font-size:22px;cursor:pointer;
     background:white;box-shadow:0 2px 10px rgba(0,0,0,.15);transition:transform .1s,box-shadow .1s}
.btn:hover{transform:scale(1.1);box-shadow:0 4px 16px rgba(0,0,0,.2)}
.btn-divider{width:1px;height:36px;background:#e0e0e0;margin:0 6px}
#rating-bar{display:none}
.rating-btn{width:42px;height:42px;border-radius:50%;border:2px solid #ddd;background:white;
            font-size:15px;font-weight:700;cursor:pointer;color:#888;transition:all .15s}
.rating-btn:hover{border-color:#111;color:#111;transform:scale(1.1)}
#counter{font-size:13px;color:#888;margin-top:10px}
#hint{margin-top:8px;font-size:11px;color:#bbb}
#done-screen{text-align:center;display:none}
#done-screen h2{font-size:26px;margin-bottom:8px}
#done-screen p{color:#888;font-size:14px}
#start-screen{display:flex;align-items:center;justify-content:center;padding-top:56px;min-height:100vh}
#start-card{background:white;border-radius:20px;box-shadow:0 4px 24px rgba(0,0,0,.1);
            padding:48px 40px;text-align:center;width:340px}
#start-icon{font-size:48px;margin-bottom:16px}
#start-card h2{font-size:22px;font-weight:700;margin-bottom:8px}
#start-sub{font-size:14px;color:#888;margin-bottom:28px}
#start-btn{padding:14px 32px;background:#111;color:white;border:none;border-radius:10px;
           font-size:16px;font-weight:600;cursor:pointer;transition:background .15s;width:100%}
#start-btn:hover:not(:disabled){background:#333}
#start-btn:disabled{background:#ccc;cursor:default}

/* ── REVIEWED TAB ─────────────────────────────────────────── */
#reviewed{display:none;padding:80px 24px 40px}
.reviewed-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.reviewed-header h2{font-size:20px;font-weight:700}
.avg-rating-cell .avg-tooltip{
  display:none;position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);
  background:#222;border-radius:8px;padding:8px 12px;white-space:nowrap;
  font-size:12px;line-height:1.8;z-index:100;
  box-shadow:0 4px 12px rgba(0,0,0,.3);pointer-events:none}
.avg-rating-cell:hover .avg-tooltip{display:block}
.reviewed-meta{font-size:13px;color:#888}
.table-wrap{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.07);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:10px 14px;text-align:left;font-size:11px;color:#888;text-transform:uppercase;
   letter-spacing:.5px;border-bottom:1px solid #f0f0f0;background:#fafafa;cursor:pointer;
   user-select:none;white-space:nowrap}
th:hover{color:#111}
td{padding:10px 14px;border-bottom:1px solid #f8f8f8;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.t-name{font-weight:600;color:#111}
.t-handle{color:#0066cc;text-decoration:none;font-size:12px}
.t-handle:hover{text-decoration:underline}
.t-followers{font-weight:600}
.rating-stars{display:flex;gap:2px}
.star{font-size:14px;cursor:pointer;color:#ddd}
.star.on{color:#f59e0b}
.t-status select,.t-notes input{border:1px solid #e8e8e8;border-radius:6px;
  padding:4px 8px;font-size:12px;background:white;color:#444;width:100%}
.t-status select:focus,.t-notes input:focus{outline:none;border-color:#aaa}
.t-badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500}
.badge-yes{background:#dcfce7;color:#16a34a}
.badge-no{background:#fee2e2;color:#dc2626}
.badge-sent{background:#dbeafe;color:#2563eb}
.badge-empty{background:#f3f4f6;color:#9ca3af}
.filter-bar{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.filter-bar input{padding:8px 14px;border:1.5px solid #e8e8e8;border-radius:8px;
                   font-size:13px;width:240px;outline:none}
.filter-bar input:focus{border-color:#111}
.filter-btn{padding:7px 14px;border-radius:20px;border:1.5px solid #e0e0e0;
            background:white;font-size:12px;cursor:pointer;color:#555}
.filter-btn.active{background:#111;color:white;border-color:#111}

/* ── SETTINGS TAB ─────────────────────────────────────────── */
.settings-section{background:white;border-radius:12px;padding:24px;margin-bottom:16px;
                  box-shadow:0 2px 8px rgba(0,0,0,.06)}
.settings-label{font-size:15px;font-weight:600;color:#111;margin-bottom:4px}
.settings-desc{font-size:13px;color:#888;margin-bottom:14px}
.settings-field{display:flex;flex-direction:column;gap:4px}
.settings-field label{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.4px}
.settings-field input{width:160px;padding:9px 12px;border:1.5px solid #e0e0e0;border-radius:8px;
                       font-size:14px;outline:none}
.settings-field input:focus{border-color:#111}
#brand-input{flex:1;padding:9px 12px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:14px;outline:none}
#brand-input:focus{border-color:#111}
.add-btn{padding:9px 18px;background:#111;color:white;border:none;border-radius:8px;
         font-size:14px;font-weight:600;cursor:pointer}
.add-btn:hover{background:#333}
#brand-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.brand-chip{display:flex;align-items:center;gap:6px;background:#f0f4ff;color:#3355aa;
            font-size:13px;font-weight:500;padding:5px 10px;border-radius:20px}
.brand-chip button{background:none;border:none;color:#99aacc;cursor:pointer;
                   font-size:14px;padding:0;line-height:1}
.brand-chip button:hover{color:#e00}
</style>
</head><body>

<div id="header">
  <img src="/static/logo.png" class="logo" alt="Product Seeding">
  <button class="tab-btn {% if tab=='discover' %}active{% endif %}" onclick="showTab('discover')">Discover</button>
  <button class="tab-btn {% if tab=='reviewed' %}active{% endif %}" onclick="showTab('reviewed')">Reviewed</button>
  <button class="tab-btn {% if tab=='settings' %}active{% endif %}" onclick="showTab('settings')">Settings</button>
  <div class="spacer"></div>
  <span class="user-badge">{{ user.name or user.email }}</span>
  <a class="logout-btn" href="/logout">Sign out</a>
</div>

<!-- ── DISCOVER ───────────────────────────────────────────── -->
<div id="discover">
  <div id="start-screen">
    <div id="start-card">
      <div id="start-icon">👋</div>
      <h2>Ready to review?</h2>
      <p id="start-sub">Loading prospects…</p>
      <button id="start-btn" onclick="startReviewing()" disabled>Start Reviewing</button>
    </div>
  </div>
  <div id="card-container" style="display:none"></div>
  <div id="buttons" style="display:none">
    <button class="btn btn-no" onclick="swipe('left')" title="Pass (0)">❌</button>
    <div class="btn-divider"></div>
    <button class="rating-btn" onclick="rate(1)">1</button>
    <button class="rating-btn" onclick="rate(2)">2</button>
    <button class="rating-btn" onclick="rate(3)">3</button>
    <button class="rating-btn" onclick="rate(4)">4</button>
    <button class="rating-btn" onclick="rate(5)">5</button>
  </div>
  <div id="rating-bar" style="display:none"></div>
  <div id="counter" style="display:none"></div>
  <div id="hint" style="display:none">← / → to swipe &nbsp;•&nbsp; 1–5 to rate &nbsp;•&nbsp; ↑ or click @handle to open profile</div>
  <div id="popup-blocked-msg" style="display:none;margin-top:10px;padding:8px 14px;background:#fff3cd;border:1px solid #ffc107;border-radius:8px;font-size:12px;color:#856404;max-width:380px;text-align:center">
    ⚠️ Popups are blocked. Click the address bar icon to <strong>allow popups for localhost:5001</strong>, then click any @handle to reopen.
  </div>
  <div id="ig-debug" style="margin-top:8px;font-size:11px;color:#aaa"></div>
  <div id="done-screen">
    <h2>🎉 All done!</h2>
    <p>You've reviewed all prospects.</p>
  </div>
</div>

<!-- ── REVIEWED ───────────────────────────────────────────── -->
<div id="reviewed">
  <div class="reviewed-header">
    <h2>Reviewed Profiles</h2>
    <span class="reviewed-meta" id="reviewed-count"></span>
  </div>
  <div class="filter-bar">
    <input type="text" id="search" placeholder="Search name or handle…" oninput="renderTable()">
    <button class="filter-btn active" data-min="1" data-max="5" onclick="setFilter(this)">All rated</button>
    <button class="filter-btn" data-min="4" data-max="5" onclick="setFilter(this)">★★★★+</button>
    <button class="filter-btn" data-min="3" data-max="5" onclick="setFilter(this)">★★★+</button>
    <button class="filter-btn" data-min="1" data-max="2" onclick="setFilter(this)">Low</button>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th onclick="sortBy('full_name')">Name</th>
          <th onclick="sortBy('followers')">Followers</th>
          <th onclick="sortBy('rating')">Rating</th>
          <th>Status</th>
          <th>Notes</th>
          <th onclick="sortBy('score')">Score</th>
        </tr>
      </thead>
      <tbody id="reviewed-tbody"></tbody>
    </table>
  </div>
</div>

<!-- ── SETTINGS ──────────────────────────────────────────── -->
<div id="settings" style="display:none;padding:80px 24px 40px;max-width:680px;margin:0 auto">
  <h2 style="font-size:20px;font-weight:700;margin-bottom:6px">Settings</h2>
  <p style="font-size:14px;color:#888;margin-bottom:32px">Changes save automatically and apply to the next scrape.</p>

  <div class="settings-section">
    <div class="settings-label">Brands to scrape</div>
    <div class="settings-desc">Instagram handles to pull tagged posts from. One brand per entry.</div>
    <div id="brand-chips"></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input id="brand-input" type="text" placeholder="e.g. arcteryx" onkeydown="if(event.key==='Enter')addBrand()">
      <button class="add-btn" onclick="addBrand()">Add</button>
    </div>
  </div>

  <div class="settings-section">
    <div class="settings-label">Follower range</div>
    <div class="settings-desc">Only include accounts within this follower range.</div>
    <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <div class="settings-field">
        <label>Min followers</label>
        <input type="number" id="min_followers" min="0" step="100" onchange="saveConfig()">
      </div>
      <div style="color:#ccc;font-size:20px;padding-top:18px">—</div>
      <div class="settings-field">
        <label>Max followers</label>
        <input type="number" id="max_followers" min="0" step="1000" onchange="saveConfig()">
      </div>
    </div>
  </div>

  <div class="settings-section">
    <div class="settings-label">Scrape depth</div>
    <div class="settings-desc">How many tagged posts to pull per brand. More = broader results but slower scrape.</div>
    <div class="settings-field">
      <label>Posts per brand</label>
      <input type="number" id="posts_per_brand" min="50" max="5000" step="50" onchange="saveConfig()">
    </div>
  </div>

  <div class="settings-section">
    <div class="settings-label">Engagement filter</div>
    <div class="settings-desc">Minimum engagement rate (likes+comments / followers). 0.02 = 2%.</div>
    <div class="settings-field">
      <label>Min engagement rate</label>
      <input type="number" id="min_engagement_rate" min="0" max="1" step="0.01" onchange="saveConfig()">
    </div>
  </div>

  <div class="settings-section">
    <div class="settings-label">Outreach statuses</div>
    <div class="settings-desc">The options that appear in the Status dropdown on the Reviewed tab.</div>
    <div id="status-chips"></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input id="status-input" type="text" placeholder="e.g. shipped" onkeydown="if(event.key==='Enter')addStatus()">
      <button class="add-btn" onclick="addStatus()">Add</button>
    </div>
  </div>

  <div class="settings-section">
    <div class="settings-label">Team</div>
    <div class="settings-desc">People who can log in and review prospects.</div>
    <div id="team-list" style="margin-bottom:16px"></div>
    <div style="border-top:1px solid #f0f0f0;padding-top:16px">
      <div style="font-size:13px;font-weight:600;color:#444;margin-bottom:12px">Invite a teammate</div>
      <div style="display:flex;flex-direction:column;gap:10px;max-width:360px">
        <input id="new-email" type="email" placeholder="their@email.com"
               style="padding:9px 12px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:14px;outline:none">
        <div style="display:flex;align-items:center;gap:12px">
          <button class="add-btn" onclick="inviteTeammate()">Send invite</button>
          <span id="team-msg" style="font-size:13px;color:#888"></span>
        </div>
        <div id="invite-link-box" style="display:none;background:#f0f4ff;border-radius:8px;padding:12px">
          <div style="font-size:12px;font-weight:600;color:#555;margin-bottom:6px">
            📋 Copy this link and send it to them:
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <input id="invite-link" type="text" readonly
                   style="flex:1;padding:7px 10px;border:1.5px solid #c0cce8;border-radius:6px;
                          font-size:12px;background:white;color:#111;outline:none">
            <button onclick="copyInviteLink()"
                    style="padding:7px 12px;background:#111;color:white;border:none;
                           border-radius:6px;font-size:12px;cursor:pointer;white-space:nowrap">
              Copy
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="settings-saved" style="display:none;color:#16a34a;font-size:13px;margin-top:8px">✓ Saved</div>

  <div class="settings-section">
    <div class="settings-label">Find new creators</div>
    <div class="settings-desc">Scrape tagged posts from all brands above, score them, and add new prospects to your queue.</div>
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <button id="settings-scrape-btn" onclick="settingsScrape()" style="padding:12px 28px;background:#111;color:white;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer">
        🔍 Run Scrape
      </button>
      <span id="settings-scrape-msg" style="font-size:13px;color:#888"></span>
    </div>
    <div id="settings-scrape-progress" style="display:none;margin-top:16px">
      <div style="background:#f0f0f0;border-radius:99px;height:10px;overflow:hidden;margin-bottom:6px">
        <div id="settings-scrape-bar" style="height:100%;width:0%;background:#111;border-radius:99px;transition:width .5s ease"></div>
      </div>
      <div id="settings-scrape-stage" style="font-size:12px;color:#888">Starting…</div>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────
let allProspects = [];
let reviewedData = [];
let current = 0;
let profileWindow = null;
let started = false;
let sortCol = 'avg_rating', sortAsc = false;
let filterMin = 1, filterMax = 5;

// ── Tab switching ──────────────────────────────────────────
function showTab(tab) {
  ['discover','reviewed','settings'].forEach(t =>
    document.getElementById(t).style.display = 'none');
  document.getElementById(tab).style.display = tab==='discover' ? 'flex' : 'block';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const idx = ['discover','reviewed','settings'].indexOf(tab);
  document.querySelectorAll('.tab-btn')[idx].classList.add('active');
  if (tab==='reviewed') loadReviewed();
  if (tab==='settings') loadSettings();
  history.replaceState(null,'','/?tab='+tab);
}

// ── Settings ───────────────────────────────────────────────
let configData = {};

async function loadSettings() {
  const res = await fetch('/api/config');
  configData = await res.json();
  document.getElementById('min_followers').value       = configData.min_followers;
  document.getElementById('max_followers').value       = configData.max_followers;
  document.getElementById('posts_per_brand').value     = configData.posts_per_brand;
  document.getElementById('min_engagement_rate').value = configData.min_engagement_rate;
  renderBrandChips();
  renderStatusChips();
  loadTeam();
}

function renderBrandChips() {
  const container = document.getElementById('brand-chips');
  container.innerHTML = (configData.target_brands || []).map(b => `
    <div class="brand-chip">
      @${b}
      <button onclick="removeBrand('${b}')" title="Remove">×</button>
    </div>`).join('');
}

function addBrand() {
  const input = document.getElementById('brand-input');
  const val = input.value.trim().replace(/^@/,'').toLowerCase();
  if (!val) return;
  if (!configData.target_brands.includes(val)) {
    configData.target_brands.push(val);
    renderBrandChips();
    saveConfig();
  }
  input.value = '';
  input.focus();
}

function removeBrand(brand) {
  configData.target_brands = configData.target_brands.filter(b => b !== brand);
  renderBrandChips();
  saveConfig();
}

function renderStatusChips() {
  const container = document.getElementById('status-chips');
  container.innerHTML = (configData.outreach_statuses || []).map(s => `
    <div class="brand-chip">
      ${s}
      <button onclick="removeStatus('${s}')" title="Remove">×</button>
    </div>`).join('');
}

function addStatus() {
  const input = document.getElementById('status-input');
  const val = input.value.trim().toLowerCase();
  if (!val) return;
  if (!configData.outreach_statuses) configData.outreach_statuses = [];
  if (!configData.outreach_statuses.includes(val)) {
    configData.outreach_statuses.push(val);
    renderStatusChips();
    saveConfig();
  }
  input.value = '';
  input.focus();
}

function removeStatus(status) {
  configData.outreach_statuses = configData.outreach_statuses.filter(s => s !== status);
  renderStatusChips();
  saveConfig();
}

// ── Team ───────────────────────────────────────────────────
async function loadTeam() {
  const res = await fetch('/api/users');
  const users = await res.json();
  const currentId = {{ user.id }};
  document.getElementById('team-list').innerHTML = users.map(u => `
    <div style="display:flex;align-items:center;justify-content:space-between;
                padding:10px 0;border-bottom:1px solid #f5f5f5">
      <div>
        <div style="font-size:14px;font-weight:600;color:#111">${u.name || '—'}</div>
        <div style="font-size:12px;color:#888">${u.email}</div>
      </div>
      ${u.id !== currentId ? `<button onclick="removeTeammate(${u.id},'${u.name||u.email}')"
        style="background:none;border:none;color:#ccc;cursor:pointer;font-size:18px"
        title="Remove">×</button>` : `<span style="font-size:11px;color:#aaa">you</span>`}
    </div>`).join('');
}

async function inviteTeammate() {
  const email = document.getElementById('new-email').value.trim();
  const msg   = document.getElementById('team-msg');
  const linkBox = document.getElementById('invite-link-box');
  if (!email) { msg.textContent = 'Enter an email address.'; return; }
  msg.style.color = '#888';
  msg.textContent = 'Generating invite…';
  linkBox.style.display = 'none';
  const res = await fetch('/api/invite_user', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email})});
  const data = await res.json();
  if (data.ok) {
    msg.textContent = '';
    document.getElementById('new-email').value = '';
    document.getElementById('invite-link').value = data.invite_url;
    linkBox.style.display = 'block';
  } else {
    msg.style.color = '#dc2626';
    msg.textContent = data.error || 'Could not create invite.';
  }
}

function copyInviteLink() {
  const input = document.getElementById('invite-link');
  input.select();
  navigator.clipboard.writeText(input.value).then(() => {
    const btn = input.nextElementSibling;
    btn.textContent = 'Copied!';
    btn.style.background = '#16a34a';
    setTimeout(() => { btn.textContent = 'Copy'; btn.style.background = '#111'; }, 2000);
  });
}

async function removeTeammate(id, label) {
  if (!confirm(`Remove ${label} from the team?`)) return;
  const res = await fetch('/api/remove_user', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})});
  const data = await res.json();
  if (data.ok) loadTeam();
  else alert(data.error);
}

let saveTimer = null;
function saveConfig() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    configData.min_followers       = parseInt(document.getElementById('min_followers').value) || 300;
    configData.max_followers       = parseInt(document.getElementById('max_followers').value) || 999999;
    configData.posts_per_brand     = parseInt(document.getElementById('posts_per_brand').value) || 500;
    configData.min_engagement_rate = parseFloat(document.getElementById('min_engagement_rate').value) || 0.01;
    await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(configData)});
    const saved = document.getElementById('settings-saved');
    saved.style.display = 'block';
    setTimeout(() => saved.style.display = 'none', 2000);
  }, 600);
}

// ── Discover ───────────────────────────────────────────────
async function loadProspects() {
  const already = await checkExistingScrape();
  if (already) return;
  const res = await fetch('/api/prospects');
  allProspects = await res.json();
  // Start at first unrated
  current = allProspects.findIndex(p => p.rating == null || p.rating === '');
  if (current === -1) current = 0;
  const sub = document.getElementById('start-sub');
  const btn = document.getElementById('start-btn');
  sub.textContent = `${allProspects.length} unrated · ready to review`;
  if (allProspects.length === 0) {
    document.getElementById('start-icon').textContent = '🔍';
    document.getElementById('start-card').querySelector('h2').textContent = 'All caught up!';
    sub.textContent = "You've reviewed everyone. Search for new creators to keep going.";
    btn.textContent = 'Search for More Creators';
    btn.disabled = false;
    btn.onclick = startScrape;
  } else {
    btn.disabled = false;
  }
}

let pollTimer = null;

async function startScrape() {
  const res = await fetch('/api/scrape', {method: 'POST'});
  const data = await res.json();
  if (data.ok) {
    showScrapeProgress();
    pollScrapeStatus();
  } else {
    document.getElementById('start-sub').textContent = 'Error: ' + (data.error || 'Could not start.');
  }
}

function showScrapeProgress() {
  const card = document.getElementById('start-card');
  card.innerHTML = `
    <div id="start-icon" style="font-size:48px;margin-bottom:16px">🔍</div>
    <h2 style="font-size:20px;font-weight:700;margin-bottom:8px">Finding creators…</h2>
    <p id="scrape-stage" style="font-size:13px;color:#888;margin-bottom:20px">Starting up…</p>
    <div style="background:#f0f0f0;border-radius:99px;height:10px;overflow:hidden;margin-bottom:10px">
      <div id="scrape-bar" style="height:100%;width:0%;background:#111;border-radius:99px;transition:width .5s ease"></div>
    </div>
    <div id="scrape-pct" style="font-size:12px;color:#aaa;text-align:right">0%</div>
  `;
}

async function pollScrapeStatus() {
  try {
    const res = await fetch('/api/scrape/status');
    const s = await res.json();
    const bar = document.getElementById('scrape-bar');
    const stage = document.getElementById('scrape-stage');
    const pct = document.getElementById('scrape-pct');
    if (bar) bar.style.width = s.pct + '%';
    if (stage) stage.textContent = s.stage || 'Running…';
    if (pct) pct.textContent = s.pct + '%';
    if (s.pct === 100) {
      document.getElementById('start-icon').textContent = '🎉';
      document.getElementById('start-card').querySelector('h2').textContent = 'Done!';
      stage.textContent = s.stage || 'New creators are ready.';
      const btn = document.createElement('button');
      btn.id = 'start-btn';
      btn.style.cssText = 'margin-top:20px;padding:12px 28px;background:#111;color:white;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;width:100%';
      btn.textContent = 'Start Reviewing';
      btn.onclick = () => location.reload();
      document.getElementById('start-card').appendChild(btn);
      return;
    }
    // Show live count if available in stage text
    const startSub = document.getElementById('start-sub');
    if (startSub && s.stage && s.stage.includes('inserted')) startSub.textContent = s.stage;
    if (s.error) {
      document.getElementById('start-icon').textContent = '⚠️';
      stage.textContent = 'Error: ' + s.error;
      return;
    }
    pollTimer = setTimeout(pollScrapeStatus, 3000);
  } catch(e) {
    pollTimer = setTimeout(pollScrapeStatus, 5000);
  }
}

async function settingsScrape() {
  const btn = document.getElementById('settings-scrape-btn');
  const msg = document.getElementById('settings-scrape-msg');
  const prog = document.getElementById('settings-scrape-progress');
  const bar = document.getElementById('settings-scrape-bar');
  const stage = document.getElementById('settings-scrape-stage');
  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  msg.textContent = '';
  const res = await fetch('/api/scrape', {method:'POST'});
  const data = await res.json();
  if (!data.ok) { btn.disabled=false; btn.textContent='🔍 Run Scrape'; msg.textContent='Error: '+(data.error||'Could not start.'); return; }
  prog.style.display = 'block';
  async function poll() {
    try {
      const s = await (await fetch('/api/scrape/status')).json();
      bar.style.width = s.pct+'%';
      stage.textContent = s.stage || 'Running…';
      if (s.pct === 100) { btn.disabled=false; btn.textContent='🔍 Run Scrape'; msg.textContent='✓ Done! Go to Discover tab to review new creators.'; return; }
      if (s.error) { btn.disabled=false; btn.textContent='🔍 Run Scrape'; stage.textContent='Error: '+s.error; return; }
      setTimeout(poll, 3000);
    } catch(e) { setTimeout(poll, 5000); }
  }
  poll();
}

// Resume polling if scrape was already running when page loaded
async function checkExistingScrape() {
  const res = await fetch('/api/scrape/status');
  const s = await res.json();
  if (s.running || s.pct > 0) {
    showScrapeProgress();
    pollScrapeStatus();
    return true;
  }
  return false;
}

function startReviewing() {
  started = true;
  document.getElementById('start-screen').style.display = 'none';
  document.getElementById('card-container').style.display = 'block';
  document.getElementById('buttons').style.display = 'flex';
  document.getElementById('counter').style.display = 'block';
  document.getElementById('hint').style.display = 'block';
  showCard();
  updateCounter();
}

function fmt(n) {
  if (n >= 1000000) return (n/1000000).toFixed(1)+'M';
  if (n >= 1000) return (n/1000).toFixed(1)+'K';
  return n;
}

function imageGrid(images) {
  if (!images || !images.length) return '';
  // Instagram CDN URLs are blocked cross-origin — show count placeholder instead
  const n = Math.min(images.length, 6);
  const cells = Array(6).fill(0).map((_,i) =>
    `<div class="image-grid-placeholder" style="font-size:11px;color:#bbb">${i<n?'📷':''}</div>`
  );
  return `<div class="image-grid">${cells.join('')}</div>`;
}

function openProfileWindow(url) {
  const w=680, h=window.screen.height, left=window.screen.width-w;
  const features = `width=${w},height=${h},left=${left},top=0,scrollbars=yes,resizable=yes`;
  // window.open with a named target: if the window is already open it navigates
  // it (no popup blocker). If it's closed, it opens a new one (needs user gesture).
  const win = window.open(url, 'gravel_ig', features);
  if (win) {
    profileWindow = win;
    win.focus();
    document.getElementById('popup-blocked-msg').style.display = 'none';
  } else {
    // Popup was blocked — show a one-time message so user can allow it
    document.getElementById('popup-blocked-msg').style.display = 'block';
    profileWindow = null;
  }
}

function closeProfileWindow() {
  try {
    if (profileWindow && !profileWindow.closed) profileWindow.close();
  } catch(e) {}
  profileWindow = null;
}

function showCard(skipPopup) {
  const container = document.getElementById('card-container');
  container.innerHTML = '';

  if (current >= allProspects.length) {
    container.style.display = 'none';
    document.getElementById('buttons').style.display = 'none';
    document.getElementById('done-screen').style.display = 'block';
    closeProfileWindow();
    return;
  }

  const p = allProspects[current];
  const initial = (p.username||'?')[0].toUpperCase();
  const brandList = (p.source_brands_str||'').split(',').filter(Boolean);
  const brands = brandList.map(b=>`<span class="brand-tag">@${b.trim()}</span>`).join('');
  const brandLine = brandList.length
    ? `<div style="font-size:11px;color:#aaa;margin-bottom:4px">Found via ${brandList.map(b=>'@'+b.trim()).join(', ')}</div>`
    : '';

  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <div class="overlay-label overlay-yes" id="ov-yes">LIKE</div>
    <div class="overlay-label overlay-no"  id="ov-no">PASS</div>
    <div class="card-score">Score: ${p.score||0}</div>
    <div class="card-avatar">${initial}</div>
    <div class="card-name">${p.full_name||p.username}</div>
    <div class="card-handle"><a href="${p.profile_url}" target="gravel_ig" onclick="openProfileWindow('${p.profile_url}');return false;">@${p.username}</a></div>
    ${brandLine}
    <div class="card-stats">
      <div class="stat"><span class="stat-value">${fmt(p.followers)}</span><span class="stat-label">Followers</span></div>
      <div class="stat"><span class="stat-value">${fmt(p.following)}</span><span class="stat-label">Following</span></div>
      <div class="stat"><span class="stat-value">${p.posts_count||0}</span><span class="stat-label">Posts</span></div>
    </div>
    <div class="card-bio">${p.bio||'No bio'}</div>
  `;
  setupDrag(card);
  container.appendChild(card);
  if (started && !skipPopup) openProfileWindow(p.profile_url);
}

function updateCounter() {
  const unrated = allProspects.filter(p => p.rating==null||p.rating==='').length;
  document.getElementById('counter').textContent = `${allProspects.length - unrated} / ${allProspects.length} reviewed`;
}

function openProfile() {
  if (current < allProspects.length) openProfileWindow(allProspects[current].profile_url);
}

async function rate(value) {
  if (current >= allProspects.length) return;
  const p = allProspects[current];
  p.rating = value;
  fetch('/api/rate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: p.username, rating: value})});

  const next = allProspects[current + 1];
  const igW=680, igH=window.screen.height, igLeft=window.screen.width-680;
  const igFeatures = `width=${igW},height=${igH},left=${igLeft},top=0,scrollbars=yes,resizable=yes`;
  if (next) {
    // window.open with same name reuses the existing window — same window, new profile
    profileWindow = window.open(next.profile_url, 'gravel_ig', igFeatures);
  } else {
    // Last profile — close the window
    closeProfileWindow();
  }

  const card = document.querySelector('.card');
  if (card) {
    card.classList.add(value >= 3 ? 'fly-right' : 'fly-left');
    setTimeout(() => { current++; showCard(true); updateCounter(); }, 280);
  }
}

function swipe(dir) { rate(dir==='right' ? 4 : 0); }

document.addEventListener('keydown', e => {
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
  if (e.key==='ArrowLeft')  swipe('left');
  else if (e.key==='ArrowRight') swipe('right');
  else if (e.key==='ArrowUp') openProfile();
  else if (['1','2','3','4','5'].includes(e.key)) rate(parseInt(e.key));
});

function setupDrag(card) {
  let startX=0, currentX=0;
  const ovY = card.querySelector('#ov-yes');
  const ovN = card.querySelector('#ov-no');
  card.addEventListener('mousedown', e=>{ startX=e.clientX; card.classList.add('dragging'); });
  document.addEventListener('mousemove', e=>{
    if (!card.classList.contains('dragging')) return;
    currentX = e.clientX - startX;
    card.style.transform = `translateX(${currentX}px) rotate(${currentX*.05}deg)`;
    const pct = Math.min(Math.abs(currentX)/100,1);
    ovY.style.opacity = currentX>0 ? pct : 0;
    ovN.style.opacity = currentX<0 ? pct : 0;
  });
  document.addEventListener('mouseup', ()=>{
    if (!card.classList.contains('dragging')) return;
    card.classList.remove('dragging');
    if (Math.abs(currentX)>100) swipe(currentX>0?'right':'left');
    else { card.style.transform=''; ovY.style.opacity=0; ovN.style.opacity=0; }
  });
}

// ── Reviewed tab ───────────────────────────────────────────
let reviewedUsers = [];

async function loadReviewed() {
  try {
    // Also fetch config so outreach_statuses dropdowns are populated
    const [revRes, cfgRes] = await Promise.all([
      fetch('/api/reviewed'),
      fetch('/api/config')
    ]);
    const data = await revRes.json();
    if (Object.keys(configData).length === 0) {
      configData = await cfgRes.json();
    }
    reviewedUsers = data.users     || [];
    reviewedData  = data.prospects || [];
    console.log('loadReviewed: got', reviewedData.length, 'prospects,', reviewedUsers.length, 'users');
    document.getElementById('reviewed-count').textContent = reviewedData.length + ' profiles';
    renderTable();
  } catch(e) {
    console.error('loadReviewed failed:', e);
    document.getElementById('reviewed-count').textContent = 'Error loading data — see console';
  }
}

function setFilter(btn) {
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  filterMin = parseInt(btn.dataset.min);
  filterMax = parseInt(btn.dataset.max);
  renderTable();
}

function sortBy(col) {
  if (sortCol===col) sortAsc=!sortAsc; else { sortCol=col; sortAsc=false; }
  renderTable();
}

function renderTable() {
  const q = document.getElementById('search').value.toLowerCase();
  let rows = reviewedData.filter(r => {
    const rating = parseFloat(r.avg_rating)||0;
    if (rating < filterMin || rating > filterMax) return false;
    if (q && !`${r.full_name} ${r.username}`.toLowerCase().includes(q)) return false;
    return true;
  });

  rows.sort((a,b)=>{
    let va=a[sortCol]??0, vb=b[sortCol]??0;
    if (typeof va==='string') va=va.toLowerCase();
    if (typeof vb==='string') vb=vb.toLowerCase();
    return sortAsc ? (va>vb?1:-1) : (va<vb?1:-1);
  });

  // Dynamic user-rating column headers — just one Avg column now
  const currentId = {{ user.id }};
  const thead = document.querySelector('#reviewed table thead tr');
  thead.innerHTML = `
    <th onclick="sortBy('full_name')">Name</th>
    <th onclick="sortBy('followers')">Followers</th>
    <th onclick="sortBy('avg_rating')">Rating</th>
    <th>Status</th><th>Notes</th>
    <th onclick="sortBy('score')">Score</th>`;

  const tbody = document.getElementById('reviewed-tbody');
  tbody.innerHTML = rows.map(r => {
    const avg = r.avg_rating || 0;
    const avgColor = avg>=4?'#16a34a':avg>=2?'#f59e0b':'#aaa';

    const tooltipHtml = reviewedUsers.map(function(u) {
      const val = (r.ratings && r.ratings[u.id]) || null;
      const name = u.name || u.email.split('@')[0];
      const stars = val ? '★'.repeat(val) + '☆'.repeat(5-val) : '—';
      return '<div><span style="color:#ccc;font-size:11px">' + name + '</span> <span style="color:#fff">' + stars + '</span></div>';
    }).join('');

    const statuses = ['', ...(configData.outreach_statuses || [])];
    const statusOpts = statuses.map(s=>`<option ${r.outreach_status===s?'selected':''}>${s}</option>`).join('');

    return `<tr>
      <td>
        <div class="t-name">${r.full_name||r.username}</div>
        <a class="t-handle" href="${r.profile_url}" target="_blank">@${r.username}</a>
      </td>
      <td class="t-followers">${fmt(r.followers)}</td>
      <td>
        <div class="avg-rating-cell" style="position:relative;display:inline-block">
          <span style="font-weight:700;color:${avgColor};cursor:default">${avg}</span>
          <div class="avg-tooltip">${tooltipHtml}</div>
        </div>
      </td>
      <td class="t-status">
        <select onchange="updateField('${r.username}','outreach_status',this.value)">${statusOpts}</select>
      </td>
      <td class="t-notes">
        <input type="text" value="${(r.notes||'').replace(/"/g,'&quot;')}" placeholder="Add note…"
          onblur="updateField('${r.username}','notes',this.value)"
          onkeydown="if(event.key==='Enter')this.blur()">
      </td>
      <td style="color:#888">${r.score||0}</td>
    </tr>`;
  }).join('');
}

function updateField(username, field, value) {
  fetch('/api/update_row', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, field, value})});
  const row = reviewedData.find(r=>r.username===username);
  if (row) { row[field]=value; renderTable(); }
}

function updateRating(username, value) {
  fetch('/api/update_row', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, field:'rating', value})});
  const row = reviewedData.find(r=>r.username===username);
  const currentId = {{ user.id }};
  if (row) {
    if (!row.ratings) row.ratings = {};
    row.ratings[currentId] = value;
    const vals = Object.values(row.ratings).filter(v=>v);
    row.avg_rating = vals.length ? Math.round(vals.reduce((a,b)=>a+b,0)/vals.length*10)/10 : 0;
    renderTable();
  }
}

function hoverStars(el, n) {
  [...el.parentElement.children].forEach((s,i)=>{
    s.textContent=i<n?'★':'☆'; s.style.color=i<n?'#f59e0b':'#ddd';
  });
}
function unhoverStars(el, current) {
  [...el.parentElement.children].forEach((s,i)=>{
    s.textContent=i<current?'★':'☆'; s.style.color=i<current?'#f59e0b':'#ddd';
  });
}

// ── Init ───────────────────────────────────────────────────
const initTab = '{{ tab }}';
showTab(initTab);
loadProspects();
</script>
</body></html>"""


# ── Bootstrap ──────────────────────────────────────────────────────────────────

# Always initialize the DB — runs under both gunicorn and direct python
init_db()

if __name__ == "__main__":
    import webbrowser
    init_db()
    print("\\n🎯 Gravel Prospect App")

    # Check if any users exist
    db = sqlite3.connect(DB_PATH)
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    prospect_count = db.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    db.close()

    if count == 0:
        print("No users found — opening registration page.")
        print("Run  python3 -B migrate.py  after logging in to import your data.")
    else:
        print(f"Users: {count}  |  Prospects: {prospect_count}")

    port = int(os.environ.get("PORT", 5001))
    print(f"Opening at http://localhost:{port}")
    print("Press Ctrl+C to stop\\n")
    if port == 5001:
        webbrowser.open(f"http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
