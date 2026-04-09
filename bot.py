"""
╔══════════════════════════════════════════════════════╗
║  Polymarket BTC 5-Min Bot — DRY RUN                 ║
║  + Dashboard Web · Graphique Bankroll · Telegram    ║
╚══════════════════════════════════════════════════════╝
"""

import time
import json
import logging
import os
import requests
import threading
import gc
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List
from flask import Flask, jsonify, render_template_string
import websocket

# ══════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════

STARTING_BANKROLL    = float(os.getenv("STARTING_BANKROLL", "300.0"))
BET_SIZE_PCT         = float(os.getenv("BET_SIZE_PCT", "0.05"))
MIN_BET              = float(os.getenv("MIN_BET", "2.0"))
MAX_BET              = float(os.getenv("MAX_BET", "25.0"))
ENTRY_SECONDS_BEFORE = int(os.getenv("ENTRY_SECONDS_BEFORE", "30"))
MIN_CONFIDENCE_PCT   = float(os.getenv("MIN_CONFIDENCE_PCT", "0.03"))
DRY_RUN              = os.getenv("DRY_RUN", "true").lower() == "true"
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
DASHBOARD_PORT       = int(os.getenv("PORT", "8080"))

WINDOW_SIZE = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("btc5m")

# ══════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════

@dataclass
class WindowState:
    window_ts: int
    open_price: float
    current_price: float
    traded: bool = False

@dataclass
class Trade:
    window_ts: int
    slug: str
    side: str
    bet_size: float
    token_price: float
    potential_profit: float
    delta_pct: float
    timestamp: str
    result: Optional[str] = None
    pnl: Optional[float] = None

@dataclass
class BankrollPoint:
    timestamp: str
    value: float
    label: str  # "start", "win", "loss", "init"

@dataclass
class BotState:
    bankroll: float = STARTING_BANKROLL
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    trade_log: List[Trade] = field(default_factory=list)
    bankroll_history: List[BankrollPoint] = field(default_factory=list)
    paused: bool = False  # Telegram /pause command

# ══════════════════════════════════════════════
# PRIX TOKEN SIMULÉ
# ══════════════════════════════════════════════

def estimate_token_price(delta_pct: float, side: str) -> float:
    abs_delta = abs(delta_pct)
    if abs_delta < 0.005:   prob_up = 0.50
    elif abs_delta < 0.02:  prob_up = 0.50 + (abs_delta - 0.005) / 0.015 * 0.05
    elif abs_delta < 0.05:  prob_up = 0.55 + (abs_delta - 0.02)  / 0.03  * 0.10
    elif abs_delta < 0.10:  prob_up = 0.65 + (abs_delta - 0.05)  / 0.05  * 0.15
    elif abs_delta < 0.15:  prob_up = 0.80 + (abs_delta - 0.10)  / 0.05  * 0.12
    else:                   prob_up = min(0.97, 0.92 + (abs_delta - 0.15) * 0.5)
    if delta_pct < 0:
        prob_up = 1.0 - prob_up
    return round(prob_up if side == "YES" else 1.0 - prob_up, 4)

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════

