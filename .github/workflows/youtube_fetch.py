#!/usr/bin/env python3
"""
YouTube 台灣地區熱門影片抓取（GitHub Actions 版）

用途：每天定時抓取 YouTube chart=mostPopular, regionCode=TW 榜單，
      輸出 youtube_data/trending.json，供 Trend Radar Routine 讀取，
      整合進「娛樂/影視熱門」欄位。

配額成本：每次執行 1 unit（每日預設額度 10,000 units）。

環境變數：
    YOUTUBE_API_KEY  -- 必要，從 GitHub Actions Secrets 帶入

輸出：
    youtube_data/trending.json
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BASE_URL = "https://www.googleapis.com/youtube/v3/videos"

OUTPUT_DIR = "youtube_data"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "trending.json")

TAIPEI_TZ = timezone(timedelta(hours=8))


def fetch_trending(region_code="TW", max_results=50):
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": region_code,
        "maxResults": max_results,
        "key": API_KEY,
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"❌ HTTP {e.code} 錯誤：{body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ 抓取失敗：{e}", file=sys.stderr)
        sys.exit(1)

    return data


def normalize(data):
    """整理成給 Routine 讀取的精簡格式，保留必要欄位即可"""
    items = data.get("items", [])
    results = []
    for item in items:
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        results.append({
            "video_id": item.get("id"),
            "title": snippet.get("title"),
            "channel": snippet.get("channelTitle"),
            "category_id": snippet.get("categoryId"),
            "view_count": stats.get("viewCount"),
            "like_count": stats.get("likeCount"),
            "published_at": snippet.get("publishedAt"),
            "url": f"https://www.youtube.com/watch?v={item.get('id')}",
        })
    return results


def main():
    if not API_KEY:
        print("❌ 環境變數 YOUTUBE_API_KEY 未設定", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("抓取台灣地區 YouTube 熱門影片榜...")
    raw = fetch_trending(region_code="TW", max_results=50)
    videos = normalize(raw)

    output = {
        "fetched_at": datetime.now(TAIPEI_TZ).isoformat(),
        "region_code": "TW",
        "chart": "mostPopular",
        "count": len(videos),
        "videos": videos,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✅ 完成，共 {len(videos)} 筆，已寫入 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
