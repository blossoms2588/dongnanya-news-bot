"""
东南亚新闻自动化推送系统 v3.2
整合模块：
1. RSS 抓取（支持6国）
2. DeepL 翻译（自动识别中文跳过）
3. 本地缓存去重（ArticleStore）
4. 日志记录 + 推送重试机制
5. 管理员警报通知
6. 保活运行支持
"""

import requests, json, feedparser
import deepl
from time import sleep
from datetime import datetime
from config import BOT_TOKEN, CHANNEL_USERNAME, DEEPL_AUTH_KEY, ADMIN_CHAT_ID

# ---------- Logger ----------
import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NewsBot")


# ---------- 存储模块 ----------
class ArticleStore:

    def __init__(self, store_file="posted_titles.txt"):
        self.store_file = store_file
        self.posted_titles = set()
        self._load()

    def _load(self):
        try:
            with open(self.store_file, "r", encoding="utf-8") as f:
                for line in f:
                    self.posted_titles.add(line.strip())
        except FileNotFoundError:
            pass

    def add_record(self, title):
        if title not in self.posted_titles:
            self.posted_titles.add(title)
            with open(self.store_file, "a", encoding="utf-8") as f:
                f.write(title + "\n")


# ---------- 翻译模块 ----------
translator = deepl.Translator(DEEPL_AUTH_KEY)


def translate_text(text, retry=3):
    """
    支持中文判断 + DeepL翻译 + 失败提示
    """
    if is_chinese(text):
        logger.info("🔠 原文为中文，跳过翻译")
        return text

    for attempt in range(retry):
        try:
            result = translator.translate_text(text, target_lang="ZH").text
            logger.info(f"📘 翻译成功：{text[:20]} -> {result[:20]}")
            return result
        except Exception as e:
            logger.warning(f"⚠️ 翻译失败（尝试{attempt+1}/{retry}）: {str(e)}")
            sleep(2**attempt)

    return text + "（翻译失败）"


def is_chinese(text):
    return any('\u4e00' <= ch <= '\u9fff' for ch in text[:10])


# ---------- RSS 来源 ----------
RSS_SOURCES = {
    "柬埔寨": "http://www.jianhuadaily.com/rss.xml",
    "新加坡": "https://www.channelnewsasia.com/rssfeeds/8395986",
    "泰国": "https://www.bangkokpost.com/rss/data/topstories.xml",
    "越南": "https://e.vnexpress.net/rss/news.rss",
    "缅甸": "https://www.irrawaddy.com/feed",
    "老挝": "https://vientianetimes.org.la/rss"
}


def fetch_rss_articles(rss_url):
    try:
        feed = feedparser.parse(rss_url)
        return [{
            "title": entry.title.strip(),
            "link": entry.link.strip(),
            "published": entry.get("published", "未知")
        } for entry in feed.entries[:5]]
    except Exception as e:
        logger.error(f"抓取 RSS 出错：{e}")
        return []


# ---------- 主系统 ----------
class NewsSystem:

    def __init__(self):
        self.store = ArticleStore()
        self.retry_queue = []
        self.status_log = "send_status.log"
        self._load_retry_queue()

    def _load_retry_queue(self):
        try:
            with open(self.status_log, "r", encoding="utf-8") as f:
                for line in f:
                    record = json.loads(line)
                    if record["state"] == "failed":
                        self.retry_queue.append(record)
            logger.info(f"加载重试队列：{len(self.retry_queue)}")
        except FileNotFoundError:
            pass

    def _log_status(self, article, state, response=None):
        article.setdefault('retry_count', 0)
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "country": article.get("country", "未知"),
            "original_title": article["title"],
            "translated_title": article.get("translated", ""),
            "state": state,
            "retries": article.get("retry_count", 0),
            "response": response.text[:200] if response else None
        }
        with open(self.status_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

        if state == "failed":
            self.retry_queue.append(article)
            self._notify_admin(article)

    def _notify_admin(self, article):
        text = (f"🚨 推送失败警报\n"
                f"国家：{article.get('country', '未知')}\n"
                f"原标题：{article['title'][:50]}\n"
                f"翻译标题：{article.get('translated', '')[:50]}\n"
                f"重试次数：{article.get('retry_count', 0)}")
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_CHAT_ID,
                    "text": text
                },
                timeout=10)
        except Exception as e:
            logger.error(f"管理员通知失败: {e}")

    def send_article(self, article, country):
        article.setdefault('retry_count', 0)
        article.setdefault('translated', article['title'])  # 防止因翻译失败而报错

        payload = {
            "chat_id":
            f"@{CHANNEL_USERNAME}",
            "text": (f"📢【{country}新闻】<b>{article['translated']}</b>\n"
                     f"🔤 原文：{article['title']}\n"
                     f"📅 发布时间：{article.get('published', '未知')}\n"
                     f"👉 阅读原文：{article['link']}"),
            "parse_mode":
            "HTML",
            "disable_web_page_preview":
            True
        }

        for attempt in range(3):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json=payload,
                    timeout=15)
                response.raise_for_status()
                self._log_status(article, "success", response)
                return True
            except Exception as e:
                logger.warning(f"推送失败（{attempt+1}/3）: {e}")
                sleep(5 * (attempt + 1))
                article["retry_count"] += 1

        article.setdefault("retry_count", 0)
        self._log_status(article, "failed")
        return False

    def process_retries(self):
        logger.info(f"处理重试队列（共 {len(self.retry_queue)}）")
        for article in self.retry_queue[:5]:
            article.setdefault("retry_count", 0)
            if article["retry_count"] >= 3:
                logger.warning(f"跳过重试：{article['title'][:30]}")
                self.retry_queue.remove(article)
                continue
            if self.send_article(article, article.get("country", "未知")):
                self.retry_queue.remove(article)
                self.store.add_record(article["title"])
            sleep(3)


# ---------- 主处理循环 ----------
def processing_cycle(system):
    logger.info("=" * 40)
    for country, rss_url in RSS_SOURCES.items():
        logger.info(f"📡 抓取国家：{country}")
        try:
            articles = fetch_rss_articles(rss_url)
            logger.info(f"共获取 {len(articles)} 篇")

            system.process_retries()

            for article in articles:
                if article["title"] in system.store.posted_titles:
                    logger.info(f"⏩ 跳过已发：{article['title']}")
                    continue

                logger.info(f"📰 新文章：{article['title'][:50]}")
                article["translated"] = translate_text(article["title"])
                article["country"] = country

                if system.send_article(article, country):
                    system.store.add_record(article["title"])
                    sleep(3)

        except Exception as e:
            logger.error(f"‼️ {country} 处理异常：{e}")
            sleep(10)


def main_loop():
    system = NewsSystem()
    logger.info("✅ 机器人启动成功")
    while True:
        try:
            processing_cycle(system)
            logger.info("😴 休眠 30 分钟...")
            sleep(1800)
        except KeyboardInterrupt:
            logger.info("🛑 用户中断程序")
            break
        except Exception as e:
            logger.error(f"‼️ 全局异常: {e}")
            sleep(60)


if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    main_loop()