class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.last_update_id = 0
        self.bot_ref = None  # sera injecté après init

    def send(self, msg: str, parse_mode="Markdown"):
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": parse_mode},
                timeout=8
            )
        except Exception as e:
            log.warning(f"Telegram send error: {e}")

    def get_updates(self):
        if not self.token:
            return []
        try:
            resp = requests.get(
                f"{self.base}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 5},
                timeout=10
            )
            updates = resp.json().get("result", [])
            if updates:
                self.last_update_id = updates[-1]["update_id"]
            return updates
        except Exception:
            return []

    def handle_command(self, text: str, from_chat: str) -> str:
        """Traite les commandes Telegram et retourne la réponse."""
        bot = self.bot_ref
        if bot is None:
            return "Bot non initialisé."

        text = text.strip().lower().split()[0] if text.strip() else ""
        state = bot.state
        price = bot.get_btc_price()
        wr = (state.wins / state.total_trades * 100) if state.total_trades > 0 else 0

        if text == "/start" or text == "/help":
            return (
                "🤖 *BTC 5-Min Bot — Commandes*\n\n"
                "`/status` — État du bot\n"
                "`/stats` — Statistiques détaillées\n"
                "`/pause` — Mettre en pause\n"
                "`/resume` — Reprendre\n"
                "`/last` — Dernier trade\n"
                "`/btc` — Prix BTC actuel\n"
                "`/bankroll` — Bankroll simulée\n"
                "`/help` — Cette aide"
            )

        elif text == "/status":
            paused = "⏸ *EN PAUSE*" if state.paused else "▶️ *EN COURS*"
            return (
                f"📊 *Status du Bot*\n\n"
                f"Mode: {paused}\n"
                f"BTC: `${price:,.2f}`\n"
                f"Bankroll: `${state.bankroll:.2f}`\n"
                f"PnL: `{'+'if state.total_pnl>=0 else ''}{state.total_pnl:.2f}$`\n"
                f"Trades: `{state.total_trades}` (W:{state.wins} L:{state.losses})\n"
                f"Win Rate: `{wr:.1f}%`"
            )

        elif text == "/stats":
            avg_win = 0.0
            avg_loss = 0.0
            wins_list = [t.pnl for t in state.trade_log if t.result == "WIN" and t.pnl]
            loss_list = [t.pnl for t in state.trade_log if t.result == "LOSS" and t.pnl]
            if wins_list: avg_win = sum(wins_list) / len(wins_list)
            if loss_list: avg_loss = sum(loss_list) / len(loss_list)
            return (
                f"📈 *Statistiques Détaillées*\n\n"
                f"Trades total: `{state.total_trades}`\n"
                f"Victoires: `{state.wins}` ✅\n"
                f"Défaites: `{state.losses}` ❌\n"
                f"Win Rate: `{wr:.1f}%`\n"
                f"PnL total: `{'+'if state.total_pnl>=0 else ''}{state.total_pnl:.2f}$`\n"
                f"Gain moyen/win: `+${avg_win:.2f}`\n"
                f"Perte moy/loss: `${avg_loss:.2f}`\n"
                f"Bankroll init: `${STARTING_BANKROLL:.2f}`\n"
                f"Bankroll actuelle: `${state.bankroll:.2f}`\n"
                f"ROI: `{'+'if state.total_pnl>=0 else ''}{state.total_pnl/STARTING_BANKROLL*100:.1f}%`"
            )

        elif text == "/pause":
            state.paused = True
            return "⏸ *Bot mis en pause.* Utilisez /resume pour reprendre."

        elif text == "/resume":
            state.paused = False
            return "▶️ *Bot repris.* Trading actif."

        elif text == "/last":
            recent = [t for t in state.trade_log if t.result is not None]
            if not recent:
                return "Aucun trade résolu pour l'instant."
            t = recent[-1]
            emoji = "✅" if t.result == "WIN" else "❌"
            return (
                f"{emoji} *Dernier Trade*\n\n"
                f"Side: `{t.side}`\n"
                f"Delta: `{t.delta_pct:+.4f}%`\n"
                f"Token: `${t.token_price:.3f}`\n"
                f"Mise: `${t.bet_size:.2f}`\n"
                f"PnL: `{'+'if t.pnl>=0 else ''}{t.pnl:.2f}$`\n"
                f"Résultat: `{t.result}`"
            )

        elif text == "/btc":
            op = bot.current_window.open_price if bot.current_window else 0
            delta = (price - op) / op * 100 if op > 0 else 0
            return (
                f"₿ *Prix Bitcoin*\n\n"
                f"Prix actuel: `${price:,.2f}`\n"
                f"Open fenêtre: `${op:,.2f}`\n"
                f"Delta: `{delta:+.4f}%`\n"
                f"Clôture dans: `{bot.seconds_left}s`"
            )

        elif text == "/bankroll":
            roi = (state.total_pnl / STARTING_BANKROLL * 100)
            bar_len = 20
            filled = int(min(bar_len, max(0, state.bankroll / STARTING_BANKROLL * bar_len)))
            bar = "█" * filled + "░" * (bar_len - filled)
            return (
                f"💰 *Bankroll Simulée*\n\n"
                f"`{bar}`\n"
                f"Départ: `${STARTING_BANKROLL:.2f}`\n"
                f"Actuelle: `${state.bankroll:.2f}`\n"
                f"ROI: `{'+'if roi>=0 else ''}{roi:.1f}%`"
            )

        else:
            return f"Commande inconnue: `{text}`\nTapez /help pour la liste."

    def poll_loop(self):
        """Boucle de polling des commandes Telegram."""
        log.info("Telegram polling démarré")
        while True:
            try:
                updates = self.get_updates()
                for u in updates:
                    msg = u.get("message", {})
                    text = msg.get("text", "")
                    from_chat = str(msg.get("chat", {}).get("id", ""))
                    if text.startswith("/"):
                        response = self.handle_command(text, from_chat)
                        # Répondre au chat d'où vient la commande
                        try:
                            requests.post(
                                f"{self.base}/sendMessage",
                                json={"chat_id": from_chat or self.chat_id,
                                      "text": response, "parse_mode": "Markdown"},
                                timeout=8
                            )
                        except Exception as e:
                            log.warning(f"Telegram reply error: {e}")
            except Exception as e:
                log.warning(f"Telegram poll error: {e}")
            time.sleep(2)

# ══════════════════════════════════════════════
# BOT CORE
# ══════════════════════════════════════════════

