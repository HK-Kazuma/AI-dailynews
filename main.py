"""
Daily News Digest
GitHub Actions で毎朝実行 → Gmail に送信
複数トピックの記事を Claude が重要度で自動選択する
"""

import os
import json
import re
import smtplib
import yaml
import anthropic
import feedparser
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo


# ── Config ──────────────────────────────────────────────────

def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── News Fetching ────────────────────────────────────────────

def fetch_rss(url: str, topic_name: str, count: int = 5) -> list[dict]:
    """RSSフィードから記事を取得してトピックタグを付与"""
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:count]:
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            articles.append({
                "topic": topic_name,
                "title": entry.get("title", ""),
                "summary": summary[:600],
                "url": entry.get("link", ""),
                "source": feed.feed.get("title", "RSS"),
            })
        return articles
    except Exception as e:
        print(f"  [RSS ERROR] {url[:50]}: {e}")
        return []


def fetch_hackernews(keywords: list[str], topic_name: str, count: int = 5) -> list[dict]:
    """HackerNews トップ記事をキーワードフィルタ"""
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        )
        story_ids = resp.json()[:120]
    except Exception as e:
        print(f"  [HN ERROR] {e}")
        return []

    stories = []
    kws = [k.lower() for k in keywords]

    for sid in story_ids:
        if len(stories) >= count:
            break
        try:
            story = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=5
            ).json()
            if not story:
                continue
            title_lower = (story.get("title", "") or "").lower()
            if any(kw in title_lower for kw in kws):
                stories.append({
                    "topic": topic_name,
                    "title": story.get("title", ""),
                    "summary": "",
                    "url": story.get("url") or f"https://news.ycombinator.com/item?id={sid}",
                    "source": "Hacker News",
                })
        except Exception:
            continue
    return stories


