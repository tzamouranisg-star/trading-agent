#!/usr/bin/env python3
"""
AI Trading Agent για uFunded Platform - v3.0
- Όλες οι μετοχές S&P 500 + υποψήφιες
- Αυτόματη ενημέρωση λίστας
- Monitoring 16:20-18:00
- Alerts για σημαντικές αλλαγές
"""

import yfinance as yf
import anthropic
import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import logging
import io
import os

TELEGRAM_BOT_TOKEN = "8713919672:AAEtVBMT9NsSfvXdHlVygrr7XanJU8GilG4"
TELEGRAM_CHAT_ID = "7235378762"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LEVERAGE_CAPITAL = 90_000
MONITOR_INTERVAL_MINUTES = 5
MONITOR_END_HOUR = 18
PRICE_CHANGE_ALERT = 0.8
VOLUME_SPIKE_ALERT = 2.5

# Commodities & Indices (σταθερά)
EXTRA_SYMBOLS = {
    "GC=F":"Χρυσός","SI=F":"Ασήμι","CL=F":"Αργό Πετρέλαιο",
    "NG=F":"Φυσικό Αέριο","HG=F":"Χαλκός","PL=F":"Πλατίνα",
    "ZW=F":"Σιτάρι","ZC=F":"Καλαμπόκι","ZS=F":"Σόγια","KC=F":"Καφές",
    "SPY":"S&P 500 ETF","QQQ":"NASDAQ 100 ETF","DIA":"Dow Jones ETF","IWM":"Russell 2000 ETF",
}

