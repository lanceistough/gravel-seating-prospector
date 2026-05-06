"""
scrape.py — Scrape Instagram accounts and insert them into the DB in real-time.

For each target brand:
  1. Pull tagged posts via Apify
  2. Extract unique poster handles
  3. Enrich profiles via Apify
  4. Score each account (score.py logic)
  5. Upsert into SQLite immediately — visible in the app right away
  6. Save a checkpoint so an interrupted run can resume

Run standalone:  python3 -B scrape.py
Or triggered via the app's /api/scrape endpoint.
"""

import os, json, sqlite3, time, yaml
from pathlib import Path
from dotenv import load_dotenv
from apify_client import ApifyClient
from tqdm import tqdm

# Import scoring logic from score.py
from score import score_account, load_config as _load_config

load_dotenv()

DB_PATH         = os.path.join(os.path.dirname(__file__), "data", "gravel.db")
CHECKPOINT_PATH = os.path.join(os.path.dirname(__file__), "data", "scrape_checkpoint.json")


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_apify_client():
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise ValueError("APIFY_API_TOKEN not set in .env")
    return ApifyClient(token)


def load_checkpoint() -> dict:
    if Path(CHECKPOINT_PATH).exists():
        try:
            data = json.loads(Path(CHECKPOINT_PATH).read_text())
            print(f"→ Resuming checkpoint: {len(data['done_brands'])} brands done, "
                  f"{data['total_inserted']} accounts inserted so far")
            return data
        except Exception:
            pass
    return {"done_brands": [], "total_inserted": 0}


def save_checkpoint(done_brands: list, total_inserted: int):
    Path("data").mkdir(exist_ok=True)
    Path(CHECKPOINT_PATH).write_text(
        json.dumps({"done_brands": done_brands, "total_inserted": total_inserted})
    )


def upsert_accounts(accounts: list[dict], config: dict) -> tuple[int, int]:
    """Score accounts and upsert into DB. Returns (inserted_new, updated_existing)."""
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    inserted = updated = 0

    for raw in accounts:
        scored = score_account(raw, config)
        if scored is None:
            continue

        username = scored["username"].lower()
        source   = scored.get("source_brands_str", "")
        images   = json.dumps(raw.get("recent_images", []))

        # Check if already exists
        existing = db.execute(
            "SELECT id, source_brands_str FROM prospects WHERE username=?", [username]
        ).fetchone()

        if existing:
            # Merge source brands
            old_brands = set(b.strip() for b in (existing["source_brands_str"] or "").split(",") if b.strip())
            new_brands = set(b.strip() for b in source.split(",") if b.strip())
            merged = ", ".join(sorted(old_brands | new_brands))
            db.execute(
                "UPDATE prospects SET source_brands_str=?, score=?, recent_images=? WHERE id=?",
                [merged, scored["score"], images, existing["id"]]
            )
            updated += 1
        else:
            db.execute("""
                INSERT INTO prospects
                  (username, full_name, bio, followers, following, posts_count,
                   profile_url, source_brands_str, score, ml_score, recent_images)
                VALUES (?,?,?,?,?,?,?,?,?,0,?)
            """, [
                username,
                scored.get("full_name", ""),
                scored.get("bio", ""),
                int(scored.get("followers", 0) or 0),
                int(scored.get("following", 0) or 0),
                int(scored.get("posts_count", 0) or 0),
                scored.get("profile_url", ""),
                source,
                float(scored.get("score", 0) or 0),
                images,
            ])
            inserted += 1

    db.commit()
    db.close()
    return inserted, updated


# ── Main scrape loop ──────────────────────────────────────────────────────────

