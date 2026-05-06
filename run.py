"""
run.py — One command to run the full Gravel Seating Prospector pipeline.

Usage:
    python run.py                   # Full run: scrape → score → export
    python run.py --skip-scrape     # Re-score + export from existing raw data
    python run.py --skip-export     # Scrape + score, save CSV only
"""

import argparse
import sys
from pathlib import Path

from scrape import scrape_tagged_accounts, save_raw, load_config
from score import score_all, save_scored
from export import export_to_sheets


def main():
    parser = argparse.ArgumentParser(description="Gravel Seating Prospector")
    parser.add_argument("--skip-scrape", action="store_true", help="Use existing raw data")
    parser.add_argument("--skip-export", action="store_true", help="Don't push to Google Sheets")
    args = parser.parse_args()

    config = load_config()

    print("=" * 60)
    print("GRAVEL SEATING PROSPECTOR")
    print("=" * 60)

    # Step 1: Scrape
    if not args.skip_scrape:
        print("\n[1/3] SCRAPING INSTAGRAM TAGGED POSTS")
        print(f"  Target brands: {len(config['target_brands'])}")
        print(f"  Posts per brand: {config['posts_per_brand']}")
        accounts = scrape_tagged_accounts(config)
        save_raw(accounts)
    else:
        raw_path = Path("data/raw_accounts.json")
        if not raw_path.exists():
            print("ERROR: No raw data found. Run without --skip-scrape first.")
            sys.exit(1)
        print("\n[1/3] SKIPPING SCRAPE — using existing raw data")

    # Step 2: Score & filter
    print("\n[2/3] SCORING AND FILTERING")
    print(f"  Follower range: {config['min_followers']:,} – {config['max_followers']:,}")
    print(f"  Min engagement: {config['min_engagement_rate']*100:.0f}%")
    df = score_all()
    save_scored(df)

    if df.empty:
        print("\n  No accounts passed filters — nothing to export.")
        print("\n" + "=" * 60)
        print("Total prospects ready for outreach: 0")
        print("=" * 60)
        return

    print(f"\n  Top 10 prospects:")
    preview_cols = ["username", "followers", "score", "source_brands_str"]
    preview_cols = [c for c in preview_cols if c in df.columns]
    print(df[preview_cols].head(10).to_string(index=False))

    # Step 3: Export to Google Sheets
    if not args.skip_export:
        print("\n[3/3] EXPORTING TO GOOGLE SHEETS")
        url = export_to_sheets(df)
        print(f"\n✓ DONE. Open your sheet: {url}")
    else:
        print("\n[3/3] SKIPPING EXPORT — CSV saved to data/scored_accounts.csv")
        print("\n✓ DONE.")

    print("\n" + "=" * 60)
    print(f"Total prospects ready for outreach: {len(df)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