# Υποψήφιες για S&P 500 (high-cap που δεν είναι ακόμα μέσα)
SP500_CANDIDATES = [
    "UBER","ABNB","SNOW","PLTR","RIVN","LCID","RBLX","HOOD",
    "COIN","DKNG","PENN","SOFI","AFRM","UPST","OPEN","WISH",
    "IONQ","JOBY","ARCHER","LILM","ACHR","EVTL","SPCE","ASTS",
    "LUNR","RDW","MNTS","BKSY","SATL","GNPK"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TradingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.all_symbols = {}
        self.active_signals = []
        self.snapshot_at_16 = {}
        self.monitoring_active = False
        self.alerts_sent = set()

    # ─────────────────────────────────────────────
    # ΦΟΡΤΩΣΗ S&P 500 + ΥΠΟΨΗΦΙΩΝ
    # ─────────────────────────────────────────────
    def load_sp500_symbols(self):
        """Κατεβάζει αυτόματα όλες τις μετοχές S&P 500 από Wikipedia"""
        logger.info("📋 Φόρτωση S&P 500 από Wikipedia...")
        try:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            resp = requests.get(url, timeout=15)
            tables = pd.read_html(io.StringIO(resp.text))
            df = tables[0]
            symbols = {}
            for _, row in df.iterrows():
                sym = str(row['Symbol']).replace('.', '-')
                name = str(row['Security'])
                symbols[sym] = {"name": name, "category": "S&P 500"}
            logger.info(f"✅ Φορτώθηκαν {len(symbols)} μετοχές S&P 500")
            return symbols
        except Exception as e:
            logger.error(f"❌ Σφάλμα φόρτωσης S&P 500: {e}")
            # Fallback με βασικές μετοχές
            return {
                "AAPL":{"name":"Apple","category":"S&P 500"},
                "MSFT":{"name":"Microsoft","category":"S&P 500"},
                "NVDA":{"name":"NVIDIA","category":"S&P 500"},
                "GOOGL":{"name":"Alphabet","category":"S&P 500"},
                "AMZN":{"name":"Amazon","category":"S&P 500"},
                "META":{"name":"Meta","category":"S&P 500"},
                "TSLA":{"name":"Tesla","category":"S&P 500"},
                "JPM":{"name":"JPMorgan","category":"S&P 500"},
                "V":{"name":"Visa","category":"S&P 500"},
                "UNH":{"name":"UnitedHealth","category":"S&P 500"},
            }

    def load_all_symbols(self):
        """Φορτώνει S&P500 + υποψήφιες + commodities"""
        # S&P 500
        self.all_symbols = self.load_sp500_symbols()

        # Υποψήφιες για S&P 500
        for sym in SP500_CANDIDATES:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                name = info.get('longName', sym)
                self.all_symbols[sym] = {"name": name, "category": "Υποψήφια S&P 500"}
            except:
                self.all_symbols[sym] = {"name": sym, "category": "Υποψήφια S&P 500"}

        # Commodities & Indices
        for sym, name in EXTRA_SYMBOLS.items():
            cat = "Commodity" if "=F" in sym else "Index ETF"
            self.all_symbols[sym] = {"name": name, "category": cat}

        logger.info(f"✅ Σύνολο συμβόλων: {len(self.all_symbols)}")

    # ─────────────────────────────────────────────
    # ΦΙΛΤΡΑΡΙΣΜΑ - Τα πιο ενδιαφέροντα σήμερα
    # ─────────────────────────────────────────────
    def get_top_movers(self, limit=80):
        """Βρίσκει τις μετοχές με τη μεγαλύτερη κίνηση σήμερα"""
        logger.info(f"🔍 Σάρωση {len(self.all_symbols)} συμβόλων για top movers...")
        results = []
        count = 0

        for symbol, info in self.all_symbols.items():
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="2d", interval="1d")
                if len(hist) < 2:
                    continue

                current = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                change_pct = ((current - prev) / prev) * 100
                volume = hist['Volume'].iloc[-1]

                results.append({
                    "symbol": symbol,
                    "name": info["name"],
                    "category": info["category"],
                    "current_price": round(current, 4),
                    "change_pct": round(change_pct, 2),
                    "volume": volume,
                    "abs_change": abs(change_pct)
                })
                count += 1
                if count % 50 == 0:
                    logger.info(f"  ... σκανάρισα {count} μετοχές")

            except Exception as e:
                continue

        # Ταξινόμηση: μεγαλύτερη κίνηση πρώτα
        results.sort(key=lambda x: x['abs_change'], reverse=True)
        top = results[:limit]
        logger.info(f"✅ Top {len(top)} movers επιλέχθηκαν για ανάλυση")
        return top

    # ─────────────────────────────────────────────
    # ΤΕΧΝΙΚΗ ΑΝΑΛΥΣΗ
    # ─────────────────────────────────────────────
    def fetch_technical_data(self, symbol, basic_data):
        """Προσθέτει τεχνικούς δείκτες στα βασικά δεδομένα"""
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="60d", interval="1d")
            if len(hist) < 20:
                return basic_data

            closes = hist['Close']
            volumes = hist['Volume']

            ma20 = closes.rolling(20).mean().iloc[-1]
            ma50 = closes.rolling(min(50, len(closes))).mean().iloc[-1]

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

            basic_data.update({
                "ma20": round(ma20, 4),
                "ma50": round(ma50, 4),
                "rsi": round(rsi, 2),
                "macd_hist": round(macd_hist, 6),
                "atr": round(atr, 4),
                "vol_ratio": round(vol_ratio, 2),
            })
            return basic_data
        except:
            return basic_data

    # ─────────────────────────────────────────────
    # TELEGRAM
    # ─────────────────────────────────────────────
    def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            try:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Telegram error: {e}")

    # ─────────────────────────────────────────────
    # AI ΑΝΑΛΥΣΗ
    # ─────────────────────────────────────────────
    def analyze_with_claude(self, market_data):
        data_summary = []
        for d in market_data[:60]:  # Max 60 για να χωράει στο context
            rsi = d.get('rsi', 50)
            rsi_signal = "ΥΠΕΡΑΓΟΡΑ" if rsi > 70 else ("ΥΠΕΡΠΩΛΗΣΗ" if rsi < 30 else "ΟΥΔΕΤΕΡΟ")
            candidate_flag = " ⭐ΥΠΟΨΗΦΙΑ S&P500" if d['category'] == "Υποψήφια S&P 500" else ""
            data_summary.append(
                f"• {d['symbol']} ({d['name']}) [{d['category']}]{candidate_flag}\n"
                f"  Τιμή: ${d['current_price']} | Σήμερα: {d['change_pct']:+.2f}%\n"
                f"  RSI: {rsi} ({rsi_signal}) | MACD: {d.get('macd_hist','N/A')} | ATR: {d.get('atr','N/A')}\n"
                f"  Όγκος: {d.get('vol_ratio','N/A')}x μέσο"
            )

        today_date = datetime.now().strftime("%d/%m/%Y")
        prompt = f"""Είσαι expert trading analyst για uFunded με μόχλευση $90,000 USD.
ΗΜΕΡΟΜΗΝΙΑ: {today_date} | ΣΤΟΧΟΣ: €250 ημερήσιο κέρδος | ΙΣΟΤΙΜΙΑ: 1 USD = 0.85 EUR

=== ΚΑΝΟΝΕΣ ΔΙΑΧΕΙΡΙΣΗΣ ΚΕΦΑΛΑΙΟΥ ===
ΣΥΝΟΛΙΚΟ ΚΕΦΑΛΑΙΟ: $90,000 (με μόχλευση uFunded)
ΜΕΓΙΣΤΟ ανά θέση: $20,000 (22% του χαρτοφυλακίου)
ΕΛΑΧΙΣΤΟ ανά θέση: $5,000 (5% του χαρτοφυλακίου)
ΜΕΓΙΣΤΟ ΡΙΣΚΟ ανά trade: 1.5% του κεφαλαίου = $1,350
Αριθμός μετοχών που αγοράζω = ποσό επένδυσης / τιμή entry

Ανέλυσα {len(market_data)} μετοχές S&P 500 + υποψήφιες + commodities.
Τα παρακάτω είναι τα TOP movers σήμερα:

{chr(10).join(data_summary)}

Επέλεξε τις ΚΑΛΥΤΕΡΕΣ 5 ευκαιρίες για trading σήμερα.
Προτίμησε μετοχές με: ισχυρά τεχνικά σήματα, υψηλό όγκο, σαφή τάση.
Αν υπάρχει υποψήφια S&P 500 με δυνατά σήματα, συμπερίλαβέ την!

ΥΠΟΛΟΓΙΣΕ για κάθε trade:
- Ποσό επένδυσης βάσει εμπιστοσύνης: Υψηλό=$18,000-20,000 / Μέτριο=$10,000-15,000 / Χαμηλό=$5,000-8,000
- Αριθμό μετοχών/units = ποσό / τιμή entry (στρογγυλοποίησε)
- Stop Loss βάσει ATR x 1.5
- Take Profit βάσει R:R = 2:1 minimum
- Αναμενόμενο κέρδος = (Take Profit - Entry) x αριθμός μετοχών

Format:
🎯 TOP 5 TRADING SIGNALS - {today_date}
📊 Ανάλυση: {len(market_data)} μετοχές S&P500 + υποψήφιες + commodities

═══════════════════════════
📌 1. [ΣΥΜΒΟΛΟ] - [ΟΝΟΜΑ] [[ΚΑΤΗΓΟΡΙΑ]]
[BUY 🟢 ή SELL 🔴]
💰 Entry: $[τιμή]
🛑 Stop Loss: $[τιμή] (-[%]% | -$[ζημία] αν χτυπήσει)
🎯 Take Profit: $[τιμή] (+[%]% | +$[κέρδος] αν χτυπήσει)
💼 Επένδυση: $[ποσό] ([%]% του χαρτοφυλακίου)
📦 Αριθμός: [X] μετοχές/units
📈 Αναμ. Κέρδος: $[USD] ≈ €[EUR]
⚠️ Μέγιστη Ζημία: $[USD] ≈ €[EUR]
📊 Σήματα: [ανάλυση RSI/MACD/MA]
⚡ Εμπιστοσύνη: [Χαμηλό/Μέτριο/Υψηλό]

[επανέλαβε για 2,3,4,5]

━━━━━━━━━━━━━━━━━━━━━━━━━━
💹 ΣΥΝΟΛΙΚΟ ΑΝΑΜ. ΚΕΡΔΟΣ: $[USD] ≈ €[EUR]
💼 ΣΥΝΟΛΙΚΗ ΕΠΕΝΔΥΣΗ: $[USD] ([%]% του χαρτοφυλακίου)
🛡️ ΣΥΝΟΛΙΚΟ ΜΕΓΙΣΤΟ ΡΙΣΚΟ: $[USD] ≈ €[EUR]
⚠️ Monitoring κάθε 5λεπτα έως 18:00"""

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    # ─────────────────────────────────────────────
    # ΑΡΧΙΚΗ ΑΝΑΛΥΣΗ 16:20
    # ─────────────────────────────────────────────
    def run_daily_analysis(self):
        logger.info("🚀 ΑΝΑΛΥΣΗ 16:20")
        self.send_telegram(
            f"🔍 Ξεκινά ανάλυση {len(self.all_symbols)} μετοχών...\n"
            f"📊 S&P 500 + Υποψήφιες + Commodities\n"
            f"⏳ Περίμενε 3-5 λεπτά..."
        )

        try:
            # Βήμα 1: Βρες top movers
            top_movers = self.get_top_movers(limit=80)

            # Βήμα 2: Τεχνική ανάλυση στα top movers
            logger.info("📈 Τεχνική ανάλυση top movers...")
            enriched = []
            for d in top_movers:
                enriched_d = self.fetch_technical_data(d['symbol'], d)
                enriched.append(enriched_d)

            # Αποθήκευση snapshot
            self.snapshot_at_16 = {d['symbol']: d for d in enriched}
            self.active_signals = [d['symbol'] for d in enriched[:5]]
            self.alerts_sent = set()
            self.monitoring_active = True

            # Βήμα 3: AI ανάλυση
            analysis = self.analyze_with_claude(enriched)

            header = (
                f"🤖 AI Trading Agent v3.0 - uFunded\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')} (Ώρα Ελλάδας)\n"
                f"💼 Κεφάλαιο: $90,000 | 🎯 Στόχος: €250\n"
                f"📊 Σκανάρισα: {len(self.all_symbols)} μετοχές\n"
                f"🔍 Monitoring κάθε 5λεπτα έως 18:00\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            self.send_telegram(header + analysis)
            logger.info("✅ Ανάλυση εστάλη!")

        except Exception as e:
            logger.error(f"❌ {e}")
            self.send_telegram(f"❌ Σφάλμα: {e}")

    # ─────────────────────────────────────────────
    # MONITORING 16:20-18:00
    # ─────────────────────────────────────────────
    def check_for_changes(self):
        greece_tz = pytz.timezone("Europe/Athens")
        now = datetime.now(greece_tz)

        if now.hour >= MONITOR_END_HOUR:
            if self.monitoring_active:
                self.monitoring_active = False
                self.send_telegram("🔴 Monitoring ολοκληρώθηκε.\n📊 Καλή επιτυχία με τις επενδύσεις σου!")
            return

        if not self.monitoring_active or not self.snapshot_at_16:
            return

        logger.info(f"🔍 Monitor check {now.strftime('%H:%M')}")
        alerts = []

        for symbol in list(self.snapshot_at_16.keys())[:20]:
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1d", interval="5m")
                if hist.empty:
                    continue

                current_price = hist['Close'].iloc[-1]
                snap = self.snapshot_at_16[symbol]
                old_price = snap['current_price']
                name = snap['name']
                price_change = ((current_price - old_price) / old_price) * 100
                alert_key = f"{symbol}_{round(price_change, 1)}"

                if abs(price_change) >= PRICE_CHANGE_ALERT and alert_key not in self.alerts_sent:
                    direction = "📈 ΑΝΕΒΗΚΕ" if price_change > 0 else "📉 ΕΠΕΣΕ"
                    emoji = "🟢" if price_change > 0 else "🔴"

                    try:
                        advice_resp = self.client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=150,
                            messages=[{"role": "user", "content":
                                f"Trading alert: {symbol} άλλαξε {price_change:+.2f}% από ${old_price} → ${round(current_price,4)}. RSI: {snap.get('rsi','N/A')}. Σύντομη σύσταση σε 2 γραμμές: κράτα/κλείσε θέση και γιατί."}]
                        )
                        advice = advice_resp.content[0].text
                    except:
                        advice = "💡 Έλεγξε χειροκίνητα τη θέση σου."

                    alerts.append(
                        f"⚡ ΑΛΛΑΓΗ: {symbol} ({name})\n"
                        f"{direction} {emoji} {price_change:+.2f}%\n"
                        f"💰 ${old_price} → ${round(current_price,4)}\n"
                        f"━━━━━━━\n{advice}"
                    )
                    self.alerts_sent.add(alert_key)

            except Exception as e:
                logger.error(f"Monitor {symbol}: {e}")

        if alerts:
            msg = f"🚨 ALERT {now.strftime('%H:%M')}\n\n" + "\n\n─────\n\n".join(alerts)
            self.send_telegram(msg)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    agent = TradingAgent()
    greece_tz = pytz.timezone("Europe/Athens")

    # Φόρτωση συμβόλων
    agent.load_all_symbols()

    agent.send_telegram(
        f"🟢 AI Trading Agent v3.0 ξεκίνησε!\n"
        f"📊 Κοιτάει {len(agent.all_symbols)} σύμβολα:\n"
        f"• Όλες οι μετοχές S&P 500\n"
        f"• Υποψήφιες για S&P 500\n"
        f"• Commodities & Indices\n"
        f"⏰ Ανάλυση κάθε μέρα 16:00\n"
        f"🔍 Monitoring 16:00-17:00\n"
        f"🎯 Στόχος: €250/ημέρα"
    )

    logger.info("⏳ Agent έτοιμος!")

    analysis_done_today = False
    last_monitor_check = None

    while True:
        now_greece = datetime.now(greece_tz)
        current_hour = now_greece.hour
        current_minute = now_greece.minute

        # Αρχική ανάλυση 16:20
        if current_hour == 16 and current_minute == 20  and not analysis_done_today:
            agent.run_daily_analysis()
            analysis_done_today = True

        # Reset για επόμενη μέρα
        if current_hour == 0 and current_minute == 0:
            analysis_done_today = False
            agent.load_all_symbols()  # Ενημέρωση λίστας κάθε μέρα

        # Monitoring κάθε 5 λεπτά 16:20-18:00
        if current_hour == 16:
            if (last_monitor_check is None or
                    (now_greece - last_monitor_check).seconds >= MONITOR_INTERVAL_MINUTES * 60):
                agent.check_for_changes()
                last_monitor_check = now_greece

        time.sleep(20)


if __name__ == "__main__":
    main()
