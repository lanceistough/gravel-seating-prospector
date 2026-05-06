"""
export.py — Push scored prospects to Google Sheets for outreach tracking.

Sheet columns:
Username | Full Name | Bio | Followers | Profile URL | External URL |
Source Brands | Bio Keywords | Score | Outreach Status | Address Collected |
Product Sent | Notes
"""

import os
import json
import yaml
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_COLOR = {"red": 0.13, "green": 0.13, "blue": 0.13}  # Near-black
HEADER_TEXT_COLOR = {"red": 1.0, "green": 1.0, "blue": 1.0}  # White


def get_gspread_client():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    if not Path(sa_path).exists():
        raise FileNotFoundError(
            f"Google service account JSON not found at {sa_path}.\n"
            "See README for setup instructions."
        )
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return gspread.authorize(creds)


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def export_to_sheets(df: pd.DataFrame = None, csv_path: str = "data/scored_accounts.csv"):
    if df is None:
        df = pd.read_csv(csv_path)

    config = load_config()
    sheet_name = config["google_sheet_name"]

    client = get_gspread_client()

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    # Open by ID if provided, otherwise open/create by name
    if sheet_id:
        spreadsheet = client.open_by_key(sheet_id)
        print(f"→ Opening sheet by ID: {sheet_id}")
        worksheet = spreadsheet.sheet1
    else:
        try:
            spreadsheet = client.open(sheet_name)
            print(f"→ Opening existing sheet: {sheet_name}")
            worksheet = spreadsheet.sheet1
        except gspread.SpreadsheetNotFound:
            raise RuntimeError(
                f"Sheet '{sheet_name}' not found and GOOGLE_SHEET_ID is not set.\n\n"
                "To fix:\n"
                "  1. Create a Google Sheet at sheets.google.com\n"
                f"  2. Share it with: ig-product-seeding@claude-code-gravel-reports.iam.gserviceaccount.com (Editor)\n"
                "  3. Copy the sheet ID from the URL (the long string between /d/ and /edit)\n"
                "  4. Add GOOGLE_SHEET_ID=<that-id> to your .env file\n"
            )

    # Load existing sheet data BEFORE clearing so we can preserve outreach columns
    OUTREACH_COLS = ["approved", "outreach_status", "address_collected", "product_sent", "notes", "posted_about_gravel", "ml_score"]
    existing_rows = worksheet.get_all_records()
    existing = {str(row["username"]).lower(): row for row in existing_rows if row.get("username")}
    print(f"→ Found {len(existing)} existing accounts to preserve")

    worksheet.clear()

    df = df.fillna("")

    # Ensure outreach columns exist in df
    for col in OUTREACH_COLS:
        if col not in df.columns:
            df[col] = ""

    # Restore outreach data from existing sheet row by row
    for idx, row in df.iterrows():
        username = str(row["username"]).lower()
        existing_row = existing.get(username, {})
        for col in OUTREACH_COLS:
            existing_val = str(existing_row.get(col, "")).strip()
            if existing_val:
                df.at[idx, col] = existing_val

    # Only keep existing rows that the user has explicitly rated — drop unreviewed old accounts
    new_usernames = set(df["username"].str.lower())
    for username, row in existing.items():
        if username.lower() not in new_usernames:
            rating = str(row.get("approved", "")).strip()
            if rating and rating != "0":  # has a non-zero rating = user reviewed it
                new_row = {c: row.get(c, "") for c in df.columns}
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    headers = list(df.columns)
    rows = df.values.tolist()

    worksheet.update([headers] + rows)

    # Format header row
    worksheet.format("1:1", {
        "backgroundColor": HEADER_COLOR,
        "textFormat": {
            "foregroundColor": HEADER_TEXT_COLOR,
            "bold": True,
            "fontSize": 10,
        },
    })

    # Freeze header row
    spreadsheet.batch_update({
        "requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        }]
    })

    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}"
    print(f"✓ Exported {len(df)} prospects to Google Sheets")
    print(f"  URL: {sheet_url}")
    return sheet_url


if __name__ == "__main__":
    export_to_sheets()
