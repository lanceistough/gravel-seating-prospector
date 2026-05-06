"""
learn.py — Learn from your Yes/No approvals and re-score all prospects.

Reads your approved/rejected accounts from the sheet, trains a model on
what makes a good Gravel prospect, then updates ml_score for everyone.

Run this any time you've reviewed a batch of accounts:
    python3 learn.py
"""

import os
import re
import gspread
import pandas as pd
import numpy as np
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import cross_val_score

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","pnw","nyc","sf","atl","pdx","sea","chi",
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
}


def get_sheet():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    return client.open_by_key(sheet_id).sheet1


def extract_features(row: dict) -> dict:
    """Turn a sheet row into numeric features for the model."""
    bio = (row.get("bio") or "").lower()
    followers = int(row.get("followers") or 0)
    following = int(row.get("following") or 0)
    posts = int(row.get("posts_count") or 0)
    keywords_matched = len(str(row.get("bio_keywords_matched") or "").split(",")) if row.get("bio_keywords_matched") else 0
    source_brand_count = int(row.get("source_brand_count") or 0)

    # Follower tiers
    is_micro = 1 if 5000 <= followers <= 50000 else 0
    is_nano = 1 if 2000 <= followers < 5000 else 0
    is_mid = 1 if 50000 < followers <= 150000 else 0

    # Engagement proxy (posts relative to followers)
    engagement_proxy = posts / followers if followers > 0 else 0

    # Following ratio (high following/followers = less organic)
    follow_ratio = following / followers if followers > 0 else 0

    # US signals
    us_signal = int(
        "🇺🇸" in (row.get("bio") or "")
        or "usa" in bio
        or "united states" in bio
        or any(f" {s} " in f" {bio} " or bio.endswith(s) for s in US_STATES)
    )

    # Has external URL
    has_url = 1 if row.get("external_url") else 0

    # Is business account
    is_business = 1 if str(row.get("is_business", "")).lower() in ("true", "1", "yes") else 0

    # Bio length (more words = more active)
    bio_word_count = len(bio.split())

    # Has email in bio
    has_email = 1 if re.search(r"[\w.]+@[\w.]+", bio) else 0

    return {
        "followers": followers,
        "following": following,
        "posts_count": posts,
        "is_micro": is_micro,
        "is_nano": is_nano,
        "is_mid": is_mid,
        "engagement_proxy": engagement_proxy,
        "follow_ratio": follow_ratio,
        "keywords_matched": keywords_matched,
        "source_brand_count": source_brand_count,
        "us_signal": us_signal,
        "has_url": has_url,
        "is_business": is_business,
        "bio_word_count": bio_word_count,
        "has_email": has_email,
    }


