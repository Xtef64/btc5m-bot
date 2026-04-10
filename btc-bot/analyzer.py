"""
analyzer.py + strategy.py
Agrège les 3 scores (macro, sentiment, onchain) en un signal composite
et génère les ordres Spot et Futures pour Binance.
"""

import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  ANALYZER
# ─────────────────────────────────────────────

WEIGHTS = {
    "macro":     0.30,
    "sentiment": 0.30,
    "onchain":   0.40,
}

# Seuils de décision
LONG_THRESHOLD  =  0.25   # score > 0.25  → BUY/LONG
SHORT_THRESHOLD = -0.25   # score < -0.25 → SELL/SHORT
# Entre les deux → HOLD


def compute_composite_score(macro_score: float,
                             sentiment_score: float,
                             onchain_score: float) -> dict:
    """Calcule le score composite pondéré et le signal de marché."""
    composite = (
        macro_score     * WEIGHTS["macro"] +
        sentiment_score * WEIGHTS["sentiment"] +
        onchain_score   * WEIGHTS["onchain"]
    )
    composite = round(composite, 4)

    # Conviction : distance au seuil (0→faible, 1→très fort signal)
    if composite >= LONG_THRESHOLD:
        direction  = "LONG"
        conviction = min((composite - LONG_THRESHOLD) / (1 - LONG_THRESHOLD), 1.0)
    elif composite <= SHORT_THRESHOLD:
        direction  = "SHORT"
        conviction = min((abs(composite) - abs(SHORT_THRESHOLD)) / (1 - abs(SHORT_THRESHOLD)), 1.0)
    else:
        direction  = "HOLD"
        conviction = 0.0

    return {
        "composite_score": composite,
        "direction":       direction,
        "conviction":      round(conviction, 4),
        "macro_score":     macro_score,
        "sentiment_score": sentiment_score,
        "onchain_score":   onchain_score,
        "weights":         WEIGHTS,
        "timestamp":       datetime.now(timezone.utc).isoformat()
    }


# ─────────────────────────────────────────────
#  STRATEGY GENERATOR
# ─────────────────────────────────────────────

@dataclass
class Order:
    """Représente un ordre à exécuter sur Binance."""
    market:     str   # "spot" ou "futures"
    side:       str   # "BUY" / "SELL"
    symbol:     str   # "BTCUSDT"
    usdt_amount: float
    leverage:   int   # 1 pour spot
    stop_loss_pct:   float
    take_profit_pct: float
    reason:     str
    timestamp:  str

    def to_dict(self):
        return asdict(self)


# Config risque
CAPITAL_USDT       = float(__import__("os").getenv("CAPITAL_USDT", "300"))
MAX_RISK_PCT       = 0.02   # 2% du capital par trade max
SPOT_LEVERAGE      = 1
FUTURES_LEVERAGE   = 3      # prudent pour commencer

# SL/TP en fonction de la conviction
def get_sl_tp(direction: str, conviction: float) -> tuple[float, float]:
    """
    Retourne (stop_loss_pct, take_profit_pct) adaptés à la conviction.
    Conviction faible → SL serré, TP proche
    Conviction forte  → on laisse plus de marge
    """
    base_sl = 0.02 + conviction * 0.02   # 2% à 4%
    base_tp = 0.04 + conviction * 0.06   # 4% à 10%
    return round(base_sl, 4), round(base_tp, 4)


def generate_strategy(analysis: dict, btc_price: float) -> dict:
    """
    Génère les ordres Spot et/ou Futures selon le signal composite.

    Règles :
      - HOLD      → aucun ordre
      - LONG faible (conviction < 0.4)  → spot seulement
      - LONG fort  (conviction >= 0.4)  → spot + futures
      - SHORT      → futures seulement (pas de short en spot)
    """
    direction  = analysis["direction"]
    conviction = analysis["conviction"]
    orders     = []

    if direction == "HOLD":
        logger.info("Signal HOLD — aucun ordre généré.")
        return {
            "signal":  "HOLD",
            "orders":  [],
            "analysis": analysis,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    sl_pct, tp_pct = get_sl_tp(direction, conviction)

    # Taille de position : proportionnelle à la conviction, limitée par le risque max
    position_usdt = min(
        CAPITAL_USDT * MAX_RISK_PCT / sl_pct,   # risque $
        CAPITAL_USDT * 0.20 * (0.5 + conviction)  # max 20% * conviction
    )
    position_usdt = round(position_usdt, 2)

    if direction == "LONG":
        # Ordre Spot (toujours pour LONG)
        orders.append(Order(
            market="spot",
            side="BUY",
            symbol="BTCUSDT",
            usdt_amount=position_usdt,
            leverage=SPOT_LEVERAGE,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"Score composite {analysis['composite_score']:.3f} — LONG conviction {conviction:.2f}",
            timestamp=datetime.now(timezone.utc).isoformat()
        ))

        # Ordre Futures si conviction forte
        if conviction >= 0.40:
            futures_usdt = round(position_usdt * 0.5, 2)  # moitié en futures
            orders.append(Order(
                market="futures",
                side="BUY",
                symbol="BTCUSDT",
                usdt_amount=futures_usdt,
                leverage=FUTURES_LEVERAGE,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                reason=f"Score {analysis['composite_score']:.3f} — LONG fort conviction {conviction:.2f} × {FUTURES_LEVERAGE}x",
                timestamp=datetime.now(timezone.utc).isoformat()
            ))

    elif direction == "SHORT":
        # Short uniquement en Futures
        orders.append(Order(
            market="futures",
            side="SELL",
            symbol="BTCUSDT",
            usdt_amount=position_usdt,
            leverage=FUTURES_LEVERAGE,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"Score {analysis['composite_score']:.3f} — SHORT conviction {conviction:.2f} × {FUTURES_LEVERAGE}x",
            timestamp=datetime.now(timezone.utc).isoformat()
        ))

    logger.info(f"Stratégie : {direction} | {len(orders)} ordre(s) | conviction {conviction:.2f}")
    for o in orders:
        logger.info(f"  → {o.market.upper()} {o.side} {o.usdt_amount} USDT | SL {o.stop_loss_pct*100:.1f}% TP {o.take_profit_pct*100:.1f}%")

    return {
        "signal":    direction,
        "conviction": conviction,
        "orders":    [o.to_dict() for o in orders],
        "analysis":  analysis,
        "btc_price": btc_price,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# ─────────────────────────────────────────────
#  TEST STANDALONE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Simulation avec des scores fictifs
    test_analysis = compute_composite_score(
        macro_score=0.15,
        sentiment_score=0.40,
        onchain_score=0.55
    )
    print("=== ANALYSE ===")
    print(json.dumps(test_analysis, indent=2))

    strategy = generate_strategy(test_analysis, btc_price=65000)
    print("\n=== STRATÉGIE ===")
    print(json.dumps(strategy, indent=2))
