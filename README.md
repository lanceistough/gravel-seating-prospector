# Gravel Seating Prospector

Finds Instagram creators who have tagged lookalike brands (Arc'teryx, Rains, Cotopaxi, etc.) and outputs a scored, outreach-ready list to Google Sheets.

---

## How it works

1. **Scrape** — For each brand in `config.yaml`, Apify pulls their recent tagged posts and extracts the poster's profile data
2. **Score** — Filters by follower range + engagement, scores by bio keywords, category loyalty (tagged multiple brands), and creator signals
3. **Export** — Pushes a ranked list to Google Sheets with outreach tracking columns

---

## Setup (one time)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get your Apify API token
1. Sign up at [apify.com](https://apify.com) (~$49/mo plan covers this)
2. Go to Settings → Integrations → API token
3. Copy the token

### 3. Set up Google Sheets access
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**
4. Create a **Service Account** (IAM & Admin → Service Accounts)
5. Download the JSON key file → save as `service_account.json` in this folder
6. That's it — the script will create the Sheet automatically

### 4. Configure your .env
```bash
cp .env.example .env
```
Edit `.env` and add:
```
APIFY_API_TOKEN=apify_api_xxxxxxxxxxxxxxxx
GOOGLE_SERVICE_ACCOUNT_JSON=./service_account.json
```

### 5. Customize config.yaml
- Add/remove brands from `target_brands`
- Adjust `min_followers` / `max_followers` for your tier preference
- Add bio keywords relevant to Gravel's customer profile

---

## Running it

```bash
# Full pipeline (scrape + score + export to Sheets)
python run.py

# Re-score without re-scraping (useful for tuning filters)
python run.py --skip-scrape

# Scrape + score but skip Sheets (just get the CSV)
python run.py --skip-export
```

---

## Output

Your Google Sheet will have these columns:

| Column | What it is |
|--------|------------|
| username | Instagram handle |
| full_name | Display name |
| bio | Profile bio |
| followers | Follower count |
| profile_url | Direct link to their IG |
| external_url | Link in bio (often email/website) |
| source_brands_str | Which lookalike brands they've tagged |
| source_brand_count | How many (higher = more category-loyal) |
| bio_keywords_matched | Which of your keywords matched |
| score | Overall prospect score (higher = better fit) |
| outreach_status | Track: Not Contacted / DMed / Responded / Sending Product / Done |
| address_collected | Shipping address once they respond |
| product_sent | Which product you sent |
| notes | Anything else |

---

## Cost estimate (one month)

| Tool | Est. cost |
|------|-----------|
| Apify Starter | $49/mo |
| Google Sheets API | Free |
| **Total** | **~$49** |

Apify charges per compute unit. For 10 brands × 200 posts = ~2,000 profiles enriched, expect to use roughly $5–15 of compute on the $49 plan. You have plenty of runway to run it multiple times.

---

## Tweaking the scoring

Edit `score.py` → `score_account()` to adjust point values. Current weights:

- **Follower tier** (micro 5K-50K): +30 pts
- **Bio keyword match**: +8 pts per keyword
- **Tagged 3+ lookalike brands**: +20 pts
- **Tagged 2 lookalike brands**: +10 pts  
- **Has external URL**: +5 pts
- **Business/creator account**: +5 pts
