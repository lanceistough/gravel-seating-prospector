"""
swipe.py — Tinder-style swipe UI for rating prospects.

Run with: python3 swipe.py
Then open: http://localhost:5000
"""

import os
import json
import gspread
import pandas as pd
from flask import Flask, render_template_string, jsonify, request
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "scored_accounts.csv")


def get_sheet():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).sheet1


def load_prospects():
    df = pd.read_csv(CSV_PATH)
    df = df.fillna("")

    # Merge in recent_images and profile_pic from raw data
    raw_path = os.path.join(os.path.dirname(__file__), "data", "raw_accounts.json")
    if os.path.exists(raw_path):
        with open(raw_path) as f:
            raw = {a["username"]: a for a in json.load(f)}
        df["recent_images"] = df["username"].apply(lambda u: raw.get(u, {}).get("recent_images", []))
        df["profile_pic"] = df["username"].apply(lambda u: raw.get(u, {}).get("profile_pic", ""))
    else:
        df["recent_images"] = [[] for _ in range(len(df))]
        df["profile_pic"] = ""

    # Pull approved ratings from the Google Sheet
    approved_map = {}
    try:
        worksheet = get_sheet()
        sheet_rows = worksheet.get_all_records()
        approved_map = {str(r["username"]).lower(): r.get("approved", "") for r in sheet_rows if r.get("username")}
    except Exception as e:
        print(f"Could not load ratings from sheet: {e}")
    df["approved"] = df["username"].apply(lambda u: approved_map.get(str(u).lower(), ""))

    # Show unrated first, then sort by score
    df["_rated"] = df["approved"].apply(lambda x: 0 if str(x).strip() == "" else 1)
    df = df.sort_values(["_rated", "score"], ascending=[True, False]).drop(columns=["_rated"])
    return df.to_dict("records")


def sync_to_sheet(username, rating):
    """Background thread: push rating to Google Sheet."""
    try:
        worksheet = get_sheet()
        rows = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        approved_col = headers.index("approved") + 1
        for i, row in enumerate(rows):
            if row.get("username") == username:
                worksheet.update_cell(i + 2, approved_col, rating)
                break
    except Exception as e:
        print(f"Sheet sync error: {e}")


def save_rating(username, rating):
    # Save to CSV immediately (fast)
    df = pd.read_csv(CSV_PATH)
    df.loc[df["username"] == username, "approved"] = rating
    df.to_csv(CSV_PATH, index=False)

    # Sync to Google Sheet in background (non-blocking)
    import threading
    threading.Thread(target=sync_to_sheet, args=(username, rating), daemon=True).start()


HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Gravel Prospect Swiper</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f0f0f0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .image-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 3px;
      margin-top: 12px;
      border-radius: 8px;
      overflow: hidden;
    }
    .image-grid img {
      width: 100%;
      aspect-ratio: 1;
      object-fit: cover;
      display: block;
    }
    .image-grid-placeholder {
      background: #f5f5f5;
      aspect-ratio: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #ccc;
      font-size: 20px;
    }
    #header {
      position: fixed;
      top: 0; left: 0; right: 0;
      padding: 16px 24px;
      background: white;
      border-bottom: 1px solid #e0e0e0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      z-index: 100;
    }
    #header h1 { font-size: 18px; font-weight: 700; color: #111; }
    #counter { font-size: 14px; color: #888; }
    #card-container {
      position: relative;
      width: 380px;
      height: 640px;
      margin-top: 70px;
    }
    .card {
      position: absolute;
      width: 100%;
      height: 100%;
      background: white;
      border-radius: 16px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.12);
      padding: 28px;
      display: flex;
      flex-direction: column;
      cursor: grab;
      user-select: none;
      transition: transform 0.1s ease;
    }
    .card.dragging { cursor: grabbing; transition: none; }
    .card.fly-left {
      animation: flyLeft 0.3s ease forwards;
    }
    .card.fly-right {
      animation: flyRight 0.3s ease forwards;
    }
    @keyframes flyLeft {
      to { transform: translateX(-150%) rotate(-20deg); opacity: 0; }
    }
    @keyframes flyRight {
      to { transform: translateX(150%) rotate(20deg); opacity: 0; }
    }
    .card-avatar {
      width: 64px;
      height: 64px;
      border-radius: 50%;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      color: white;
      font-weight: 700;
      margin-bottom: 16px;
      flex-shrink: 0;
    }
    .card-name {
      font-size: 22px;
      font-weight: 700;
      color: #111;
      margin-bottom: 4px;
    }
    .card-handle {
      font-size: 14px;
      color: #888;
      margin-bottom: 12px;
    }
    .card-handle a {
      color: #0066cc;
      text-decoration: none;
    }
    .card-handle a:hover { text-decoration: underline; }
    .card-stats {
      display: flex;
      gap: 16px;
      margin-bottom: 14px;
    }
    .stat {
      display: flex;
      flex-direction: column;
    }
    .stat-value { font-size: 16px; font-weight: 700; color: #111; }
    .stat-label { font-size: 11px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
    .card-bio {
      font-size: 14px;
      color: #444;
      line-height: 1.5;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
    }
    .card-brands {
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .brand-tag {
      background: #f0f4ff;
      color: #3355aa;
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 20px;
      font-weight: 500;
    }
    .card-score {
      position: absolute;
      top: 16px;
      right: 16px;
      background: #111;
      color: white;
      font-size: 12px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 20px;
    }
    .overlay-label {
      position: absolute;
      top: 24px;
      font-size: 32px;
      font-weight: 900;
      padding: 6px 16px;
      border-radius: 8px;
      border: 4px solid;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.1s;
    }
    .overlay-yes {
      left: 20px;
      color: #22c55e;
      border-color: #22c55e;
      transform: rotate(-15deg);
    }
    .overlay-no {
      right: 20px;
      color: #ef4444;
      border-color: #ef4444;
      transform: rotate(15deg);
    }
    #buttons {
      display: flex;
      gap: 24px;
      margin-top: 28px;
      align-items: center;
    }
    .btn {
      width: 60px;
      height: 60px;
      border-radius: 50%;
      border: none;
      font-size: 24px;
      cursor: pointer;
      box-shadow: 0 2px 10px rgba(0,0,0,0.15);
      transition: transform 0.1s, box-shadow 0.1s;
    }
    .btn:hover { transform: scale(1.1); box-shadow: 0 4px 16px rgba(0,0,0,0.2); }
    .btn-no { background: white; }
    .btn-yes { background: white; }
    .btn-open {
      width: 44px;
      height: 44px;
      background: white;
      font-size: 18px;
    }
    #rating-bar {
      display: flex;
      gap: 8px;
      margin-top: 16px;
    }
    .rating-btn {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      border: 2px solid #ddd;
      background: white;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      color: #888;
      transition: all 0.15s;
    }
    .rating-btn:hover {
      border-color: #111;
      color: #111;
      transform: scale(1.1);
    }
    #done-screen {
      text-align: center;
      display: none;
    }
    #done-screen h2 { font-size: 28px; margin-bottom: 8px; }
    #done-screen p { color: #888; }
    #hint {
      margin-top: 12px;
      font-size: 12px;
      color: #bbb;
    }
  </style>
</head>
<body>

<div id="header">
  <h1>Gravel Prospects</h1>
  <span id="counter">Loading...</span>
</div>

<div id="card-container"></div>

<div id="buttons">
  <button class="btn btn-no" onclick="swipe('left')" title="Pass (0)">❌</button>
  <button class="btn btn-open" onclick="openProfile()" title="Open Instagram">🔗</button>
  <button class="btn btn-yes" onclick="swipe('right')" title="Approve">✅</button>
</div>

<div id="rating-bar">
  <button class="rating-btn" onclick="rate(1)" title="1">1</button>
  <button class="rating-btn" onclick="rate(2)" title="2">2</button>
  <button class="rating-btn" onclick="rate(3)" title="3">3</button>
  <button class="rating-btn" onclick="rate(4)" title="4">4</button>
  <button class="rating-btn" onclick="rate(5)" title="5">5</button>
</div>

<div id="hint">← / → arrow keys to swipe • 1-5 keys to rate</div>

<div id="done-screen">
  <h2>🎉 All done!</h2>
  <p>You've reviewed all prospects.<br>Run <code>python3 learn.py</code> to update ML scores.</p>
</div>

<script>
let prospects = [];
let current = 0;

function imageGrid(images) {
  if (!images || images.length === 0) return '';
  const cells = images.slice(0, 6).map(src =>
    `<img src="${src}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\"image-grid-placeholder\\">📷</div>'">`
  );
  // Pad to 6 with placeholders
  while (cells.length < 6) cells.push('<div class="image-grid-placeholder">📷</div>');
  return `<div class="image-grid">${cells.join('')}</div>`;
}

async function loadProspects() {
  const res = await fetch('/prospects');
  prospects = await res.json();
  showCard();
  updateCounter();
}

function formatFollowers(n) {
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return n;
}

let profileWindow = null;

function openProfileWindow(url) {
  const w = 680, h = window.screen.height;
  const left = window.screen.width - w;
  if (profileWindow && !profileWindow.closed) {
    profileWindow.location.href = url;
  } else {
    profileWindow = window.open(url, 'instagram_profile',
      `width=${w},height=${h},left=${left},top=0,scrollbars=yes`);
  }
}

