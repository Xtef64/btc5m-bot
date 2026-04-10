"""
onchain_collector.py
Collecte les métriques onchain BTC
Sources : Glassnode (API gratuite limitée), Blockchain.com (public), CryptoQuant (public)
"""

import os
import requests
import json
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GLASSNODE_API_KEY = os.getenv("GLASSNODE_API_KEY", "")
GLASSNODE_BASE    = "https://api.glassnode.com/v1/metrics"


def _glassnode_get(endpoint: str, params: dict = None) -> float | None:
    """Appel générique Glassnode — retourne la dernière valeur ou None."""
    if not GLASSNODE_API_KEY:
        logger.warning("GLASSNODE_API_KEY non définie, skip.")
        return None
    url = f"{GLASSNODE_BASE}/{endpoint}"
    p = {"a": "BTC", "api_key": GLASSNODE_API_KEY, "f": "JSON", "i": "24h"}
    if params:
        p.update(params)
    try:
        r = requests.get(url, params=p, timeout=15)
        r.raise_for_status()
        data = r.json()
        return float(data[-1]["v"]) if data else None
    except Exception as e:
        logger.error(f"Glassnode {endpoint}: {e}")
        return None


def get_sopr() -> dict:
    """
    SOPR (Spent Output Profit Ratio).
    > 1 = les gens vendent en profit (distribution potentielle)
    < 1 = les gens vendent à perte (capitulation / opportunité d'achat)
    """
    value = _glassnode_get("indicators/sopr")
    if value is None:
        # Fallback : valeur neutre
        return {"sopr": 1.0, "signal": "neutral", "source": "unavailable"}

    if value > 1.05:
        signal = "bearish"   # distribution
    elif value < 0.97:
        signal = "bullish"   # capitulation / achat
    else:
        signal = "neutral"

    return {
        "sopr": round(value, 4),
        "signal": signal,
        "source": "glassnode",
        "timestamp": datetime.utcnow().isoformat()
    }


def get_exchange_flows() -> dict:
    """
    Flux BTC vers/depuis les exchanges.
    Entrées nettes positives → ventes probables (bearish)
    Entrées nettes négatives → accumulation (bullish)
    """
    inflow  = _glassnode_get("transactions/transfers_volume_to_exchanges_sum")
    outflow = _glassnode_get("transactions/transfers_volume_from_exchanges_sum")

    if inflow is None or outflow is None:
        return {"net_flow": 0.0, "signal": "neutral", "source": "unavailable"}

    net = inflow - outflow
    if net > 1000:
        signal = "bearish"
    elif net < -1000:
        signal = "bullish"
    else:
        signal = "neutral"

    return {
        "inflow":   round(inflow, 2),
        "outflow":  round(outflow, 2),
        "net_flow": round(net, 2),
        "signal":   signal,
        "source":   "glassnode",
        "timestamp": datetime.utcnow().isoformat()
    }


def get_mvrv() -> dict:
    """
    MVRV Ratio (Market Value / Realized Value).
    > 3.5 = zone de distribution historique (bearish)
    < 1   = zone d'achat historique (bullish)
    """
    value = _glassnode_get("market/mvrv")
    if value is None:
        return {"mvrv": 2.0, "signal": "neutral", "source": "unavailable"}

    if value > 3.5:
        signal = "bearish"
    elif value > 2.5:
        signal = "caution"
    elif value < 1.0:
        signal = "strong_bullish"
    elif value < 1.5:
        signal = "bullish"
    else:
        signal = "neutral"

    return {
        "mvrv":   round(value, 4),
        "signal": signal,
        "source": "glassnode",
        "timestamp": datetime.utcnow().isoformat()
    }


def get_hashrate() -> dict:
    """
    Hashrate BTC via Blockchain.com (API publique, sans clé).
    Hashrate en hausse = réseau fort = signal de confiance des mineurs.
    """
    try:
        r = requests.get(
            "https://api.blockchain.info/charts/hash-rate?timespan=7days&format=json",
            timeout=15
        )
        r.raise_for_status()
        values = r.json()["values"]
        if len(values) < 2:
            return {"hashrate_change_pct": 0.0, "signal": "neutral"}
        current = values[-1]["y"]
        prev    = values[-7]["y"] if len(values) >= 7 else values[0]["y"]
        change  = (current - prev) / prev * 100
        signal  = "bullish" if change > 3 else ("bearish" if change < -3 else "neutral")
        return {
            "hashrate_th": round(current, 2),
            "change_7d_pct": round(change, 2),
            "signal": signal,
            "source": "blockchain.com",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur hashrate: {e}")
        return {"hashrate_change_pct": 0.0, "signal": "neutral"}


def get_mempool_congestion() -> dict:
    """
    Congestion mempool via mempool.space (API publique).
    Frais élevés → forte activité → signal bullish à court terme.
    """
    try:
        r = requests.get("https://mempool.space/api/v1/fees/recommended", timeout=10)
        r.raise_for_status()
        fees = r.json()
        fast = fees.get("fastestFee", 10)
        if fast > 50:
            signal = "bullish"   # forte demande de transactions
        elif fast < 5:
            signal = "bearish"   # faible activité
        else:
            signal = "neutral"
        return {
            "fastest_fee_sat_vb": fast,
            "signal": signal,
            "source": "mempool.space",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur mempool: {e}")
        return {"fastest_fee_sat_vb": 0, "signal": "neutral"}


SIGNAL_MAP = {
    "strong_bullish": 1.0,
    "bullish":         0.6,
    "neutral":         0.0,
    "caution":        -0.3,
    "bearish":        -0.7,
}


def compute_onchain_score(sopr: dict, flows: dict, mvrv: dict,
                          hashrate: dict, mempool: dict) -> float:
    """
    Score onchain entre -1 et +1.
    Pondération :
      MVRV    : 30%  (contexte de marché long terme)
      SOPR    : 25%  (comportement récent des holders)
      Flows   : 25%  (pression sell immédiate)
      Hashrate: 10%  (sécurité / confiance mineurs)
      Mempool : 10%  (activité réseau court terme)
    """
    weights = {
        "mvrv":     (SIGNAL_MAP.get(mvrv.get("signal", "neutral"), 0.0),     0.30),
        "sopr":     (SIGNAL_MAP.get(sopr.get("signal", "neutral"), 0.0),     0.25),
        "flows":    (SIGNAL_MAP.get(flows.get("signal", "neutral"), 0.0),    0.25),
        "hashrate": (SIGNAL_MAP.get(hashrate.get("signal", "neutral"), 0.0), 0.10),
        "mempool":  (SIGNAL_MAP.get(mempool.get("signal", "neutral"), 0.0),  0.10),
    }
    score = sum(v * w for v, w in weights.values())
    return round(score, 4)


def collect_onchain() -> dict:
    """Point d'entrée principal."""
    logger.info("Collecte onchain en cours...")

    sopr     = get_sopr()
    flows    = get_exchange_flows()
    mvrv     = get_mvrv()
    hashrate = get_hashrate()
    mempool  = get_mempool_congestion()

    onchain_score = compute_onchain_score(sopr, flows, mvrv, hashrate, mempool)
    logger.info(f"Score onchain : {onchain_score}")

    return {
        "sopr":          sopr,
        "exchange_flows": flows,
        "mvrv":          mvrv,
        "hashrate":      hashrate,
        "mempool":       mempool,
        "onchain_score": onchain_score,
        "timestamp":     datetime.utcnow().isoformat()
    }


if __name__ == "__main__":
    result = collect_onchain()
    print(json.dumps(result, indent=2))
