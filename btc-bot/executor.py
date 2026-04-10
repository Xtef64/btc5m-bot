"""
executor.py + orchestrator
Exécute les ordres sur Binance (Spot et Futures)
Envoie les alertes Telegram
Lance le cycle complet toutes les heures
"""

import os
import json
import logging
import time
import schedule
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config depuis variables d'environnement ───
DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET  = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
CAPITAL_USDT        = float(os.getenv("CAPITAL_USDT", "300"))

logger.info(f"Mode : {'DRY RUN (simulation)' if DRY_RUN else '*** LIVE ***'}")


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str):
    """Envoie un message Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram non configuré — message ignoré.")
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Erreur Telegram: {e}")


def format_signal_message(strategy: dict, analysis: dict) -> str:
    """Formate le message Telegram pour un signal."""
    direction  = strategy.get("signal", "HOLD")
    conviction = strategy.get("conviction", 0)
    orders     = strategy.get("orders", [])
    btc_price  = strategy.get("btc_price", 0)
    cs         = analysis.get("composite_score", 0)

    emoji = {"LONG": "🟢", "SHORT": "🔴", "HOLD": "🟡"}.get(direction, "⚪")
    mode  = "[DRY RUN]" if DRY_RUN else "[LIVE]"

    msg = (
        f"{emoji} <b>Signal BTC {mode}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Direction : <b>{direction}</b>\n"
        f"Conviction : {conviction:.0%}\n"
        f"Score composite : {cs:+.3f}\n"
        f"Prix BTC : ${btc_price:,.0f}\n\n"
        f"<b>Scores détaillés :</b>\n"
        f"  Macro     : {analysis.get('macro_score', 0):+.3f}\n"
        f"  Sentiment : {analysis.get('sentiment_score', 0):+.3f}\n"
        f"  Onchain   : {analysis.get('onchain_score', 0):+.3f}\n"
    )

    if orders:
        msg += f"\n<b>Ordres ({len(orders)}) :</b>\n"
        for o in orders:
            msg += (
                f"  • {o['market'].upper()} {o['side']} "
                f"{o['usdt_amount']} USDT"
                f" | SL {o['stop_loss_pct']*100:.1f}%"
                f" | TP {o['take_profit_pct']*100:.1f}%\n"
            )
    else:
        msg += "\nAucun ordre — En attente.\n"

    msg += f"\n<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
    return msg


# ─────────────────────────────────────────────
#  BINANCE EXECUTOR
# ─────────────────────────────────────────────

def get_binance_clients():
    """Initialise les clients Binance Spot et Futures."""
    if not BINANCE_API_KEY:
        return None, None
    try:
        from binance.client import Client
        from binance.um_futures import UMFutures
        spot    = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        futures = UMFutures(BINANCE_API_KEY, BINANCE_API_SECRET)
        return spot, futures
    except ImportError:
        logger.error("python-binance non installé : pip install python-binance")
        return None, None
    except Exception as e:
        logger.error(f"Erreur init Binance: {e}")
        return None, None


def execute_spot_order(client, order: dict) -> dict:
    """Exécute un ordre Spot sur Binance avec SL/TP via OCO."""
    symbol      = order["symbol"]
    side        = order["side"]
    usdt_amount = order["usdt_amount"]
    sl_pct      = order["stop_loss_pct"]
    tp_pct      = order["take_profit_pct"]

    if DRY_RUN:
        logger.info(f"[DRY RUN] Spot {side} {usdt_amount} USDT {symbol}")
        return {"status": "DRY_RUN", "order": order}

    try:
        # Prix courant
        price = float(client.get_symbol_ticker(symbol=symbol)["price"])
        qty   = round(usdt_amount / price, 5)

        # Ordre market d'entrée
        entry = client.order_market_buy(symbol=symbol, quantity=qty) \
                if side == "BUY" else \
                client.order_market_sell(symbol=symbol, quantity=qty)

        # OCO pour SL/TP (BUY → on vend avec OCO)
        if side == "BUY":
            sl_price = round(price * (1 - sl_pct), 2)
            tp_price = round(price * (1 + tp_pct), 2)
            client.order_oco_sell(
                symbol=symbol,
                quantity=qty,
                price=str(tp_price),
                stopPrice=str(sl_price),
                stopLimitPrice=str(round(sl_price * 0.995, 2)),
                stopLimitTimeInForce="GTC"
            )

        logger.info(f"Spot {side} exécuté : {qty} BTC @ {price}")
        return {"status": "OK", "entry": entry}

    except Exception as e:
        logger.error(f"Erreur ordre Spot: {e}")
        return {"status": "ERROR", "error": str(e)}


def execute_futures_order(futures_client, order: dict) -> dict:
    """Exécute un ordre Futures (USDT-M) avec SL/TP."""
    symbol      = order["symbol"]
    side        = order["side"]
    usdt_amount = order["usdt_amount"]
    leverage    = order["leverage"]
    sl_pct      = order["stop_loss_pct"]
    tp_pct      = order["take_profit_pct"]

    if DRY_RUN:
        logger.info(f"[DRY RUN] Futures {side} {usdt_amount} USDT × {leverage}x {symbol}")
        return {"status": "DRY_RUN", "order": order}

    try:
        # Définir le levier
        futures_client.change_leverage(symbol=symbol, leverage=leverage)

        price = float(futures_client.ticker_price(symbol=symbol)["price"])
        notional = usdt_amount * leverage
        qty = round(notional / price, 3)

        # Ordre market d'entrée
        entry = futures_client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

        # Stop Loss
        sl_side  = "SELL" if side == "BUY" else "BUY"
        sl_price = price * (1 - sl_pct) if side == "BUY" else price * (1 + sl_pct)
        tp_price = price * (1 + tp_pct) if side == "BUY" else price * (1 - tp_pct)

        futures_client.new_order(
            symbol=symbol, side=sl_side, type="STOP_MARKET",
            stopPrice=round(sl_price, 2), closePosition=True
        )
        futures_client.new_order(
            symbol=symbol, side=sl_side, type="TAKE_PROFIT_MARKET",
            stopPrice=round(tp_price, 2), closePosition=True
        )

        logger.info(f"Futures {side} exécuté : {qty} BTC @ {price} × {leverage}x")
        return {"status": "OK", "entry": entry}

    except Exception as e:
        logger.error(f"Erreur ordre Futures: {e}")
        return {"status": "ERROR", "error": str(e)}


def execute_strategy(strategy: dict) -> list:
    """Exécute tous les ordres d'une stratégie."""
    orders  = strategy.get("orders", [])
    results = []

    if not orders:
        return results

    spot_client, futures_client = get_binance_clients()

    for order in orders:
        if order["market"] == "spot":
            result = execute_spot_order(spot_client, order)
        else:
            result = execute_futures_order(futures_client, order)
        results.append(result)
        time.sleep(0.5)

    return results


