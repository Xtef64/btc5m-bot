"""
macro_collector.py
Collecte les données macro : DXY, SPX, Gold, BTC price, taux Fed
Sources : yfinance (gratuit, sans clé)
"""

import yfinance as yf
import requests
import json
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_price_data(ticker: str, period: str = "5d", interval: str = "1h") -> dict:
    """Récupère prix + variation pour un ticker yfinance."""
    try:
        data = yf.Ticker(ticker).history(period=period, interval=interval)
        if data.empty:
            return {}
        latest = float(data["Close"].iloc[-1])
        prev    = float(data["Close"].iloc[-2])
        change  = (latest - prev) / prev * 100
        return {
            "price": round(latest, 4),
            "change_pct": round(change, 4),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur yfinance {ticker}: {e}")
        return {}


def get_fear_greed() -> dict:
    """Index Fear & Greed crypto (Alternative.me) — API publique, sans clé."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        d = r.json()["data"][0]
        return {
            "value": int(d["value"]),
            "label": d["value_classification"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur Fear&Greed: {e}")
        return {}


def get_fed_rate() -> dict:
    """
    Taux directeur Fed via FRED API (gratuit).
    Clé par défaut = demo (limitée). Créer un compte sur https://fred.stlouisfed.org
    et remplacer FRED_API_KEY dans votre .env
    """
    import os
    api_key = os.getenv("FRED_API_KEY", "demo")
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=FEDFUNDS&api_key={api_key}&file_type=json"
        f"&sort_order=desc&limit=1"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        obs = r.json()["observations"]
        if obs:
            return {
                "fed_rate": float(obs[0]["value"]),
                "date": obs[0]["date"],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
    except Exception as e:
        logger.error(f"Erreur FRED: {e}")
    return {}


def compute_macro_score(data: dict) -> float:
    """
    Score macro entre -1 (très bearish) et +1 (très bullish).
    Logique :
      - DXY fort  → bearish BTC  (négatif)
      - SPX fort  → bullish BTC  (positif, corrélation risk-on)
      - Gold fort → neutre/légèrement bullish
      - Fear&Greed > 60 = greed → attention, possible top (légèrement négatif)
      - Fed rate élevé → bearish (liquidités chères)
    """
    score = 0.0
    weight_total = 0.0

    # DXY : dollar fort = mauvais pour BTC
    dxy = data.get("DXY", {})
    if dxy.get("change_pct") is not None:
        dxy_score = -min(max(dxy["change_pct"] / 2, -1), 1)  # normalisé [-1, 1]
        score += dxy_score * 0.25
        weight_total += 0.25

    # SPX : marchés actions haussiers = risk-on = bon pour BTC
    spx = data.get("SPX", {})
    if spx.get("change_pct") is not None:
        spx_score = min(max(spx["change_pct"] / 2, -1), 1)
        score += spx_score * 0.30
        weight_total += 0.30

    # Fear & Greed : extrême greed (>75) = suracheté, extrême fear (<25) = opportunité
    fg = data.get("fear_greed", {})
    if fg.get("value") is not None:
        v = fg["value"]
        if v > 75:
            fg_score = -0.5
        elif v < 25:
            fg_score = 0.7
        else:
            fg_score = (v - 50) / 50
        score += fg_score * 0.25
        weight_total += 0.25

    # Fed rate : taux élevé = bearish
    fed = data.get("fed_rate", {})
    if fed.get("fed_rate") is not None:
        rate = fed["fed_rate"]
        fed_score = -min(rate / 6, 1)  # normalisé : 6% = score -1
        score += fed_score * 0.20
        weight_total += 0.20

    if weight_total == 0:
        return 0.0
    return round(score / weight_total, 4)


def collect_macro() -> dict:
    """Point d'entrée principal — retourne toutes les données macro + score."""
    logger.info("Collecte macro en cours...")

    data = {
        "BTC":       get_price_data("BTC-USD"),
        "DXY":       get_price_data("DX-Y.NYB"),
        "SPX":       get_price_data("^GSPC"),
        "GOLD":      get_price_data("GC=F"),
        "fear_greed": get_fear_greed(),
        "fed_rate":   get_fed_rate(),
    }

    data["macro_score"] = compute_macro_score(data)
    logger.info(f"Score macro : {data['macro_score']}")
    return data


if __name__ == "__main__":
    result = collect_macro()
    print(json.dumps(result, indent=2))