def run(status_callback=None):
    """
    status_callback(stage_str, pct_int) — optional hook for the web app
    to update its progress display.
    """
    config = _load_config()
    client = get_apify_client()

    checkpoint     = load_checkpoint()
    done_brands    = set(checkpoint["done_brands"])  # may include old brand names — that's fine
    total_inserted = checkpoint["total_inserted"]

    brands    = config["target_brands"]
    total     = len(brands)
    remaining = [b for b in brands if b not in done_brands]
    # How many current-config brands are already done (for accurate progress)
    done_current = total - len(remaining)

    print(f"→ {done_current}/{total} brands already done, {len(remaining)} remaining")

    for i, brand in enumerate(remaining):
        brand_num = done_current + i + 1
        pct = min(99, int(brand_num / total * 90))  # cap at 99 until truly done

        stage = f"Scraping @{brand} ({brand_num}/{total})…"
        print(f"\n{'─'*60}\n{stage}")
        if status_callback:
            status_callback(stage, pct)

        tagged_url = f"https://www.instagram.com/{brand}/tagged/"

        # ── Step 1: pull tagged posts ──────────────────────────────────────
        try:
            run_result = client.actor("apify/instagram-scraper").call(
                run_input={
                    "directUrls":     [tagged_url],
                    "resultsType":    "posts",
                    "resultsLimit":   config["posts_per_brand"],
                    "skipPinnedPosts": False,
                }
            )
            posts = list(client.dataset(run_result["defaultDatasetId"]).iterate_items())
        except Exception as e:
            print(f"   ⚠ Failed to scrape @{brand}: {e} — skipping")
            done_brands.append(brand)
            save_checkpoint(done_brands, total_inserted)
            continue

        print(f"   {len(posts)} tagged posts")

        # ── Step 2: extract unique poster handles ──────────────────────────
        handles = set()
        for post in posts:
            owner = (
                post.get("ownerUsername")
                or post.get("owner", {}).get("username")
                or (post.get("coauthorProducers") or [{}])[0].get("username")
            )
            if owner:
                handles.add(str(owner).lower())

        print(f"   {len(handles)} unique posters")
        if not handles:
            done_brands.append(brand)
            save_checkpoint(done_brands, total_inserted)
            continue

        # ── Step 3: enrich profiles ────────────────────────────────────────
        stage2 = f"Enriching {len(handles)} profiles from @{brand}…"
        print(f"   {stage2}")
        if status_callback:
            status_callback(stage2, pct)

        try:
            profile_run = client.actor("apify/instagram-profile-scraper").call(
                run_input={"usernames": list(handles)}
            )
            profiles = list(client.dataset(profile_run["defaultDatasetId"]).iterate_items())
        except Exception as e:
            print(f"   ⚠ Profile enrichment failed for @{brand}: {e} — skipping")
            done_brands.append(brand)
            save_checkpoint(done_brands, total_inserted)
            continue

        # ── Step 4: build account dicts ────────────────────────────────────
        raw_accounts = []
        for profile in profiles:
            username = (profile.get("username") or "").lower()
            if not username:
                continue
            recent_images = []
            for post in (profile.get("latestPosts") or [])[:6]:
                img = post.get("displayUrl") or post.get("thumbnailUrl") or post.get("url")
                if img:
                    recent_images.append(img)
            raw_accounts.append({
                "username":     username,
                "full_name":    profile.get("fullName", ""),
                "bio":          profile.get("biography", ""),
                "followers":    profile.get("followersCount", 0),
                "following":    profile.get("followsCount", 0),
                "posts_count":  profile.get("postsCount", 0),
                "profile_url":  f"https://www.instagram.com/{username}/",
                "external_url": profile.get("externalUrl", ""),
                "is_business":  profile.get("isBusinessAccount", False),
                "recent_images": recent_images,
                "source_brands": [brand],
                "engagement_rate": profile.get("engagementRate"),
            })

        # ── Step 5: score + insert into DB immediately ─────────────────────
        new, upd = upsert_accounts(raw_accounts, config)
        total_inserted += new
        print(f"   ✓ +{new} new prospects inserted, {upd} updated (total: {total_inserted})")

        done_brands.add(brand)
        save_checkpoint(list(done_brands), total_inserted)
        time.sleep(2)

    # Clean up checkpoint
    if Path(CHECKPOINT_PATH).exists():
        Path(CHECKPOINT_PATH).unlink()

    stage = f"Done! {total_inserted} new prospects added across {total} brands."
    print(f"\n✅ {stage}")
    if status_callback:
        status_callback(stage, 100)

    return total_inserted


if __name__ == "__main__":
    run()