# ─────────────────────────────────────────────
#  ORCHESTRATEUR PRINCIPAL
# ─────────────────────────────────────────────

def run_cycle():
    """Lance un cycle complet d'analyse et d'exécution."""
    logger.info("═" * 50)
    logger.info("Nouveau cycle BTC bot")
    logger.info("═" * 50)

    try:
        # Import local des modules
        from macro_collector     import collect_macro
        from sentiment_collector import collect_sentiment
        from onchain_collector   import collect_onchain
        from analyzer            import compute_composite_score, generate_strategy

        # 1. Collecte
        logger.info("1/4 Collecte macro...")
        macro_data = collect_macro()

        logger.info("2/4 Collecte sentiment...")
        sentiment_data = collect_sentiment()

        logger.info("3/4 Collecte onchain...")
        onchain_data = collect_onchain()

        # 2. Analyse
        logger.info("4/4 Analyse composite...")
        analysis = compute_composite_score(
            macro_score     = macro_data.get("macro_score", 0.0),
            sentiment_score = sentiment_data.get("sentiment_score", 0.0),
            onchain_score   = onchain_data.get("onchain_score", 0.0)
        )

        # 3. Stratégie
        btc_price = macro_data.get("BTC", {}).get("price", 0)
        strategy  = generate_strategy(analysis, btc_price)

        # 4. Exécution
        results = execute_strategy(strategy)

        # 5. Alerte Telegram
        if strategy["signal"] != "HOLD" or True:  # Toujours alerter
            msg = format_signal_message(strategy, analysis)
            send_telegram(msg)

        # 6. Log résumé
        logger.info(f"Cycle terminé — Signal: {strategy['signal']} | Score: {analysis['composite_score']:+.3f}")
        logger.info(f"Ordres exécutés : {len(results)}")

        return {
            "macro":     macro_data,
            "sentiment": sentiment_data,
            "onchain":   onchain_data,
            "analysis":  analysis,
            "strategy":  strategy,
            "results":   results
        }

    except Exception as e:
        logger.error(f"Erreur cycle : {e}", exc_info=True)
        send_telegram(f"⚠️ <b>Erreur bot BTC</b>\n<code>{str(e)[:300]}</code>")
        return None


def main():
    """Point d'entrée Railway — cycle toutes les heures."""
    logger.info("Bot BTC démarré")
    send_telegram(
        f"🚀 <b>Bot BTC démarré</b>\n"
        f"Mode : {'DRY RUN' if DRY_RUN else 'LIVE'}\n"
        f"Capital : {CAPITAL_USDT} USDT\n"
        f"Cycle : toutes les heures"
    )

    # Premier cycle immédiat
    run_cycle()

    # Cycle planifié toutes les heures
    schedule.every(1).hours.do(run_cycle)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
