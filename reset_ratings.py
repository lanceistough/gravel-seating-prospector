"""
reset_ratings.py — Sets all empty approved values to 0.
Only touches rows where approved is blank — won't overwrite anything you've already rated.
"""

import os
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
client = gspread.authorize(creds)
sheet_id = os.environ.get("GOOGLE_SHEET_ID")
worksheet = client.open_by_key(sheet_id).sheet1

rows = worksheet.get_all_records()
headers = worksheet.row_values(1)
approved_col = headers.index("approved") + 1

updates = []
for i, row in enumerate(rows):
    if str(row.get("approved", "")).strip() == "":
        updates.append({
            "range": gspread.utils.rowcol_to_a1(i + 2, approved_col),
            "values": [["0"]]
        })

if updates:
    worksheet.spreadsheet.values_batch_update({
        "valueInputOption": "RAW",
        "data": [{"range": u["range"], "values": u["values"]} for u in updates]
    })
    print(f"✓ Set {len(updates)} blank ratings to 0")
else:
    print("No blank ratings found.")
