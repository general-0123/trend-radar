#!/usr/bin/env python3
"""
PTT 熱門文章 + 留言抓取工具
用途：每日趨勢雷達 routine 的資料收集層（GitHub Actions 版）

輸出：ptt_data/ptt_YYYY-MM-DD_HH-MM.json（單次快照）
      ptt_data/summary.json（當日彙總，Routine 讀取這份）

與本機版的差異：
  1. 不用 curl_cffi，改用 subprocess 呼叫系統內建 curl 模擬瀏覽器指紋
     （日誌 2026-07-12 驗證過此方案在 curl_cffi 於 Apple Silicon + anaconda3
      環境下出現 dlopen 相容性問題後，改採此法穩定連線成功）
  2. 輸出路徑從使用者家目錄改為 repo 內相對路徑 ptt_data/，
     因為 GitHub Actions runner 每次執行完就銷毀，家目錄檔案不會留存，
     必須寫進 repo 才能被後續 commit/push 保留下來
  3. 額外輸出固定檔名 summary.json，供 Trend Radar Routine 直接讀取
     （目前一天只跑一次，push_count_growth 恆為 0，是預留給未來
      恢復「一天多次抓取」時使用的欄位，先留著無害）

使用方式：
    python3 ptt_fetch.py

可調整參數在檔案最下方 CONFIG 區塊。
"""

import subprocess
import json
import re
import time
import os
import glob
from datetime import datetime
from bs4 import BeautifulSoup

