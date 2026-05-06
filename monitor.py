"""
monitor.py — Check if accounts you've sent product to have posted about Gravel.

Looks at anyone in the sheet with product_sent filled in, scrapes their recent
posts via Apify, and flags them in the sheet if they've mentioned Gravel.
"""

import os
import gspread
import pandas as pd
from apify_client import ApifyClient
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

GRAVEL_SIGNALS = [
    "gravel", "workbygravel", "@gravel.co", "#gravel",
    "gravelchair", "gravelseat", "gravelstudio",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    client = get_gspread_client()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.sheet1


def get_accounts_to_monitor(worksheet):
    rows = worksheet.get_all_records()
    # Anyone who has been sent product but hasn't been flagged as posted yet
    to_check = [
        row for row in rows
        if row.get("product_sent") and row.get("posted_about_gravel", "") != "✓ Posted"
    ]
    print(f"Found {len(to_check)} accounts to monitor")
    return to_check, rows


def check_for_gravel_posts(usernames: list[str]) -> set[str]:
    """Scrape recent posts from these accounts and look for Gravel mentions."""
    if not usernames:
        return set()

    token = os.environ.get("APIFY_API_TOKEN")
    client = ApifyClient(token)

    urls = [f"https://www.instagram.com/{u}/" for u in usernames]

    print(f"Scraping recent posts for {len(usernames)} accounts...")
    run = client.actor("apify/instagram-scraper").call(
        run_input={
            "directUrls": urls,
            "resultsType": "posts",
            "resultsLimit": 12,  # Last ~12 posts per account
        }
    )

    posts = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"Pulled {len(posts)} posts")

    mentioned = set()
    for post in posts:
        caption = (post.get("caption") or "").lower()
        username = (
            post.get("ownerUsername")
            or post.get("owner", {}).get("username", "")
        ).lower()

        if any(signal in caption for signal in GRAVEL_SIGNALS):
            mentioned.add(username)
            print(f"  ✓ {username} posted about Gravel!")

    return mentioned


def update_sheet(worksheet, all_rows: list, mentioned: set[str]):
    if not mentioned:
        print("No new Gravel posts found.")
        return

    # Find the column index for posted_about_gravel
    headers = list(all_rows[0].keys()) if all_rows else []
    if "posted_about_gravel" not in headers:
        print("Warning: posted_about_gravel column not found in sheet")
        return

    col_idx = headers.index("posted_about_gravel") + 1  # 1-indexed

    updated = 0
    for i, row in enumerate(all_rows):
        if row.get("username", "").lower() in mentioned:
            sheet_row = i + 2  # +1 for header, +1 for 1-indexing
            worksheet.update_cell(sheet_row, col_idx, "✓ Posted")
            updated += 1

    print(f"✓ Updated {updated} accounts in sheet")


def main():
    print("=" * 50)
    print("GRAVEL POST MONITOR")
    print("=" * 50)

    worksheet = get_sheet()
    to_check, all_rows = get_accounts_to_monitor(worksheet)

    if not to_check:
        print("No accounts to monitor yet. Send some product first!")
        return

    usernames = [row["username"] for row in to_check]
    mentioned = check_for_gravel_posts(usernames)
    update_sheet(worksheet, all_rows, mentioned)


if __name__ == "__main__":
    main()
