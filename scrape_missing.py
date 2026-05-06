"""
scrape_missing.py — Scrape only the brands that were missed in the last run.
Merges results into existing raw_accounts.json without touching anything else.
"""

import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv
from apify_client import ApifyClient
from tqdm import tqdm

load_dotenv()

MISSING_BRANDS = [
    "on",
    "mountainhardwear",
    "outdoorresearch",
    "rab.equipment",
    "mizuno_sportstyle",
    "9.9journey",
    "goretexstudio",
]

POSTS_PER_BRAND = 1000
RAW_PATH = "data/raw_accounts.json"


def main():
    token = os.environ.get("APIFY_API_TOKEN")
    client = ApifyClient(token)

    # Load existing accounts
    with open(RAW_PATH) as f:
        all_accounts = {a["username"]: a for a in json.load(f)}

    print(f"Starting with {len(all_accounts)} existing accounts")

    for brand in tqdm(MISSING_BRANDS, desc="Scraping missing brands"):
        tagged_url = f"https://www.instagram.com/{brand}/tagged/"
        print(f"\n→ @{brand}")

        run = client.actor("apify/instagram-scraper").call(
            run_input={
                "directUrls": [tagged_url],
                "resultsType": "posts",
                "resultsLimit": POSTS_PER_BRAND,
                "skipPinnedPosts": False,
            }
        )

        posts = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        print(f"   Found {len(posts)} tagged posts")

        handles = set()
        for post in posts:
            owner = (
                post.get("ownerUsername")
                or post.get("owner", {}).get("username")
                or post.get("coauthorProducers", [{}])[0].get("username")
            )
            if owner:
                handles.add(str(owner).lower())

        print(f"   Unique posters: {len(handles)}")
        if not handles:
            continue

        profile_run = client.actor("apify/instagram-profile-scraper").call(
            run_input={"usernames": list(handles)}
        )
        profiles = list(client.dataset(profile_run["defaultDatasetId"]).iterate_items())

        new_count = 0
        for profile in profiles:
            username = profile.get("username", "").lower()
            if not username:
                continue
            if username not in all_accounts:
                all_accounts[username] = {
                    "username": username,
                    "full_name": profile.get("fullName", ""),
                    "bio": profile.get("biography", ""),
                    "followers": profile.get("followersCount", 0),
                    "following": profile.get("followsCount", 0),
                    "posts_count": profile.get("postsCount", 0),
                    "profile_url": f"https://www.instagram.com/{username}/",
                    "external_url": profile.get("externalUrl", ""),
                    "is_business": profile.get("isBusinessAccount", False),
                    "source_brands": [brand],
                }
                new_count += 1
            else:
                if brand not in all_accounts[username]["source_brands"]:
                    all_accounts[username]["source_brands"].append(brand)

        print(f"   New accounts added: {new_count}")
        time.sleep(2)

    results = list(all_accounts.values())
    with open(RAW_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n✓ Total accounts: {len(results)} — saved to {RAW_PATH}")
    print("\nNow run: python3 run.py --skip-scrape")


if __name__ == "__main__":
    main()