BASE_URL = "https://www.ptt.cc"
CURL_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _get(url: str) -> str:
    """
    統一的 GET 請求封裝。
    改用 subprocess 呼叫系統 curl，而非 Python requests 套件，
    因為 PTT 會依連線的 TLS/HTTP 底層特徵擋掉部分 HTTP client
    （包含標準 requests），系統 curl 不受此限制，且不需額外安裝任何
    Python 套件，避開了 curl_cffi 的環境相容性問題。
    """
    result = subprocess.run(
        [
            "curl",
            "-s",  # silent
            "-L",  # follow redirects
            "-A", CURL_UA,
            "-b", "over18=1",  # 跳過 18 歲年齡驗證頁（八卦板等需要）
            "--max-time", "15",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  [警告] curl 失敗 url={url} stderr={result.stderr.strip()}")
        return ""
    return result.stdout


def fetch_board_index(board: str, pages: int = 2):
    """抓取指定看板最近 N 頁的文章列表（含標題、連結、推文數、作者、日期）"""
    articles = []
    url = f"{BASE_URL}/bbs/{board}/index.html"

    for _ in range(pages):
        html = _get(url)
        if not html:
            print(f"  [警告] {board} 頁面抓取失敗，略過")
            break

        soup = BeautifulSoup(html, "html.parser")
        for entry in soup.select("div.r-ent"):
            title_tag = entry.select_one("div.title a")
            if not title_tag:
                continue

            title = title_tag.text.strip()
            link = BASE_URL + title_tag["href"]

            push_tag = entry.select_one("div.nrec")
            push_count_raw = push_tag.text.strip() if push_tag else ""
            push_count = parse_push_count(push_count_raw)

            author_tag = entry.select_one("div.author")
            author = author_tag.text.strip() if author_tag else ""

            date_tag = entry.select_one("div.date")
            date_str = date_tag.text.strip() if date_tag else ""

            articles.append({
                "board": board,
                "title": title,
                "url": link,
                "author": author,
                "date": date_str,
                "push_count": push_count,
            })

        prev_link = soup.select_one("a.btn.wide:-soup-contains('上頁')")
        if not prev_link or "href" not in prev_link.attrs:
            break
        url = BASE_URL + prev_link["href"]
        time.sleep(0.5)

    return articles


def parse_push_count(raw: str):
    """把 PTT 推文數欄位（可能是數字、'爆'、'XX' 負值）轉成可比較的整數"""
    if raw == "爆":
        return 100
    if raw.startswith("X"):
        try:
            return -int(raw[1:]) * 10
        except ValueError:
            return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def fetch_article_detail(url: str, max_comments: int = 20):
    """抓取單篇文章的內文 + 前 N 則熱門留言"""
    html = _get(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    main_content = soup.select_one("#main-content")
    if not main_content:
        return None

    pushes = []
    for push in soup.select("div.push"):
        tag_elem = push.select_one("span.push-tag")
        user_elem = push.select_one("span.push-userid")
        content_elem = push.select_one("span.push-content")
        if not (tag_elem and user_elem and content_elem):
            continue
        pushes.append({
            "type": tag_elem.text.strip(),
            "user": user_elem.text.strip(),
            "content": content_elem.text.strip(": ").strip(),
        })

    for meta in main_content.select(".article-metaline, .article-metaline-right"):
        meta.extract()
    for push in main_content.select(".push"):
        push.extract()
    content_text = main_content.get_text().strip()
    content_text = re.sub(r"\n{3,}", "\n\n", content_text)[:1500]

    type_priority = {"推": 0, "→": 1, "噓": 2}
    pushes_sorted = sorted(pushes, key=lambda p: type_priority.get(p["type"], 1))
    top_comments = pushes_sorted[:max_comments]

    push_n = sum(1 for p in pushes if p["type"] == "推")
    boo_n = sum(1 for p in pushes if p["type"] == "噓")

    return {
        "content": content_text,
        "comment_count": len(pushes),
        "push_n": push_n,
        "boo_n": boo_n,
        "top_comments": top_comments,
    }


def is_recent(date_str: str, days: int = 1) -> bool:
    """判斷文章日期是否在最近 N 天內"""
    date_str = date_str.strip()
    if not date_str:
        return False
    try:
        month, day = map(int, date_str.split("/"))
        today = datetime.now()
        candidate = datetime(today.year, month, day)
        if candidate > today:
            candidate = candidate.replace(year=today.year - 1)
        return (today - candidate).days <= days
    except Exception:
        return False


def run(boards: list, pages_per_board: int, min_push: int, max_articles_per_board: int,
        max_comments: int, recent_days: int):
    all_results = []

    for board in boards:
        print(f"抓取看板：{board}")
        articles = fetch_board_index(board, pages=pages_per_board)
        print(f"  共取得 {len(articles)} 篇候選文章")

        filtered = [
            a for a in articles
            if is_recent(a["date"], days=recent_days) and a["push_count"] >= min_push
        ]
        filtered.sort(key=lambda a: a["push_count"], reverse=True)
        filtered = filtered[:max_articles_per_board]
        print(f"  篩選後保留 {len(filtered)} 篇（推文數 >= {min_push}）")

        for a in filtered:
            detail = fetch_article_detail(a["url"], max_comments=max_comments)
            if detail:
                a.update(detail)
                all_results.append(a)
            time.sleep(0.5)

    return all_results


def build_daily_summary(output_dir: str, date_str: str, summary_filename: str = "summary.json"):
    """讀取當天所有時段的抓取檔案，合併並計算熱度變化，輸出固定檔名 summary.json"""
    pattern = os.path.join(output_dir, f"ptt_{date_str}_*.json")
    files = sorted(f for f in glob.glob(pattern) if not f.endswith(summary_filename))
    if not files:
        return

    merged = {}

    for filepath in files:
        fname = os.path.basename(filepath)
        time_part = fname.replace(f"ptt_{date_str}_", "").replace(".json", "")
        time_label = time_part.replace("-", ":")

        with open(filepath, "r", encoding="utf-8") as f:
            articles = json.load(f)

        for a in articles:
            url = a["url"]
            if url not in merged:
                merged[url] = {
                    "board": a["board"],
                    "title": a["title"],
                    "url": url,
                    "author": a["author"],
                    "snapshots": [],
                    "latest_top_comments": [],
                }
            merged[url]["snapshots"].append({
                "time": time_label,
                "push_count": a["push_count"],
                "comment_count": a.get("comment_count", 0),
                "push_n": a.get("push_n", 0),
                "boo_n": a.get("boo_n", 0),
            })
            if a.get("top_comments"):
                merged[url]["latest_top_comments"] = a["top_comments"]
                merged[url]["latest_content"] = a.get("content", "")

    summary_list = []
    for url, data in merged.items():
        snaps = sorted(data["snapshots"], key=lambda s: s["time"])
        first_push = snaps[0]["push_count"]
        last_push = snaps[-1]["push_count"]
        growth = last_push - first_push

        summary_list.append({
            "board": data["board"],
            "title": data["title"],
            "url": url,
            "author": data["author"],
            "snapshots": snaps,
            "push_count_growth": growth,
            "latest_push_count": last_push,
            "top_comments": data["latest_top_comments"],
            "content": data.get("latest_content", ""),
        })

    summary_list.sort(key=lambda x: x["push_count_growth"], reverse=True)

    summary_path = os.path.join(output_dir, summary_filename)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "articles": summary_list,
        }, f, ensure_ascii=False, indent=2)

    print(f"已更新彙總檔：{summary_path}（共 {len(summary_list)} 篇不重複文章）")


if __name__ == "__main__":
    BOARDS = ["Gossiping", "Stock", "Beauty", "home-sale", "e-shopping"]
    PAGES_PER_BOARD = 2
    MIN_PUSH = 20
    MAX_ARTICLES_PER_BOARD = 8
    MAX_COMMENTS = 20
    RECENT_DAYS = 1

    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptt_data")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = run(
        boards=BOARDS,
        pages_per_board=PAGES_PER_BOARD,
        min_push=MIN_PUSH,
        max_articles_per_board=MAX_ARTICLES_PER_BOARD,
        max_comments=MAX_COMMENTS,
        recent_days=RECENT_DAYS,
    )

    today_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H-%M")
    output_path = os.path.join(OUTPUT_DIR, f"ptt_{today_str}_{time_str}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n完成，共 {len(results)} 篇文章寫入 {output_path}")

    build_daily_summary(OUTPUT_DIR, today_str)
