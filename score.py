"""
score.py — Filter and score raw accounts against Gravel's criteria.

Scoring factors:
- Follower range (hard filter)
- Engagement rate (hard filter)  
- Bio keyword match (bonus points)
- Tagged multiple lookalike brands (bonus — signals genuine interest in category)
- Has external URL (slight bonus — more likely a real creator)
"""

import json
import re
import yaml
import pandas as pd
from pathlib import Path
from typing import Optional

# Non-US country flags (emoji) and strong non-US signals
NON_US_FLAGS = {
    "🇬🇧","🇨🇦","🇦🇺","🇩🇪","🇫🇷","🇯🇵","🇰🇷","🇨🇳","🇧🇷","🇲🇽","🇮🇳","🇮🇹","🇪🇸",
    "🇳🇱","🇸🇪","🇳🇴","🇩🇰","🇨🇭","🇦🇹","🇧🇪","🇵🇹","🇵🇱","🇷🇺","🇺🇦","🇹🇷","🇸🇦",
    "🇦🇪","🇿🇦","🇳🇿","🇸🇬","🇹🇭","🇻🇳","🇵🇭","🇮🇩","🇲🇾","🇵🇪","🇨🇴","🇨🇱","🇦🇷",
    "🇪🇨","🇬🇹","🇸🇻","🇭🇳","🇳🇮","🇨🇷","🇵🇦","🇩🇴","🇵🇷","🇨🇺",
}

US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    # Common abbreviations
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","pnw","nyc","la","sf","atl","pdx","sea","chi",
}

US_CITIES = {
    "new york","los angeles","chicago","houston","phoenix","philadelphia",
    "san antonio","san diego","dallas","san jose","austin","jacksonville",
    "fort worth","columbus","charlotte","san francisco","indianapolis","seattle",
    "denver","nashville","portland","boston","las vegas","memphis","louisville",
    "baltimore","milwaukee","albuquerque","tucson","fresno","sacramento",
    "mesa","kansas city","atlanta","omaha","colorado springs","raleigh",
    "miami","minneapolis","boise","salt lake city","denver","pittsburgh",
}


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def score_account(account: dict, config: dict) -> Optional[dict]:
    """
    Returns the account dict with a score field, or None if it fails hard filters.
    """
    followers = account.get("followers", 0)
    
    # Hard filter: follower range
    if followers < config["min_followers"] or followers > config["max_followers"]:
        return None

    # Hard filter: drop accounts with heavy non-Latin scripts (CJK etc.) — raise threshold to 50%
    bio_raw = account.get("bio") or ""
    non_latin = sum(1 for c in bio_raw if ord(c) > 0x2E7F and c not in NON_US_FLAGS and not (0x1F1E0 <= ord(c) <= 0x1F1FF))
    if len(bio_raw) > 0 and non_latin / len(bio_raw) > 0.5:
        return None

    # Soft filter: non-US flag = score penalty instead of hard drop
    has_non_us_flag = any(flag in bio_raw for flag in NON_US_FLAGS)

    # Soft scoring: US location signals
    bio_lower = bio_raw.lower()
    us_signals = (
        "🇺🇸" in bio_raw
        or "usa" in bio_lower
        or "united states" in bio_lower
        or any(f" {s} " in f" {bio_lower} " or bio_lower.endswith(s) for s in US_STATES)
        or any(city in bio_lower for city in US_CITIES)
    )

    # Engagement rate — Instagram doesn't expose likes directly on profile,
    # so we estimate from posts_count and followers as a proxy.
    # Real engagement is pulled per-post; this is a rough gate.
    # If we have it, use it. Otherwise skip the hard filter here.
    engagement_rate = account.get("engagement_rate")
    if engagement_rate is not None:
        if engagement_rate < config["min_engagement_rate"]:
            return None

    bio_lower_check = (account.get("bio") or "").lower()
    full_name_lower = (account.get("full_name") or "").lower()
    username = (account.get("username") or "").lower()
    target_brands_lower = [b.lower().replace("_official", "").replace("official", "") for b in config.get("target_brands", [])]

    # Hard filter: drop the source brands themselves
    if username in target_brands_lower or username in [b.replace("_", "") for b in target_brands_lower]:
        return None

    # Hard filter: business account + follows almost nobody = brand, not a person
    following = account.get("following", 0)
    follow_ratio = following / followers if followers > 0 else 0
    if account.get("is_business") and follow_ratio < 0.05:
        return None

    # Hard filter: even without is_business flag, very low follow_ratio + high follower
    # count is a strong brand signal (brands don't follow back)
    if followers > 5000 and follow_ratio < 0.02:
        return None

    # Hard filter: drop clear brand/business/media accounts
    hard_brand_signals = [
        "official account", "official page", "official store", "official shop",
        "wholesale", "retailer", "distributor", "free shipping", "use code",
        "dm to order", "dm for orders", "shop now", "new collection", "new arrivals",
        "editor in chief", "editor-in-chief", "subscribe to", "our magazine",
        "llc", "inc.", "ltd", "gmbh",
        # German/French brand signals
        "onlineshop", "online shop", "online store", "versand", "bestellung",
        "kollektion", "collection officielle", "boutique officielle",
        "gibt's", "gibt es", "erhältlich", "jetzt kaufen", "jetzt shoppen",
        "shop und", "und shop", "link im bio", "link in bio", "link in der bio",
    ]
    if any(s in bio_lower_check for s in hard_brand_signals):
        return None

    # Hard filter: drop accounts whose username looks like a brand/shop/media
    username_brand_signals = [
        "shop", "store", "official", "outfitters", "outfitter",
        "apparel", "clothing", "magazine", "journal",
        "news", "press", "podcast", "brand", "community",
        "institute", "istituto", "academy", "school", "university",
        "global", "worldwide", "international",
    ]
    username_parts = re.split(r'[_.\-]', username)
    if any(s in username_parts for s in username_brand_signals):
        return None

    # Hard filter: regional brand accounts (username ends in country/region suffix)
    # e.g. mammut.france, gramicci_eu, nikeacg_au
    regional_suffixes = {
        "france", "germany", "uk", "eu", "usa", "canada", "australia", "au",
        "de", "fr", "it", "es", "nl", "se", "no", "dk", "ch", "at", "be",
        "nordic", "scandinavia", "europe", "asia", "latam", "brasil", "japan",
    }
    if username_parts[-1] in regional_suffixes and len(username_parts) > 1:
        return None

    # Scoring
    score = 0

    # Follower tier score (sweet spot = micro-influencer 5K-50K)
    if 5000 <= followers <= 50000:
        score += 30
    elif 2000 <= followers < 5000:
        score += 15
    elif 50000 < followers <= 150000:
        score += 10

    # Bio keyword match
    bio = (account.get("bio") or "").lower()
    keywords_matched = [
        kw for kw in config["bio_keywords"] if kw.lower() in bio
    ]
    score += len(keywords_matched) * 8
    account["bio_keywords_matched"] = ", ".join(keywords_matched)

    # Snow/ski penalty — not Gravel's customer
    snow_keywords = [
        "ski", "skiing", "skier", "snowboard", "snowboarding", "snowboarder",
        "snow", "backcountry skiing", "powder", "après ski", "apres ski",
        "ski patrol", "ski instructor", "ski coach", "freeski",
    ]
    snow_matches = [kw for kw in snow_keywords if kw in bio]
    if snow_matches:
        score -= len(snow_matches) * 10

    # Tagged multiple lookalike brands (category-loyal)
    source_brands = account.get("source_brands", [])
    if len(source_brands) >= 3:
        score += 20
    elif len(source_brands) == 2:
        score += 10

    # Has external URL (often means active creator)
    if account.get("external_url"):
        score += 5

    # US location signal in bio = boost, non-US flag = penalty
    if us_signals:
        score += 15
    if has_non_us_flag:
        score -= 20

    # Is a business/creator account
    if account.get("is_business"):
        score += 5

    account["score"] = score
    account["source_brands_str"] = ", ".join(source_brands)
    account["source_brand_count"] = len(source_brands)

    return account


