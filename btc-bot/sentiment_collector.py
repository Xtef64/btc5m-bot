"""
sentiment_collector.py
Collecte le sentiment BTC sur X (Twitter) et Reddit
sans API payante : ntscraper + Fear&Greed + Reddit JSON
"""

import requests
import json
import re
import logging
import time
from datetime import datetime
from collections import Counter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Mots-clés positifs et négatifs pour scoring rapide
POSITIVE_WORDS = {
    "moon", "pump", "bull", "bullish", "ath", "breakout", "buy", "long",
    "accumulate", "hodl", "rally", "surge", "green", "up", "gain",
    "mooning", "rocket", "adopt", "institutional", "halving"
}
NEGATIVE_WORDS = {
    "dump", "crash", "bear", "bearish", "sell", "short", "drop", "fear",
    "panic", "down", "rekt", "scam", "fud", "rug", "liquidation",
    "correction", "dip", "falling", "blood", "dead"
}


def score_text(text: str) -> float:
    """Score simple d'un texte entre -1 et +1."""
    words = re.findall(r'\w+', text.lower())
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


def get_twitter_sentiment(query: str = "Bitcoin BTC", limit: int = 30) -> dict:
    """
    Scrape les tweets récents via ntscraper (pas d'API key requis).
    Installe : pip install ntscraper
    """
    try:
        from ntscraper import Nitter
        scraper = Nitter(log_level=0)
        tweets = scraper.get_tweets(query, mode="term", number=limit)
        if not tweets or not tweets.get("tweets"):
            logger.warning("Ntscraper : aucun tweet récupéré")
            return {"score": 0.0, "count": 0, "source": "twitter"}

        texts = [t.get("text", "") for t in tweets["tweets"]]
        scores = [score_text(t) for t in texts if t]
        avg_score = round(sum(scores) / len(scores), 4) if scores else 0.0

        return {
            "score": avg_score,
            "count": len(scores),
            "sample_size": limit,
            "source": "twitter/ntscraper",
            "timestamp": datetime.utcnow().isoformat()
        }
    except ImportError:
        logger.error("ntscraper non installé : pip install ntscraper")
        return {"score": 0.0, "count": 0, "source": "twitter_unavailable"}
    except Exception as e:
        logger.error(f"Erreur ntscraper: {e}")
        return {"score": 0.0, "count": 0, "source": "twitter_error"}


def get_reddit_sentiment(subreddit: str = "Bitcoin", limit: int = 25) -> dict:
    """
    Collecte les posts chauds de r/Bitcoin via l'API JSON publique de Reddit.
    Aucune clé requise.
    """
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    headers = {"User-Agent": "btc-sentiment-bot/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
        texts = [p["data"].get("title", "") + " " + p["data"].get("selftext", "") for p in posts]
        scores = [score_text(t) for t in texts if t.strip()]
        avg_score = round(sum(scores) / len(scores), 4) if scores else 0.0
        return {
            "score": avg_score,
            "count": len(scores),
            "subreddit": subreddit,
            "source": "reddit",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur Reddit: {e}")
        return {"score": 0.0, "count": 0, "source": "reddit_error"}


def get_fear_greed_index() -> dict:
    """Index Fear & Greed (Alternative.me) — retourne valeur normalisée entre -1 et +1."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=3", timeout=10)
        r.raise_for_status()
        entries = r.json()["data"]
        current = int(entries[0]["value"])
        # Normalise : 0=extreme fear → -1, 100=extreme greed → +1
        normalized = round((current - 50) / 50, 4)
        trend = 0.0
        if len(entries) >= 2:
            prev = int(entries[1]["value"])
            trend = round((current - prev) / 50, 4)
        return {
            "value": current,
            "label": entries[0]["value_classification"],
            "normalized": normalized,
            "trend": trend,
            "source": "alternative.me",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur Fear&Greed: {e}")
        return {"value": 50, "normalized": 0.0, "trend": 0.0}


def compute_sentiment_score(twitter: dict, reddit: dict, fg: dict) -> float:
    """
    Score sentiment composite entre -1 et +1.
    Pondération :
      - Fear & Greed : 40%  (signal le plus fiable)
      - Twitter      : 35%
      - Reddit       : 25%
    """
    fg_norm    = fg.get("normalized", 0.0)
    tw_score   = twitter.get("score", 0.0)
    re_score   = reddit.get("score", 0.0)

    composite = (fg_norm * 0.40) + (tw_score * 0.35) + (re_score * 0.25)
    return round(composite, 4)


def collect_sentiment() -> dict:
    """Point d'entrée principal."""
    logger.info("Collecte sentiment en cours...")

    twitter = get_twitter_sentiment("Bitcoin BTC", limit=30)
    time.sleep(2)  # évite le rate limiting ntscraper
    reddit  = get_reddit_sentiment("Bitcoin", limit=25)
    fg      = get_fear_greed_index()

    composite = compute_sentiment_score(twitter, reddit, fg)
    logger.info(f"Score sentiment : {composite}")

    return {
        "twitter":         twitter,
        "reddit":          reddit,
        "fear_greed":      fg,
        "sentiment_score": composite,
        "timestamp":       datetime.utcnow().isoformat()
    }


if __name__ == "__main__":
    result = collect_sentiment()
    print(json.dumps(result, indent=2))