function showCard() {
  const container = document.getElementById('card-container');
  container.innerHTML = '';

  if (current >= prospects.length) {
    container.style.display = 'none';
    document.getElementById('buttons').style.display = 'none';
    document.getElementById('rating-bar').style.display = 'none';
    document.getElementById('done-screen').style.display = 'block';
    return;
  }

  const p = prospects[current];
  const initial = (p.username || '?')[0].toUpperCase();
  const brands = (p.source_brands_str || '').split(',').filter(Boolean).map(b =>
    `<span class="brand-tag">${b.trim()}</span>`
  ).join('');

  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <div class="overlay-label overlay-yes" id="overlay-yes">LIKE</div>
    <div class="overlay-label overlay-no" id="overlay-no">PASS</div>
    <div class="card-score">Score: ${p.score || 0}</div>
    <div class="card-avatar">${initial}</div>
    <div class="card-name">${p.full_name || p.username}</div>
    <div class="card-handle"><a href="${p.profile_url}" target="_blank">@${p.username}</a></div>
    <div class="card-stats">
      <div class="stat">
        <span class="stat-value">${formatFollowers(p.followers)}</span>
        <span class="stat-label">Followers</span>
      </div>
      <div class="stat">
        <span class="stat-value">${formatFollowers(p.following)}</span>
        <span class="stat-label">Following</span>
      </div>
      <div class="stat">
        <span class="stat-value">${p.posts_count || 0}</span>
        <span class="stat-label">Posts</span>
      </div>
    </div>
    <div class="card-bio">${p.bio || 'No bio'}</div>
    <div class="card-brands">${brands}</div>
    ${imageGrid(p.recent_images)}
  `;

  setupDrag(card);
  container.appendChild(card);

  // Auto-open their Instagram profile in a side window
  openProfileWindow(p.profile_url);
}

function updateCounter() {
  const unrated = prospects.filter(p => !p.approved || p.approved === '').length;
  document.getElementById('counter').textContent = `${current}/${prospects.length} reviewed`;
}

function openProfile() {
  if (current < prospects.length) {
    window.open(prospects[current].profile_url, '_blank');
  }
}

async function rate(value) {
  if (current >= prospects.length) return;
  const p = prospects[current];
  await fetch('/rate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: p.username, rating: value})
  });
  const card = document.querySelector('.card');
  if (card) {
    card.classList.add(value >= 3 ? 'fly-right' : 'fly-left');
    setTimeout(() => { current++; showCard(); updateCounter(); }, 280);
  }
}

function swipe(direction) {
  rate(direction === 'right' ? 4 : 0);
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') swipe('left');
  else if (e.key === 'ArrowRight') swipe('right');
  else if (e.key === 'ArrowUp') openProfile();
  else if (['1','2','3','4','5'].includes(e.key)) rate(parseInt(e.key));
});

// Drag to swipe
function setupDrag(card) {
  let startX, startY, currentX;
  const overlayYes = card.querySelector('#overlay-yes');
  const overlayNo = card.querySelector('#overlay-no');

  card.addEventListener('mousedown', e => {
    startX = e.clientX;
    startY = e.clientY;
    card.classList.add('dragging');
  });

  document.addEventListener('mousemove', e => {
    if (!card.classList.contains('dragging')) return;
    currentX = e.clientX - startX;
    card.style.transform = `translateX(${currentX}px) rotate(${currentX * 0.05}deg)`;
    const pct = Math.min(Math.abs(currentX) / 100, 1);
    if (currentX > 0) {
      overlayYes.style.opacity = pct;
      overlayNo.style.opacity = 0;
    } else {
      overlayNo.style.opacity = pct;
      overlayYes.style.opacity = 0;
    }
  });

  document.addEventListener('mouseup', e => {
    if (!card.classList.contains('dragging')) return;
    card.classList.remove('dragging');
    if (Math.abs(currentX) > 100) {
      swipe(currentX > 0 ? 'right' : 'left');
    } else {
      card.style.transform = '';
      overlayYes.style.opacity = 0;
      overlayNo.style.opacity = 0;
    }
  });
}

loadProspects();
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

_prospects_cache = None

@app.route('/prospects')
def prospects():
    global _prospects_cache
    if _prospects_cache is None:
        _prospects_cache = load_prospects()
    return jsonify(_prospects_cache)

@app.route('/rate', methods=['POST'])
def rate():
    data = request.json
    save_rating(data['username'], data['rating'])
    return jsonify({'ok': True})

if __name__ == '__main__':
    import webbrowser
    print("\n🎯 Gravel Prospect Swiper")
    print("Opening at http://localhost:5001")
    print("Press Ctrl+C to stop\n")
    webbrowser.open('http://localhost:5001')
    app.run(debug=False, port=5001)
