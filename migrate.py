"""
migrate.py — Import existing CSV + Google Sheet data into the SQLite database.

Run once:  python3 -B migrate.py
"""

import os, json, sqlite3
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

DB_PATH  = os.path.join(os.path.dirname(__file__), "data", "gravel.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "scored_accounts.csv")
RAW_PATH = os.path.join(os.path.dirname(__file__), "data", "raw_accounts.json")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

def get_sheet():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).sheet1

def run():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Load raw images
    raw_images = {}
    if os.path.exists(RAW_PATH):
        with open(RAW_PATH) as f:
            raw = json.load(f)
        raw_images = {a["username"]: a.get("recent_images", []) for a in raw}
        print(f"→ Loaded images for {len(raw_images)} accounts from raw_accounts.json")

    # Load CSV
    df = pd.read_csv(CSV_PATH).fillna("")
    print(f"→ Found {len(df)} prospects in scored_accounts.csv")

    # Import prospects
    inserted = 0
    for _, row in df.iterrows():
        username = str(row.get("username","")).strip().lower()
        if not username: continue
        images = json.dumps(raw_images.get(username, []))
        try:
            db.execute("""
                INSERT INTO prospects
                  (username, full_name, bio, followers, following, posts_count,
                   profile_url, source_brands_str, score, ml_score, recent_images)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                  full_name=excluded.full_name, bio=excluded.bio,
                  followers=excluded.followers, following=excluded.following,
                  posts_count=excluded.posts_count, profile_url=excluded.profile_url,
                  source_brands_str=excluded.source_brands_str,
                  score=excluded.score, ml_score=excluded.ml_score,
                  recent_images=excluded.recent_images
            """, [
                username,
                str(row.get("full_name","")),
                str(row.get("bio","")),
                int(row.get("followers",0) or 0),
                int(row.get("following",0) or 0),
                int(row.get("posts_count",0) or 0),
                str(row.get("profile_url","")),
                str(row.get("source_brands_str","")),
                float(row.get("score",0) or 0),
                float(row.get("ml_score",0) or 0),
                images,
            ])
            inserted += 1
        except Exception as e:
            print(f"  ⚠ Skipped {username}: {e}")
    db.commit()
    print(f"✓ Imported {inserted} prospects")

    # Pull ratings from Google Sheet
    print("\n→ Fetching ratings from Google Sheet…")
    try:
        worksheet = get_sheet()
        rows = worksheet.get_all_records()
        print(f"  Found {len(rows)} rows in sheet")

        # Use the first user as the owner of imported sheet ratings
        default_user = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not default_user:
            print("  ⚠ No users in DB — create an account first, then re-run migrate.py")
            raise SystemExit

        user_id = default_user["id"]
        rated = 0
        for row in rows:
            username = str(row.get("username","")).strip().lower()
            if not username: continue
            rating_raw = str(row.get("approved","")).strip()
            try:
                rating = int(float(rating_raw))
            except:
                rating = None
            if rating is None:
                continue

            prospect = db.execute("SELECT id FROM prospects WHERE username=?", [username]).fetchone()
            if not prospect:
                continue

            # Rating goes into ratings table (per-user)
            db.execute("""
                INSERT INTO ratings (prospect_id, user_id, rating, updated_at)
                VALUES (?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(prospect_id, user_id) DO UPDATE SET
                  rating=excluded.rating, updated_at=CURRENT_TIMESTAMP
            """, [prospect["id"], user_id, rating])

            # Notes / outreach fields go into prospect_meta
            db.execute("""
                INSERT INTO prospect_meta
                  (prospect_id, notes, outreach_status,
                   address_collected, product_sent, posted_about_gravel)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(prospect_id) DO UPDATE SET
                  notes=excluded.notes,
                  outreach_status=excluded.outreach_status,
                  address_collected=excluded.address_collected,
                  product_sent=excluded.product_sent,
                  posted_about_gravel=excluded.posted_about_gravel
            """, [
                prospect["id"],
                str(row.get("notes","")),
                str(row.get("outreach_status","")),
                str(row.get("address_collected","")),
                str(row.get("product_sent","")),
                str(row.get("posted_about_gravel","")),
            ])
            rated += 1

        db.commit()
        print(f"✓ Imported {rated} ratings from Google Sheet")

    except SystemExit:
        pass
    except Exception as e:
        print(f"  ⚠ Could not load Google Sheet: {e}")
        print("  Ratings not imported — you can re-run migrate.py once the sheet is accessible.")

    db.close()
    print("\n✅ Migration complete. Run: python3 -B app.py")

if __name__ == "__main__":
    run()