def deduplicate(articles: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for a in articles:
        key = a["title"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(a)
    return result


def collect_all_articles(topics: list[dict], is_global: bool) -> list[dict]:
    """全トピックから記事を収集してフラットなリストに"""
    all_articles = []
    rss_key = "rss_global" if is_global else "rss_japan"

    for topic in topics:
        name = topic["name"]
        for url in topic.get(rss_key, []):
            print(f"  [{name}] RSS: {url[:55]}...")
            all_articles.extend(fetch_rss(url, name, count=4))

        if is_global and topic.get("hackernews_keywords"):
            print(f"  [{name}] HackerNews...")
            all_articles.extend(
                fetch_hackernews(topic["hackernews_keywords"], name, count=4)
            )

    return deduplicate(all_articles)


# ── Claude: 重要度選択 ────────────────────────────────────────

def select_and_process_with_claude(
    articles: list[dict],
    client: anthropic.Anthropic,
    total: int,
    is_english: bool,
    topic_names: list[str],
) -> list[dict]:
    """
    候補記事を全部渡して Claude に重要度で total 件を選ばせ、
    要約・翻訳も同時に実行する（1リクエスト）
    """
    if not articles:
        return []

    # 候補は多くても20件に絞る（トークン節約）
    candidates = articles[:20]

    candidates_json = json.dumps(
        [
            {
                "id": i,
                "topic": a["topic"],
                "title": a["title"],
                "summary": a["summary"][:300],
                "url": a["url"],
                "source": a["source"],
            }
            for i, a in enumerate(candidates)
        ],
        ensure_ascii=False,
        indent=2,
    )

    topics_str = "、".join(topic_names)
    lang_note = (
        "英語で書かれた記事です。日本語訳を必ず含めてください。"
        if is_english
        else "日本語で書かれた記事です。英語訳も含めてください。"
    )

    prompt = f"""あなたはニュースキュレーターです。
対象トピック：{topics_str}
{lang_note}

以下の候補記事の中から、読者にとって最も重要・興味深い {total} 件を選んでください。
選定基準：新規性、インパクトの大きさ、実用的な重要度。
複数トピックがある場合、極力バランスよく選ぶこと（1トピックに偏りすぎない）。

候補記事:
{candidates_json}

必ず以下のJSON配列形式のみで返答してください。マークダウン・コードブロック不要。
選んだ {total} 件を重要度の高い順に並べること。

[
  {{
    "topic": "トピック名",
    "title_ja": "日本語タイトル",
    "title_en": "English title",
    "summary_ja": "日本語で2〜3文の要約。具体的な数値・固有名詞を含めること。",
    "summary_en": "2-3 sentence English summary with specific details.",
    "reason_ja": "この記事を選んだ理由（1文）",
    "source": "ソース名",
    "url": "URL"
  }}
]"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    except Exception as e:
        print(f"  [CLAUDE ERROR] {e}")
        # フォールバック: 先頭 total 件をそのまま返す
        return [
            {
                "topic": a["topic"],
                "title_ja": a["title"],
                "title_en": a["title"],
                "summary_ja": a["summary"],
                "summary_en": a["summary"],
                "reason_ja": "",
                "source": a["source"],
                "url": a["url"],
            }
            for a in candidates[:total]
        ]


# ── Email Builder ─────────────────────────────────────────────

TOPIC_COLORS = [
    "#4f46e5", "#059669", "#dc2626", "#d97706",
    "#7c3aed", "#0891b2", "#be185d",
]

def get_topic_color(topic_name: str, topic_names: list[str]) -> str:
    try:
        idx = topic_names.index(topic_name) % len(TOPIC_COLORS)
    except ValueError:
        idx = 0
    return TOPIC_COLORS[idx]


def build_news_card(item: dict, idx: int, accent: str) -> str:
    reason_html = (
        f'<div style="font-size:12px; color:#94a3b8; margin-bottom:10px; '
        f'font-style:italic;">💡 {item.get("reason_ja","")}</div>'
        if item.get("reason_ja")
        else ""
    )
    topic_badge = (
        f'<span style="background:{accent}22; color:{accent}; '
        f'font-size:11px; padding:2px 8px; border-radius:20px; '
        f'font-weight:600; margin-left:8px;">{item.get("topic","")}</span>'
    )
    return f"""
    <div style="margin-bottom:24px; padding:20px; background:#f8fafc;
                border-radius:10px; border-left:4px solid {accent};">
      <div style="font-size:12px; color:#94a3b8; margin-bottom:6px; display:flex; align-items:center;">
        #{idx} &nbsp;·&nbsp; {item.get('source','')} {topic_badge}
      </div>
      <h3 style="margin:0 0 4px; font-size:16px; line-height:1.4;">
        <a href="{item.get('url','#')}" style="color:#0f172a; text-decoration:none;">
          {item.get('title_ja','')}
        </a>
      </h3>
      <div style="font-size:12px; color:#94a3b8; margin-bottom:10px;">
        {item.get('title_en','')}
      </div>
      {reason_html}
      <p style="margin:0 0 8px; font-size:14px; color:#334155; line-height:1.65;">
        {item.get('summary_ja','')}
      </p>
      <p style="margin:0 0 10px; font-size:13px; color:#64748b;
                line-height:1.6; font-style:italic;">
        {item.get('summary_en','')}
      </p>
      <a href="{item.get('url','#')}"
         style="font-size:12px; color:{accent}; text-decoration:none;">
        → 記事を読む
      </a>
    </div>"""


def build_html_email(
    global_news: list[dict],
    japan_news: list[dict],
    topic_names: list[str],
    date_str: str,
) -> str:
    topics_label = " / ".join(topic_names)

    global_cards = "".join(
        build_news_card(item, i + 1, get_topic_color(item.get("topic",""), topic_names))
        for i, item in enumerate(global_news)
    )
    japan_cards = "".join(
        build_news_card(item, i + 1, get_topic_color(item.get("topic",""), topic_names))
        for i, item in enumerate(japan_news)
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
             max-width:620px; margin:0 auto; padding:20px 16px;
             background:#ffffff; color:#0f172a;">

  <div style="background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
              padding:28px 24px; border-radius:14px; margin-bottom:32px; color:white;">
    <p style="margin:0 0 4px; font-size:12px; opacity:0.75; letter-spacing:0.05em;">
      DAILY DIGEST — AI CURATED
    </p>
    <h1 style="margin:0 0 6px; font-size:24px; font-weight:700;">
      📰 News Digest
    </h1>
    <p style="margin:0; font-size:13px; opacity:0.85;">
      {date_str} &nbsp;·&nbsp; {topics_label}
    </p>
  </div>

  <h2 style="font-size:14px; font-weight:600; color:#4f46e5;
             letter-spacing:0.06em; text-transform:uppercase;
             border-bottom:2px solid #e2e8f0; padding-bottom:8px; margin-bottom:20px;">
    🌐 グローバル
  </h2>
  {global_cards}

  <h2 style="font-size:14px; font-weight:600; color:#059669;
             letter-spacing:0.06em; text-transform:uppercase;
             border-bottom:2px solid #e2e8f0;
             padding-bottom:8px; margin:32px 0 20px;">
    🇯🇵 日本
  </h2>
  {japan_cards}

  <div style="margin-top:36px; padding:14px; background:#f1f5f9;
              border-radius:8px; font-size:11px; color:#94a3b8; text-align:center;">
    GitHub Actions × Claude Haiku で自動生成 &nbsp;|&nbsp;
    記事の選定・要約・翻訳はすべて AI が実行
  </div>

</body>
</html>"""


# ── Email Sender ──────────────────────────────────────────────

def send_gmail(
    subject: str,
    html_body: str,
    gmail_user: str,
    app_password: str,
    to_email: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, app_password)
        server.sendmail(gmail_user, to_email, msg.as_string())


# ── Main ──────────────────────────────────────────────────────

def main():
    config = load_config()

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    gmail_user    = os.environ["GMAIL_USER"]
    gmail_pass    = os.environ["GMAIL_APP_PASSWORD"]
    to_email      = os.environ.get("TO_EMAIL", gmail_user)

    client = anthropic.Anthropic(api_key=anthropic_key)

    topics     = config["topics"]
    total      = config.get("total_articles", 3)
    topic_names = [t["name"] for t in topics]

    print(f"📰 全トピックのニュース取得中... ({', '.join(topic_names)})")

    global_raw = collect_all_articles(topics, is_global=True)
    japan_raw  = collect_all_articles(topics, is_global=False)

    print(f"  取得: グローバル {len(global_raw)}件, 日本 {len(japan_raw)}件")
    print(f"🤖 Claude が重要度で {total} 件を選定・翻訳中...")

    global_selected = select_and_process_with_claude(
        global_raw, client, total, is_english=True,  topic_names=topic_names
    )
    japan_selected = select_and_process_with_claude(
        japan_raw,  client, total, is_english=False, topic_names=topic_names
    )

    jst     = ZoneInfo("Asia/Tokyo")
    now_jst = datetime.now(jst)
    date_str = now_jst.strftime("%Y年%m月%d日 (%A)")
    subject  = f"📰 News Digest — {now_jst.strftime('%m/%d')} ({' / '.join(topic_names)})"

    print("📧 メール送信中...")
    html = build_html_email(global_selected, japan_selected, topic_names, date_str)
    send_gmail(subject, html, gmail_user, gmail_pass, to_email)

    print(f"✅ 送信完了 → {to_email}")


if __name__ == "__main__":
    main()