def score_all(raw_path: str = "data/raw_accounts.json") -> pd.DataFrame:
    with open(raw_path) as f:
        accounts = json.load(f)

    config = load_config()

    scored = []
    filtered_out = 0

    for account in accounts:
        result = score_account(account, config)
        if result is None:
            filtered_out += 1
        else:
            scored.append(result)

    print(f"✓ Passed filters: {len(scored)} accounts")
    print(f"  Filtered out:   {filtered_out} accounts")

    if not scored:
        print("  No accounts passed filters — returning empty dataframe.")
        return pd.DataFrame()

    df = pd.DataFrame(scored)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    # Clean up columns for export
    keep_cols = [
        "username", "full_name", "bio", "followers", "following",
        "posts_count", "profile_url", "external_url", "is_business",
        "source_brands_str", "source_brand_count", "bio_keywords_matched",
        "engagement_rate", "score",
    ]
    # Only keep columns that exist
    df = df[[c for c in keep_cols if c in df.columns]]

    # Add outreach tracking columns
    df["outreach_status"] = "Not Contacted"
    df["address_collected"] = ""
    df["product_sent"] = ""
    df["notes"] = ""

    return df


def save_scored(df: pd.DataFrame, path: str = "data/scored_accounts.csv"):
    Path("data").mkdir(exist_ok=True)
    df.to_csv(path, index=False)
    print(f"✓ Saved scored prospects to {path}")
    return df


if __name__ == "__main__":
    df = score_all()
    save_scored(df)
    print(f"\nTop 10 prospects:")
    print(df[["username", "followers", "score", "bio_keywords_matched"]].head(10).to_string())