class BTC5mBot:
    def __init__(self):
        self.state = BotState()
        self.btc_price: float = 0.0
        self.btc_price_lock = threading.Lock()
        self.current_window: Optional[WindowState] = None
        self.running = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_signal = "—"
        self.seconds_left = 300

        # Telegram
        self.telegram = TelegramBot(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        self.telegram.bot_ref = self

        # Point initial bankroll
        self.state.bankroll_history.append(BankrollPoint(
            timestamp=datetime.now(timezone.utc).isoformat(),
            value=STARTING_BANKROLL,
            label="init"
        ))

    # ── WebSocket Binance ────────────────────────

    def _on_ws_message(self, ws, message):
        try:
            with self.btc_price_lock:
                self.btc_price = float(json.loads(message)["p"])
        except Exception:
            pass

    def _on_ws_close(self, ws, *args):
        if self.running:
            time.sleep(5)
            self._start_ws()

    def _start_ws(self):
        ws = websocket.WebSocketApp(
            "wss://stream.binance.com:9443/ws/btcusdt@trade",
            on_message=self._on_ws_message,
            on_error=lambda ws, e: log.warning(f"WS: {e}"),
            on_close=self._on_ws_close,
            on_open=lambda ws: log.info("Binance WS connecte"),
        )
        threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 30}, daemon=True).start()

    def get_btc_price(self) -> float:
        with self.btc_price_lock:
            return self.btc_price

    # ── Fenêtres ─────────────────────────────────

    def get_current_window_ts(self) -> int:
        now = int(time.time())
        return now - (now % WINDOW_SIZE)

    def seconds_until_close(self) -> int:
        now = int(time.time())
        return WINDOW_SIZE - (now - self.get_current_window_ts())

    def update_window(self):
        wts = self.get_current_window_ts()
        price = self.get_btc_price()
        if price <= 0:
            return
        if self.current_window is None or self.current_window.window_ts != wts:
            self.current_window = WindowState(window_ts=wts, open_price=price, current_price=price)
            log.info(f"Nouvelle fenetre {wts} | open=${price:,.2f}")
            gc.collect()
        else:
            self.current_window.current_price = price

    # ── Signal ───────────────────────────────────

    def compute_signal(self) -> Optional[str]:
        if not self.current_window or self.current_window.open_price <= 0:
            return None
        delta = (self.current_window.current_price - self.current_window.open_price) / self.current_window.open_price * 100
        return ("YES" if delta > 0 else "NO") if abs(delta) >= MIN_CONFIDENCE_PCT else None

    # ── Trade ────────────────────────────────────

    def place_trade(self, side: str):
        if self.state.paused:
            log.info("Bot en pause, trade ignoré")
            return
        w = self.current_window
        delta_pct = (w.current_price - w.open_price) / w.open_price * 100
        token_price = estimate_token_price(delta_pct, side)
        bet = round(max(MIN_BET, min(MAX_BET, self.state.bankroll * BET_SIZE_PCT)), 2)
        if bet > self.state.bankroll:
            return
        profit = round(bet * (1.0 / token_price - 1), 2)
        slug = f"btc-updown-5m-{w.window_ts}"
        trade = Trade(
            window_ts=w.window_ts, slug=slug, side=side,
            bet_size=bet, token_price=token_price,
            potential_profit=profit, delta_pct=round(delta_pct, 5),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.state.trade_log.append(trade)
        self.state.total_trades += 1
        w.traded = True
        self.last_signal = f"{'UP' if side=='YES' else 'DOWN'} @ ${token_price:.3f}"
        if len(self.state.trade_log) > 500:
            self.state.trade_log = self.state.trade_log[-200:]

        log.info(f"TRADE {side} delta={delta_pct:+.4f}% token=${token_price:.3f} mise=${bet:.2f}")
        self.telegram.send(
            f"*[DRY RUN] 🎯 TRADE {'📈' if side=='YES' else '📉'} {side}*\n"
            f"Delta: `{delta_pct:+.4f}%` | Token: `${token_price:.3f}`\n"
            f"Mise: `${bet:.2f}` → potentiel `+${profit:.2f}`\n"
            f"Bankroll: `${self.state.bankroll:.2f}`"
        )
        threading.Timer(self.seconds_until_close() + 3, self._resolve_trade, args=[trade]).start()

    def _resolve_trade(self, trade: Trade):
        import random
        won = random.random() < trade.token_price
        ts = datetime.now(timezone.utc).isoformat()
        if won:
            pnl = round(trade.bet_size * (1.0 / trade.token_price - 1), 2)
            trade.result, trade.pnl = "WIN", pnl
            self.state.wins += 1
            self.state.bankroll = round(self.state.bankroll + pnl, 4)
            self.state.total_pnl = round(self.state.total_pnl + pnl, 4)
            self.state.bankroll_history.append(BankrollPoint(ts, self.state.bankroll, "win"))
            log.info(f"WIN +${pnl:.2f} | Bankroll ${self.state.bankroll:.2f}")
            self.telegram.send(
                f"✅ *WIN* `+${pnl:.2f}`\n"
                f"Bankroll: `${self.state.bankroll:.2f}` "
                f"({'+'if self.state.total_pnl>=0 else ''}{self.state.total_pnl:.2f}$ au total)"
            )
        else:
            pnl = -trade.bet_size
            trade.result, trade.pnl = "LOSS", pnl
            self.state.losses += 1
            self.state.bankroll = round(self.state.bankroll + pnl, 4)
            self.state.total_pnl = round(self.state.total_pnl + pnl, 4)
            self.state.bankroll_history.append(BankrollPoint(ts, self.state.bankroll, "loss"))
            log.info(f"LOSS -${trade.bet_size:.2f} | Bankroll ${self.state.bankroll:.2f}")
            self.telegram.send(
                f"❌ *LOSS* `-${trade.bet_size:.2f}`\n"
                f"Bankroll: `${self.state.bankroll:.2f}` "
                f"({'+'if self.state.total_pnl>=0 else ''}{self.state.total_pnl:.2f}$ au total)"
            )
        # Limiter l'historique
        if len(self.state.bankroll_history) > 300:
            self.state.bankroll_history = self.state.bankroll_history[-200:]

    # ── Boucle principale ────────────────────────

    def run(self):
        log.info("BTC 5-Min Bot demarre")
        self._start_ws()

        # Démarrer le polling Telegram
        if TELEGRAM_TOKEN:
            threading.Thread(target=self.telegram.poll_loop, daemon=True).start()

        for _ in range(30):
            if self.get_btc_price() > 0:
                break
            time.sleep(1)

        self.telegram.send(
            f"🤖 *Bot BTC 5-min démarré* (DRY RUN)\n"
            f"BTC: `${self.get_btc_price():,.2f}`\n"
            f"Bankroll: `${self.state.bankroll:.2f}`\n"
            f"Tapez /help pour les commandes."
        )

        while self.running:
            try:
                self.update_window()
                self.seconds_left = self.seconds_until_close()

                if (self.current_window
                        and not self.current_window.traded
                        and not self.state.paused
                        and 5 < self.seconds_left <= ENTRY_SECONDS_BEFORE):
                    signal = self.compute_signal()
                    if signal:
                        self.place_trade(signal)
                    else:
                        self.current_window.traded = True
                        self.last_signal = "skip"
                time.sleep(1)
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                log.error(f"Erreur boucle: {e}")
                time.sleep(2)

# ══════════════════════════════════════════════
# DASHBOARD HTML
# ══════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 5-Min Bot</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{
  --bg:#070b0f;--bg2:#0d1319;--bg3:#0a1018;
  --border:#1e2d3d;--border2:#243447;
  --accent:#00d4ff;--green:#00ff9d;--red:#ff3c5a;--yellow:#ffd700;
  --text:#c8d8e8;--text2:#6a8aaa;--text3:#3a5570;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;min-height:100vh}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.02) 2px,rgba(0,0,0,.02) 4px)}

header{position:relative;z-index:10;display:flex;align-items:center;justify-content:space-between;
  padding:14px 24px;border-bottom:1px solid var(--border);
  background:linear-gradient(180deg,rgba(0,212,255,.05),transparent)}
.logo{display:flex;align-items:center;gap:12px}
.licon{width:36px;height:36px;border:1px solid var(--accent);border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:17px;color:var(--accent)}
.ltitle{font-family:'Rajdhani',sans-serif;font-size:18px;font-weight:700;letter-spacing:2px;color:var(--accent)}
.lsub{font-size:8px;color:var(--text3);letter-spacing:3px}
.hright{display:flex;align-items:center;gap:14px}
.badge{font-size:9px;letter-spacing:1px;font-weight:700;padding:3px 8px;border-radius:4px}
.bdry{background:rgba(255,215,0,.1);color:var(--yellow);border:1px solid rgba(255,215,0,.25)}
.bpause{background:rgba(255,60,90,.1);color:var(--red);border:1px solid rgba(255,60,90,.25);display:none}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:blink 1.2s ease-in-out infinite}
.htime{font-size:11px;color:var(--text2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

.main{position:relative;z-index:10;padding:18px 24px;display:grid;
  grid-template-columns:repeat(4,1fr);gap:12px}

.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;position:relative;overflow:hidden;transition:border-color .3s}
.card:hover{border-color:var(--border2)}
.cap{position:absolute;top:0;left:0;right:0;height:2px;border-radius:10px 10px 0 0}
.ca{background:linear-gradient(90deg,transparent,var(--accent),transparent)}
.cg{background:linear-gradient(90deg,transparent,var(--green),transparent)}
.cy{background:linear-gradient(90deg,transparent,var(--yellow),transparent)}
.cr{background:linear-gradient(90deg,transparent,var(--red),transparent)}
.clbl{font-size:8px;letter-spacing:2.5px;color:var(--text3);text-transform:uppercase;margin-bottom:9px}
.cval{font-family:'Rajdhani',sans-serif;font-size:32px;font-weight:700;line-height:1}
.va{color:var(--accent)}.vg{color:var(--green)}.vy{color:var(--yellow)}.vr{color:var(--red)}
.csub{font-size:10px;color:var(--text3);margin-top:5px}

.card-btc{grid-column:span 2;background:linear-gradient(135deg,var(--bg2),#0d1a24)}
.btcn{font-family:'Rajdhani',sans-serif;font-size:46px;font-weight:700;color:var(--accent);line-height:1}
.btctag{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:4px;margin-top:6px}
.btctag.up{background:rgba(0,255,157,.1);color:var(--green)}
.btctag.dn{background:rgba(255,60,90,.1);color:var(--red)}
.btctag.fl{background:rgba(255,255,255,.05);color:var(--text2)}

.card-cd{display:flex;flex-direction:column}
.cd-row{display:flex;align-items:center;gap:14px}
.ring{position:relative;width:76px;height:76px;flex-shrink:0}
.ring svg{transform:rotate(-90deg)}
circle.rbg{fill:none;stroke:var(--border2);stroke-width:4}
circle.rp{fill:none;stroke:var(--accent);stroke-width:4;stroke-linecap:round;stroke-dasharray:220}
.rnum{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  font-family:sans-serif;font-size:19px;font-weight:700;color:var(--accent);line-height:1}
.rsub{font-size:7px;color:var(--text3);letter-spacing:1px}
.entry-badge{font-size:9px;padding:3px 9px;border-radius:4px;display:inline-block;margin-top:7px;font-weight:700;letter-spacing:1px}
.ea{background:rgba(0,255,157,.12);color:var(--green);animation:pulse 1s infinite}
.ew{background:rgba(0,0,0,.2);color:var(--text3)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}

.bar-track{width:100%;height:5px;background:var(--border);border-radius:3px;overflow:hidden;margin-top:8px}
.bar-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,#00cc7a,var(--green));transition:width .6s}

/* ── CHART BANKROLL ── */
.card-chart{grid-column:span 4;padding:16px 18px}
.chart-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.chart-pills{display:flex;gap:8px}
.pill{font-size:9px;padding:3px 9px;border-radius:20px;cursor:pointer;border:1px solid var(--border2);
  color:var(--text3);transition:all .2s;letter-spacing:1px}
.pill.active{background:rgba(0,212,255,.1);border-color:var(--accent);color:var(--accent)}
.chart-wrap{position:relative;height:160px}

/* ── TABLE ── */
.card-table{grid-column:span 4}
.tbl-wrap{overflow-x:auto;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:11px}
thead th{font-size:8px;letter-spacing:2px;color:var(--text3);padding:7px 9px;
  text-align:left;border-bottom:1px solid var(--border);text-transform:uppercase}
tbody tr{border-bottom:1px solid rgba(30,45,61,.4);transition:background .2s}
tbody tr:hover{background:rgba(0,212,255,.02)}
tbody td{padding:7px 9px;color:var(--text)}
.tag{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:3px}
.tY{background:rgba(0,255,157,.12);color:var(--green)}
.tN{background:rgba(255,60,90,.12);color:var(--red)}
.tW{background:rgba(0,255,157,.1);color:var(--green)}
.tL{background:rgba(255,60,90,.1);color:var(--red)}
.tP{background:rgba(255,215,0,.1);color:var(--yellow)}
.pos{color:var(--green)}.neg{color:var(--red)}

/* ── TELEGRAM PANEL ── */
.card-tg{grid-column:span 2}
.tg-header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.tg-icon{width:28px;height:28px;border-radius:50%;background:rgba(0,136,204,.2);
  border:1px solid rgba(0,136,204,.3);display:flex;align-items:center;justify-content:center;font-size:14px}
.tg-title{font-size:12px;font-weight:700;color:#29b6f6}
.tg-status{font-size:9px;color:var(--text3)}
.tg-cmds{display:flex;flex-direction:column;gap:6px;margin-top:4px}
.tg-cmd{display:flex;align-items:center;gap:8px;padding:6px 10px;
  background:rgba(0,0,0,.2);border-radius:6px;border:1px solid var(--border);
  font-size:10px;cursor:pointer;transition:border-color .2s;text-decoration:none;color:var(--text)}
.tg-cmd:hover{border-color:var(--border2)}
.tg-cmd .cmd{color:#29b6f6;font-weight:700;min-width:72px}
.tg-connected{display:flex;align-items:center;gap:6px;font-size:10px;padding:8px 12px;
  background:rgba(41,182,246,.08);border-radius:6px;border:1px solid rgba(41,182,246,.2);margin-bottom:10px}
.tg-dot{width:6px;height:6px;border-radius:50%;background:#29b6f6;box-shadow:0 0 5px #29b6f6}
.tg-disconnected{display:flex;align-items:center;gap:6px;font-size:10px;padding:8px 12px;
  background:rgba(255,60,90,.05);border-radius:6px;border:1px solid rgba(255,60,90,.15);margin-bottom:10px;color:var(--text3)}

footer{position:relative;z-index:10;border-top:1px solid var(--border);
  padding:10px 24px;display:flex;justify-content:space-between;
  font-size:9px;color:var(--text3)}

@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.card{animation:fadeUp .4s ease both}
.card:nth-child(1){animation-delay:.04s}.card:nth-child(2){animation-delay:.08s}
.card:nth-child(3){animation-delay:.12s}.card:nth-child(4){animation-delay:.16s}
.card:nth-child(5){animation-delay:.20s}.card:nth-child(6){animation-delay:.24s}
.card:nth-child(7){animation-delay:.28s}.card:nth-child(8){animation-delay:.32s}
.card:nth-child(9){animation-delay:.36s}

@media(max-width:860px){
  .main{grid-template-columns:repeat(2,1fr);padding:12px}
  .card-btc,.card-chart,.card-table,.card-tg{grid-column:span 2}
}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="licon">₿</div>
    <div>
      <div class="ltitle">BTC5M · BOT</div>
      <div class="lsub">POLYMARKET · DRY RUN</div>
    </div>
  </div>
  <div class="hright">
    <span class="badge bdry">DRY RUN</span>
    <span class="badge bpause" id="pause-badge">⏸ PAUSE</span>
    <div class="dot"></div>
    <span class="htime" id="clock">—</span>
  </div>
</header>

<div class="main">

  <!-- BTC -->
  <div class="card card-btc" id="card-btc">
    <div class="cap ca"></div>
    <div class="clbl">Prix Bitcoin · Binance WS</div>
    <div class="btcn" id="btc-price">—</div>
    <div id="btc-tag" class="btctag fl">—</div>
    <div class="csub" id="open-label">Open fenêtre: —</div>
  </div>

  <!-- Countdown -->
  <div class="card card-cd">
    <div class="cap ca"></div>
    <div class="clbl">Clôture fenêtre</div>
    <div class="cd-row">
      <div class="ring">
        <svg width="76" height="76" viewBox="0 0 76 76">
          <circle class="rbg" cx="38" cy="38" r="34"/>
          <circle class="rp" id="ring" cx="38" cy="38" r="34" stroke-dashoffset="0"/>
        </svg>
        <div class="rnum"><span id="secs">—</span><span class="rsub">SEC</span></div>
      </div>
      <div>
        <div class="csub">Fenêtre 5 min</div>
        <div id="entry-badge" class="entry-badge ew">⏳ En attente</div>
      </div>
    </div>
  </div>

  <!-- Bankroll -->
  <div class="card">
    <div class="cap cg"></div>
    <div class="clbl">Bankroll simulée</div>
    <div class="cval vg" id="bankroll">—</div>
    <div class="csub" id="pnl-sub">PnL: —</div>
  </div>

  <!-- Trades -->
  <div class="card">
    <div class="cap ca"></div>
    <div class="clbl">Trades</div>
    <div class="cval va" id="total-trades">0</div>
    <div class="csub" id="wl-sub">W: 0 | L: 0</div>
  </div>

  <!-- Win Rate -->
  <div class="card">
    <div class="cap cg"></div>
    <div class="clbl">Win Rate</div>
    <div class="cval vg" id="winrate">—</div>
    <div class="bar-track"><div class="bar-fill" id="wr-bar" style="width:0%"></div></div>
  </div>

  <!-- Signal -->
  <div class="card">
    <div class="cap cy"></div>
    <div class="clbl">Dernier signal</div>
    <div class="cval vy" style="font-size:18px;margin-top:3px" id="last-signal">—</div>
    <div class="csub" id="delta-sub">Delta fenêtre: —</div>
  </div>

  <!-- ── GRAPHIQUE BANKROLL ── -->
  <div class="card card-chart">
    <div class="cap cg"></div>
    <div class="chart-header">
      <div class="clbl" style="margin-bottom:0">Évolution du Bankroll</div>
      <div class="chart-pills">
        <span class="pill active" onclick="setRange(0, this)">Tout</span>
        <span class="pill" onclick="setRange(20, this)">20 trades</span>
        <span class="pill" onclick="setRange(10, this)">10 trades</span>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas id="bankroll-chart"></canvas>
    </div>
  </div>

  <!-- ── TELEGRAM PANEL ── -->
  <div class="card card-tg">
    <div class="cap" style="background:linear-gradient(90deg,transparent,#29b6f6,transparent)"></div>
    <div class="clbl">Connexion Telegram</div>
    <div id="tg-status-block" class="tg-disconnected">
      <div class="tg-dot" style="background:var(--text3);box-shadow:none"></div>
      <span>Non configuré — ajouter TELEGRAM_TOKEN</span>
    </div>
    <div class="tg-cmds">
      <div class="tg-cmd"><span class="cmd">/status</span><span>État du bot</span></div>
      <div class="tg-cmd"><span class="cmd">/stats</span><span>Statistiques détaillées</span></div>
      <div class="tg-cmd"><span class="cmd">/pause</span><span>Suspendre le trading</span></div>
      <div class="tg-cmd"><span class="cmd">/resume</span><span>Reprendre le trading</span></div>
      <div class="tg-cmd"><span class="cmd">/last</span><span>Dernier trade résolu</span></div>
      <div class="tg-cmd"><span class="cmd">/btc</span><span>Prix BTC + delta</span></div>
      <div class="tg-cmd"><span class="cmd">/bankroll</span><span>Bankroll + ROI</span></div>
    </div>
  </div>

  <!-- TABLE -->
  <div class="card card-table">
    <div class="clbl">Historique des trades (30 derniers)</div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr><th>Heure</th><th>Slug</th><th>Side</th><th>Delta</th>
          <th>Token</th><th>Mise</th><th>Potentiel</th><th>Résultat</th><th>PnL</th></tr>
        </thead>
        <tbody id="tbody">
          <tr><td colspan="9" style="text-align:center;color:var(--text3);padding:24px 0">
            En attente du premier trade…
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>
<footer>
  <span>BTC 5-Min Bot · Polymarket DRY RUN</span>
  <span id="uptime">—</span>
  <span>Binance WS · Chart.js</span>
</footer>

<script>
let startedAt=null, chartRange=0;
const f2=n=>Number(n).toFixed(2);
const f4=n=>Number(n).toFixed(4);
const usd=n=>'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

/* ── CHART SETUP ── */
const ctx=document.getElementById('bankroll-chart').getContext('2d');
const bankrollChart=new Chart(ctx,{
  type:'line',
  data:{labels:[],datasets:[{
    label:'Bankroll',data:[],
    borderColor:'#00ff9d',borderWidth:2,
    pointRadius:3,pointBackgroundColor:'#00ff9d',
    pointHoverRadius:5,
    fill:true,
    backgroundColor:(ctx2)=>{
      const g=ctx2.chart.ctx.createLinearGradient(0,0,0,160);
      g.addColorStop(0,'rgba(0,255,157,0.18)');
      g.addColorStop(1,'rgba(0,255,157,0.0)');
      return g;
    },
    tension:0.35
  },{
    label:'Baseline',data:[],
    borderColor:'rgba(255,215,0,0.25)',borderWidth:1,
    borderDash:[5,5],pointRadius:0,fill:false
  }]},
  options:{
    responsive:true,maintainAspectRatio:false,
    animation:{duration:400},
    plugins:{legend:{display:false},tooltip:{
      backgroundColor:'#0d1319',borderColor:'#1e2d3d',borderWidth:1,
      titleColor:'#c8d8e8',bodyColor:'#6a8aaa',
      callbacks:{label:ctx=>`  ${usd(ctx.raw)}`}
    }},
    scales:{
      x:{ticks:{color:'#3a5570',font:{size:9},maxTicksLimit:8},
         grid:{color:'rgba(30,45,61,0.5)'}},
      y:{ticks:{color:'#3a5570',font:{size:9},callback:v=>'$'+v.toFixed(0)},
         grid:{color:'rgba(30,45,61,0.5)'}}
    }
  }
});

let allHistory=[];
function setRange(n,el){
  chartRange=n;
  document.querySelectorAll('.pill').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  renderChart();
}
function renderChart(){
  if(!allHistory.length) return;
  const pts=chartRange>0?allHistory.slice(-chartRange):allHistory;
  const labels=pts.map(p=>{
    const d=new Date(p.timestamp);
    return d.toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'});
  });
  const vals=pts.map(p=>p.value);
  const baseline=pts.map(()=>parseFloat('{{ starting_bankroll }}') || 300);
  bankrollChart.data.labels=labels;
  bankrollChart.data.datasets[0].data=vals;
  bankrollChart.data.datasets[1].data=baseline;

  // Colorier les points WIN/LOSS
  const pointColors=pts.map(p=>p.label==='win'?'#00ff9d':p.label==='loss'?'#ff3c5a':'#00d4ff');
  bankrollChart.data.datasets[0].pointBackgroundColor=pointColors;
  bankrollChart.update('none');
}

/* ── CLOCK / UPTIME ── */
setInterval(()=>{
  document.getElementById('clock').textContent=new Date().toLocaleTimeString('fr-FR',{hour12:false});
  if(startedAt){
    const d=Math.floor((Date.now()-new Date(startedAt))/1000);
    const h=Math.floor(d/3600).toString().padStart(2,'0');
    const m=Math.floor((d%3600)/60).toString().padStart(2,'0');
    const s=(d%60).toString().padStart(2,'0');
    document.getElementById('uptime').textContent=`Uptime: ${h}:${m}:${s}`;
  }
},1000);

/* ── REFRESH ── */
async function refresh(){
  try{
    const d=await(await fetch('/api/state')).json();
    startedAt=d.started_at;

    // Pause badge
    document.getElementById('pause-badge').style.display=d.paused?'inline-block':'none';

    // BTC
    const bEl=document.getElementById('btc-price');
    bEl.textContent=d.btc_price>0?usd(d.btc_price):'—';
    const op=d.open_price;
    document.getElementById('open-label').textContent=op>0?`Open fenêtre: ${usd(op)}`:'Open fenêtre: —';
    const tagEl=document.getElementById('btc-tag');
    if(op>0&&d.btc_price>0){
      const pct=(d.btc_price-op)/op*100;
      tagEl.textContent=(pct>=0?'+':'')+f4(pct)+'%';
      tagEl.className='btctag '+(pct>0.001?'up':pct<-0.001?'dn':'fl');
      document.getElementById('delta-sub').textContent='Delta fenêtre: '+(pct>=0?'+':'')+f4(pct)+'%';
    }

    // Countdown
    const s=d.seconds_left;
    document.getElementById('secs').textContent=s;
    const C=213;
    document.getElementById('ring').style.strokeDashoffset=C*(s/300);
    document.getElementById('ring').style.stroke=s<=30?'var(--green)':'var(--accent)';
    const badge=document.getElementById('entry-badge');
    if(s<=30&&s>5){badge.className='entry-badge ea';badge.textContent="🎯 ZONE D'ENTRÉE";}
    else{badge.className='entry-badge ew';badge.textContent=`⏳ Entrée dans ${Math.max(0,s-30)}s`;}

    // Stats
    const bk=document.getElementById('bankroll');
    bk.textContent=usd(d.bankroll);
    bk.className='cval '+(d.bankroll>=parseFloat('{{ starting_bankroll }}'||300)?'vg':'vr');
    document.getElementById('pnl-sub').textContent='PnL: '+(d.total_pnl>=0?'+':'')+usd(d.total_pnl);
    document.getElementById('total-trades').textContent=d.total_trades;
    document.getElementById('wl-sub').textContent=`W: ${d.wins} | L: ${d.losses}`;
    const wr=d.total_trades>0?(d.wins/d.total_trades*100):0;
    document.getElementById('winrate').textContent=f2(wr)+'%';
    document.getElementById('wr-bar').style.width=wr+'%';
    document.getElementById('last-signal').textContent=d.last_signal||'—';

    // Chart
    if(d.bankroll_history&&d.bankroll_history.length>0){
      allHistory=d.bankroll_history;
      renderChart();
    }

    // Telegram status
    const tgBlock=document.getElementById('tg-status-block');
    if(d.telegram_ok){
      tgBlock.className='tg-connected';
      tgBlock.innerHTML='<div class="tg-dot"></div><span style="color:#29b6f6">Connecté · <b>@'+d.bot_username+'</b></span>';
    }

    // Table
    if(d.trade_log&&d.trade_log.length>0){
      const rows=[...d.trade_log].reverse().slice(0,30).map(t=>{
        const ts=new Date(t.timestamp).toLocaleTimeString('fr-FR');
        const slug=t.slug?(t.slug.replace('btc-updown-5m-','…')):'—';
        const side=t.side==='YES'?'<span class="tag tY">↑ YES</span>':'<span class="tag tN">↓ NO</span>';
        const dc=t.delta_pct>0?'var(--green)':'var(--red)';
        let res='<span class="tag tP">⏳</span>';
        if(t.result==='WIN')res='<span class="tag tW">✅ WIN</span>';
        if(t.result==='LOSS')res='<span class="tag tL">❌ LOSS</span>';
        const pnl=t.pnl!=null?`<span class="${t.pnl>=0?'pos':'neg'}">${t.pnl>=0?'+':''}${usd(t.pnl)}</span>`:'—';
        return `<tr><td style="color:var(--text2)">${ts}</td><td style="color:var(--text3);font-size:9px">${slug}</td><td>${side}</td>
<td style="color:${dc}">${t.delta_pct>=0?'+':''}${f4(t.delta_pct)}%</td>
<td>${usd(t.token_price)}</td><td>${usd(t.bet_size)}</td>
<td class="pos">+${usd(t.potential_profit)}</td><td>${res}</td><td>${pnl}</td></tr>`;
      }).join('');
      document.getElementById('tbody').innerHTML=rows;
    }
  }catch(e){console.warn('API',e);}
}

setInterval(refresh,1000);
refresh();
</script>
</body>
</html>"""

# ══════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════

def create_app(bot: BTC5mBot) -> Flask:
    app = Flask(__name__)

    # Vérifier si le bot Telegram est connecté
    def check_telegram():
        if not TELEGRAM_TOKEN:
            return False, ""
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
                timeout=5
            )
            data = r.json()
            if data.get("ok"):
                return True, data["result"].get("username", "")
        except Exception:
            pass
        return False, ""

    tg_ok, tg_username = check_telegram()

    @app.route("/")
    def index():
        html = DASHBOARD_HTML.replace(
            "{{ starting_bankroll }}", str(STARTING_BANKROLL)
        )
        return render_template_string(html)

    @app.route("/api/state")
    def api_state():
        trades = []
        for t in bot.state.trade_log[-50:]:
            trades.append({
                "window_ts": t.window_ts, "slug": t.slug, "side": t.side,
                "bet_size": t.bet_size, "token_price": t.token_price,
                "potential_profit": t.potential_profit, "delta_pct": t.delta_pct,
                "timestamp": t.timestamp, "result": t.result, "pnl": t.pnl,
            })
        history = [
            {"timestamp": p.timestamp, "value": p.value, "label": p.label}
            for p in bot.state.bankroll_history[-200:]
        ]
        return jsonify({
            "btc_price": bot.get_btc_price(),
            "open_price": bot.current_window.open_price if bot.current_window else 0,
            "seconds_left": bot.seconds_left,
            "bankroll": bot.state.bankroll,
            "total_trades": bot.state.total_trades,
            "wins": bot.state.wins,
            "losses": bot.state.losses,
            "total_pnl": bot.state.total_pnl,
            "last_signal": bot.last_signal,
            "trade_log": trades,
            "bankroll_history": history,
            "started_at": bot.started_at,
            "paused": bot.state.paused,
            "telegram_ok": tg_ok,
            "bot_username": tg_username,
        })

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "btc": bot.get_btc_price()})

    return app

# ══════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    bot = BTC5mBot()
    threading.Thread(target=bot.run, daemon=True).start()
    app = create_app(bot)
    log.info(f"Dashboard: http://0.0.0.0:{DASHBOARD_PORT}")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)