def train_model(df_labeled: pd.DataFrame):
    """Train on 0-5 rated accounts."""
    feature_rows = [extract_features(row) for _, row in df_labeled.iterrows()]
    X = pd.DataFrame(feature_rows)
    y = df_labeled["rating"].astype(float)  # 0-5 continuous target

    from sklearn.ensemble import GradientBoostingRegressor
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingRegressor(n_estimators=100, random_state=42)),
    ])

    if len(df_labeled) >= 10:
        from sklearn.model_selection import cross_val_score
        scores = cross_val_score(model, X, y, cv=min(5, len(df_labeled) // 2), scoring="r2")
        print(f"  Model fit (R²): {scores.mean():.2f} ± {scores.std():.2f}")
    else:
        print(f"  Note: only {len(df_labeled)} rated examples — model will improve as you review more")

    model.fit(X, y)
    return model


def update_ml_scores(worksheet, all_rows: list, model):
    """Score all unreviewed accounts and write ml_score back to sheet."""
    headers = list(all_rows[0].keys()) if all_rows else []
    if "ml_score" not in headers:
        print("Warning: ml_score column not found")
        return

    col_idx = headers.index("ml_score") + 1

    updates = []
    for i, row in enumerate(all_rows):
        features = extract_features(row)
        X = pd.DataFrame([features])
        pred = model.predict(X)[0]  # predicted rating 0-5
        ml_score = round(max(0, min(100, pred * 20)))
        sheet_row = i + 2
        updates.append({"range": gspread.utils.rowcol_to_a1(sheet_row, col_idx), "values": [[ml_score]]})

    # Batch update
    worksheet.spreadsheet.values_batch_update({
        "valueInputOption": "RAW",
        "data": [{"range": u["range"], "values": u["values"]} for u in updates]
    })
    print(f"✓ Updated ml_score for {len(updates)} accounts")


def main():
    print("=" * 50)
    print("GRAVEL PROSPECT LEARNER")
    print("=" * 50)

    worksheet = get_sheet()
    all_rows = worksheet.get_all_records()
    df = pd.DataFrame(all_rows)

    # Mark brand-flagged rows as 0 for training, then remove them from sheet
    brand_mask = df["notes"].str.lower().str.contains("brand", na=False)
    brands_removed = brand_mask.sum()
    if brands_removed > 0:
        print(f"\nFound {brands_removed} brand accounts — using as negative training examples then removing...")
        df.loc[brand_mask, "approved"] = "0"

    # Convert approved to numeric 0-5 (also handle legacy yes/no)
    def parse_rating(val):
        val = str(val).strip().lower()
        if val in ("yes", "y"):
            return 3  # treat old yes as a 3
        if val in ("no", "n"):
            return 0
        try:
            r = int(float(val))
            return r if 0 <= r <= 5 else None
        except:
            return None

    df["rating"] = df["approved"].apply(parse_rating)
    labeled = df[df["rating"].notna()]

    rated_counts = labeled["rating"].value_counts().sort_index()
    print(f"\nRatings so far: {dict(rated_counts)}")

    if len(labeled) < 5:
        print("Need at least 5 ratings before the model can learn.")
        return

    if labeled["rating"].nunique() < 2:
        print("Need at least two different rating values to train. Keep reviewing!")
        return

    print("\nTraining model...")
    model = train_model(labeled)

    # Now remove brand rows from sheet
    if brands_removed > 0:
        df = df[~brand_mask].reset_index(drop=True)
        all_rows = df.to_dict("records")

    print("\nScoring all accounts...")
    update_ml_scores(worksheet, all_rows, model)

    # Sort: high ratings first, then unlabeled by ml_score, then 0s at bottom
    print("\nResorting sheet — 0s to the bottom, high ratings to the top...")
    df = pd.DataFrame(worksheet.get_all_records())

    def sort_key(row):
        val = str(row.get("approved", "")).strip().lower()
        if val in ("0", "no", "n"):
            return 2  # bottom
        elif val in ("", "none"):
            return 1  # middle, sorted by ml_score
        else:
            return 0  # rated 1-5, sorted by rating desc

    df["_sort"] = df.apply(sort_key, axis=1)
    df["_rating"] = pd.to_numeric(df["approved"], errors="coerce").fillna(0)
    df["_ml"] = pd.to_numeric(df["ml_score"], errors="coerce").fillna(0)
    df = df.sort_values(by=["_sort", "_rating", "_ml"], ascending=[True, False, False]).drop(columns=["_sort", "_rating", "_ml"])
    df["ml_score"] = pd.to_numeric(df["ml_score"], errors="coerce").fillna("").astype(str).str.replace(".0", "", regex=False)

    worksheet.clear()
    worksheet.update([df.columns.tolist()] + df.fillna("").values.tolist())

    # Show what the model learned
    feature_names = list(extract_features(all_rows[0]).keys())
    importances = model.named_steps["clf"].feature_importances_
    top = sorted(zip(feature_names, importances), key=lambda x: -x[1])[:5]
    print("\nTop signals the model found in your Yes picks:")
    for feat, imp in top:
        print(f"  {feat}: {imp:.2%}")

    print("\nDone! Sheet sorted: 5→4→3→2→1 (rated) → unrated (by ml_score) → 0 (bottom).")


if __name__ == "__main__":
    main()
