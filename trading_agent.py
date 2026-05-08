#!/usr/bin/env python3
"""
AI Trading Agent για uFunded Platform - v2.0
- 16:00: Αρχική ανάλυση & Top 5 signals
- 16:00-17:00: Monitoring κάθε 5 λεπτά
- Alert αν αλλάξει κάτι σημαντικό
"""

import yfinance as yf
import anthropic
import requests
import schedule
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import logging
import os

TELEGRAM_BOT_TOKEN = "8713919672:AAEtVBMT9NsSfvXdHlVygrr7XanJU8GilG4"
TELEGRAM_CHAT_ID = "7235378762"
ANTHROPIC_API_KEY = "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"

LEVERAGE_CAPITAL = 90_000
DAILY_PROFIT_TARGET_EUR = 250
SEND_HOUR = 16
SEND_MINUTE = 0
MONITOR_END_HOUR = 17
MONITOR_INTERVAL_MINUTES = 5

# Κατώφλια αλλαγής για alert
PRICE_CHANGE_ALERT = 0.8    # % αλλαγή τιμής
RSI_CHANGE_ALERT = 8        # μονάδες RSI
VOLUME_SPIKE_ALERT = 2.5    # x φορές μέσο όγκο

UFUNDED_SYMBOLS = {
    "Μετοχές": {
        "AAPL":"Apple Inc.","MSFT":"Microsoft Corp.","GOOGL":"Alphabet (Google)",
        "AMZN":"Amazon.com","NVDA":"NVIDIA Corp.","TSLA":"Tesla Inc.",
        "META":"Meta Platforms","NFLX":"Netflix Inc.","AMD":"Advanced Micro Devices",
        "INTC":"Intel Corp.","BABA":"Alibaba Group","UBER":"Uber Technologies",
        "JPM":"JPMorgan Chase","BAC":"Bank of America","GS":"Goldman Sachs",
        "V":"Visa Inc.","MA":"Mastercard","DIS":"Walt Disney Co.",
        "PYPL":"PayPal Holdings","CRM":"Salesforce Inc.","BA":"Boeing Co.",
        "XOM":"ExxonMobil Corp.","JNJ":"Johnson & Johnson","WMT":"Walmart Inc.",
        "KO":"Coca-Cola Co.","PEP":"PepsiCo Inc.","MCD":"McDonald's Corp.",
        "SBUX":"Starbucks Corp.","NKE":"Nike Inc.","SPOT":"Spotify Technology",
    },
    "Commodities": {
        "GC=F":"Χρυσός (Gold)","SI=F":"Ασήμι (Silver)","CL=F":"Αργό Πετρέλαιο",
        "NG=F":"Φυσικό Αέριο","HG=F":"Χαλκός","PL=F":"Πλατίνα",
        "ZW=F":"Σιτάρι","ZC=F":"Καλαμπόκι","ZS=F":"Σόγια","KC=F":"Καφές",
    },
    "Indices": {
        "SPY":"S&P 500 ETF","QQQ":"NASDAQ 100 ETF","DIA":"Dow Jones ETF","IWM":"Russell 2000 ETF",
    }
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TradingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.all_symbols = {}
        for category, symbols in UFUNDED_SYMBOLS.items():
            for sym, name in symbols.items():
                self.all_symbols[sym] = {"name": name, "category": category}

        # State για monitoring
        self.active_signals = []        # Τα 5 signals που δόθηκαν στις 16:00
        self.snapshot_at_16 = {}       # Τιμές/δείκτες στις 16:00
        self.monitoring_active = False  # Αν τρέχει το monitoring
        self.alerts_sent = set()        # Για να μην στέλνουμε ίδιο alert 2 φορές

    # ─────────────────────────────────────────────
    # ΣΥΛΛΟΓΗ ΔΕΔΟΜΕΝΩΝ
    # ─────────────────────────────────────────────
    def fetch_market_data(self, symbol):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="60d", interval="1d")
            hist_1h = ticker.history(period="5d", interval="5m")

            if hist.empty:
                return None

            closes = hist['Close']
            volumes = hist['Volume']
            current_price = closes.iloc[-1]
            prev_close = closes.iloc[-2]
            change_pct = ((current_price - prev_close) / prev_close) * 100

            ma20 = closes.rolling(20).mean().iloc[-1]
            ma50 = closes.rolling(50).mean().iloc[-1] if len(closes) >= 50 else ma20

            delta = closes.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss
            rsi = (100 - (100 / (1 + rs))).iloc[-1]

            ema12 = closes.ewm(span=12).mean()
            ema26 = closes.ewm(span=26).mean()
            macd_hist = (ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()).iloc[-1]

            high = hist['High']
            low = hist['Low']
            tr = pd.concat([(high-low),(high-closes.shift()).abs(),(low-closes.shift()).abs()],axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]

            avg_vol = volumes.rolling(20).mean().iloc[-1]
            vol_ratio = volumes.iloc[-1] / avg_vol if avg_vol > 0 else 1

            # Τρέχων όγκος από 5λεπτα
            if not hist_1h.empty:
                today_vol_live = hist_1h['Volume'].tail(12).sum()  # τελευταία ώρα
                avg_hourly_vol = hist_1h['Volume'].mean() * 12
                live_vol_ratio = today_vol_live / avg_hourly_vol if avg_hourly_vol > 0 else 1
            else:
                live_vol_ratio = vol_ratio

            return {
                "symbol": symbol,
                "name": self.all_symbols[symbol]["name"],
                "category": self.all_symbols[symbol]["category"],
                "current_price": round(current_price, 4),
                "change_pct": round(change_pct, 2),
                "ma20": round(ma20, 4),
                "ma50": round(ma50, 4),
                "rsi": round(rsi, 2),
                "macd_hist": round(macd_hist, 4),
                "atr": round(atr, 4),
                "vol_ratio": round(vol_ratio, 2),
                "live_vol_ratio": round(live_vol_ratio, 2),
            }
        except Exception as e:
            logger.error(f"Σφάλμα {symbol}: {e}")
            return None

    def fetch_all_market_data(self):
        results = []
        for symbol in self.all_symbols:
            data = self.fetch_market_data(symbol)
            if data:
                results.append(data)
        logger.info(f"✅ Δεδομένα για {len(results)} σύμβολα")
        return results

    # ─────────────────────────────────────────────
    # TELEGRAM
    # ─────────────────────────────────────────────
    def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            try:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
            except Exception as e:
                logger.error(f"Telegram error: {e}")

    # ─────────────────────────────────────────────
    # ΑΡΧΙΚΗ ΑΝΑΛΥΣΗ 16:00
    # ─────────────────────────────────────────────
    def analyze_with_claude(self, market_data):
        data_summary = []
        for d in market_data:
            rsi_signal = "ΥΠΕΡΑΓΟΡΑ" if d['rsi'] > 70 else ("ΥΠΕΡΠΩΛΗΣΗ" if d['rsi'] < 30 else "ΟΥΔΕΤΕΡΟ")
            ma_signal = "ΑΝΟΔΙΚΟ" if d['current_price'] > d['ma50'] else "ΚΑΘΟΔΙΚΟ"
            data_summary.append(
                f"• {d['symbol']} ({d['name']}) [{d['category']}]\n"
                f"  Τιμή: ${d['current_price']} | Σήμερα: {d['change_pct']:+.2f}%\n"
                f"  RSI: {d['rsi']} ({rsi_signal}) | MACD hist: {d['macd_hist']} | MA: {ma_signal}\n"
                f"  ATR: {d['atr']} | Όγκος: {d['vol_ratio']}x"
            )
        today_date = datetime.now().strftime("%d/%m/%Y")
        prompt = f"""Είσαι expert trading analyst για uFunded με μόχλευση $90,000 USD.
ΗΜΕΡΟΜΗΝΙΑ: {today_date} | ΣΤΟΧΟΣ: €250 ημερήσιο κέρδος | ΩΡΑ: 16:00 (κλείσιμο χρηματιστηρίου σε 1 ώρα)

=== ΔΕΔΟΜΕΝΑ ΑΓΟΡΑΣ ===
{chr(10).join(data_summary)}

Επέλεξε τις ΚΑΛΥΤΕΡΕΣ 5 για trading και στείλε ακριβώς σε αυτό το format:

🎯 TOP 5 TRADING SIGNALS - {today_date}

═══════════════════════════
📌 1. [ΣΥΜΒΟΛΟ] - [ΟΝΟΜΑ]
[BUY 🟢 ή SELL 🔴]
💰 Entry: $[τιμή]
🛑 Stop Loss: $[τιμή] (-[%]%)
🎯 Take Profit: $[τιμή] (+[%]%)
💼 Κεφάλαιο: $[ποσό] ([%]%)
📈 Αναμ. Κέρδος: $[USD] ≈ €[EUR]
📊 Σήματα: [σύντομη ανάλυση]
⚡ Εμπιστοσύνη: [Χαμηλό/Μέτριο/Υψηλό]

[επανέλαβε για 2,3,4,5]

━━━━━━━━━━━━━━━━━━━━━━━━━━
💹 ΣΥΝΟΛΙΚΟ ΑΝΑΜ. ΚΕΡΔΟΣ: $[USD] ≈ €[EUR]
⚠️ Παρακολούθηση κάθε 5 λεπτά έως 17:00. Θα ειδοποιηθείς αν αλλάξει κάτι."""

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    def run_daily_analysis(self):
        logger.info("🚀 ΑΝΑΛΥΣΗ 16:00")
        try:
            market_data = self.fetch_all_market_data()
            if not market_data:
                self.send_telegram("⚠️ Δεν ήταν δυνατή η συλλογή δεδομένων.")
                return

            analysis = self.analyze_with_claude(market_data)

            # Αποθήκευση snapshot για monitoring
            self.snapshot_at_16 = {d['symbol']: d for d in market_data}

            # Εξαγωγή symbols από την ανάλυση
            self.active_signals = []
            for d in market_data:
                if d['symbol'] in analysis:
                    self.active_signals.append(d['symbol'])
            self.active_signals = self.active_signals[:5]

            self.alerts_sent = set()
            self.monitoring_active = True

            header = (f"🤖 AI Trading Agent - uFunded\n"
                      f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')} (Ώρα Ελλάδας)\n"
                      f"💼 Κεφάλαιο: $90,000 | 🎯 Στόχος: €250\n"
                      f"🔍 Monitoring κάθε 5λεπτα έως 17:00\n"
                      f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n")
            self.send_telegram(header + analysis)
            logger.info("✅ Αρχική ανάλυση εστάλη! Ξεκινά monitoring...")

        except Exception as e:
            logger.error(f"❌ {e}")
            self.send_telegram(f"❌ Σφάλμα: {e}")

    # ─────────────────────────────────────────────
    # MONITORING 16:00 - 17:00
    # ─────────────────────────────────────────────
    def check_for_changes(self):
        """Τρέχει κάθε 5 λεπτά - ελέγχει αλλαγές"""
        greece_tz = pytz.timezone("Europe/Athens")
        now = datetime.now(greece_tz)

        # Σταμάτα monitoring μετά τις 17:00
        if now.hour >= MONITOR_END_HOUR:
            if self.monitoring_active:
                self.monitoring_active = False
                self.send_telegram("🔴 Monitoring ολοκληρώθηκε για σήμερα.\n📊 Καλή επιτυχία με τις επενδύσεις σου!")
                logger.info("⏹ Monitoring σταμάτησε (17:00)")
            return

        if not self.monitoring_active or not self.snapshot_at_16:
            return

        logger.info(f"🔍 Monitoring check στις {now.strftime('%H:%M')}")

        alerts = []

        # Έλεγξε μόνο τα active signals
        symbols_to_check = self.active_signals if self.active_signals else list(self.all_symbols.keys())[:10]

        for symbol in symbols_to_check:
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1d", interval="5m")
                if hist.empty:
                    continue

                current_price = hist['Close'].iloc[-1]
                current_vol = hist['Volume'].tail(3).sum()

                if symbol not in self.snapshot_at_16:
                    continue

                snap = self.snapshot_at_16[symbol]
                old_price = snap['current_price']
                name = snap['name']

                # % αλλαγή από το 16:00
                price_change = ((current_price - old_price) / old_price) * 100

                alert_key = f"{symbol}_{round(price_change, 1)}"

                # Μεγάλη αλλαγή τιμής
                if abs(price_change) >= PRICE_CHANGE_ALERT and alert_key not in self.alerts_sent:
                    direction = "📈 ΑΝΕΒΗΚΕ" if price_change > 0 else "📉 ΕΠΕΣΕ"
                    emoji = "🟢" if price_change > 0 else "🔴"

                    # AI αξιολόγηση της αλλαγής
                    ai_advice = self.get_ai_alert_advice(symbol, name, old_price, current_price, price_change, snap)

                    alerts.append(
                        f"⚡ ΑΛΛΑΓΗ: {symbol} ({name})\n"
                        f"{direction} {emoji} {price_change:+.2f}%\n"
                        f"💰 Από ${old_price} → ${round(current_price, 4)}\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"{ai_advice}"
                    )
                    self.alerts_sent.add(alert_key)

                # Spike όγκου
                avg_5m_vol = hist['Volume'].mean()
                if avg_5m_vol > 0 and (current_vol / (avg_5m_vol * 3)) >= VOLUME_SPIKE_ALERT:
                    vol_key = f"{symbol}_vol_{now.hour}_{now.minute // 10}"
                    if vol_key not in self.alerts_sent:
                        alerts.append(
                            f"📊 SPIKE ΟΓΚΟΥ: {symbol} ({name})\n"
                            f"🔥 Ασυνήθιστος όγκος τελευταία 15λεπτα!\n"
                            f"💡 Ενδέχεται μεγάλη κίνηση τιμής σύντομα."
                        )
                        self.alerts_sent.add(vol_key)

            except Exception as e:
                logger.error(f"Monitor error {symbol}: {e}")

        if alerts:
            msg = f"🚨 ALERT - {now.strftime('%H:%M')}\n\n" + "\n\n─────────────\n\n".join(alerts)
            self.send_telegram(msg)
            logger.info(f"🚨 Εστάλησαν {len(alerts)} alerts")
        else:
            logger.info(f"✅ {now.strftime('%H:%M')} - Δεν υπάρχουν σημαντικές αλλαγές")

    def get_ai_alert_advice(self, symbol, name, old_price, new_price, change_pct, snap):
        """Claude αξιολογεί αν πρέπει να αλλάξει η θέση"""
        try:
            prompt = f"""Είσαι trading advisor. Ένα signal άλλαξε:

Μετοχή: {symbol} ({name})
Τιμή στις 16:00: ${old_price}
Τώρα: ${round(new_price, 4)} ({change_pct:+.2f}%)
RSI: {snap['rsi']} | ATR: {snap['atr']}

Δώσε ΣΥΝΤΟΜΗ σύσταση (max 3 γραμμές):
- Κράτα θέση / Κλείσε θέση / Άλλαξε Stop Loss
- Γιατί (1 πρόταση)
- Νέο Stop Loss αν χρειάζεται"""

            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        except:
            return "💡 Έλεγξε χειροκίνητα τη θέση σου."


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
def main():
    agent = TradingAgent()
    greece_tz = pytz.timezone("Europe/Athens")

    agent.send_telegram(
        "🟢 AI Trading Agent v2.0 ξεκίνησε!\n"
        "📊 Αρχική ανάλυση: 16:00\n"
        "🔍 Monitoring κάθε 5λεπτα: 16:00-17:00\n"
        "🚨 Alerts αν αλλάξει κάτι σημαντικό\n"
        "🎯 Στόχος: €250/ημέρα"
    )

    logger.info("⏳ Agent έτοιμος - αναμονή...")

    analysis_done_today = False
    last_monitor_check = None

    while True:
        now_greece = datetime.now(greece_tz)
        now_utc = datetime.now(pytz.utc)
        today = now_greece.date()
        current_hour = now_greece.hour
        current_minute = now_greece.minute

        # ── Αρχική ανάλυση ακριβώς στις 16:00 ώρα Ελλάδας ──
        if current_hour == 16 and current_minute == 0 and not analysis_done_today:
            agent.run_daily_analysis()
            analysis_done_today = True

        # Reset για επόμενη μέρα
        if current_hour == 0 and current_minute == 0:
            analysis_done_today = False

        # ── Monitoring κάθε 5 λεπτά μεταξύ 16:00-17:00 ──
        if current_hour == 16 or (current_hour == 16 and current_minute >= 0):
            if last_monitor_check is None or (now_greece - last_monitor_check).seconds >= MONITOR_INTERVAL_MINUTES * 60:
                agent.check_for_changes()
                last_monitor_check = now_greece

        time.sleep(20)  # Έλεγχος κάθε 20 δευτερόλεπτα

if __name__ == "__main__":
    main()
