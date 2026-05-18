#!/usr/bin/env python3
"""
AI Trading Agent για uFunded Platform - v3.0
- Όλες οι μετοχές S&P 500 + υποψήφιες
- Αυτόματη ενημέρωση λίστας
- Monitoring 16:00-17:00
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

TELEGRAM_BOT_TOKEN = "8713919672:AAEtVBMT9NsSfvXdHlVygrr7XanJU8GilG4"
TELEGRAM_CHAT_ID = "7235378762"
ANTHROPIC_API_KEY = "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"

LEVERAGE_CAPITAL = 90_000
MONITOR_INTERVAL_MINUTES = 5
MONITOR_END_HOUR = 17
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
    def fetch_news(self, symbols_top10):
        """Παίρνει τελευταία νέα για τις top μετοχές μέσω RSS feeds"""
        news_data = {}
        headers = {'User-Agent': 'Mozilla/5.0'}

        for sym in symbols_top10:
            articles = []
            try:
                # Yahoo Finance RSS για κάθε μετοχή
                url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
                resp = requests.get(url, headers=headers, timeout=8)
                if resp.status_code == 200:
                    import re
                    titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', resp.text)
                    articles = titles[1:4]  # Πρώτα 3 άρθρα (παραλείπουμε τον τίτλο feed)
            except:
                pass

            if not articles:
                try:
                    # Fallback: Finviz news
                    url2 = f"https://finviz.com/quote.ashx?t={sym}"
                    resp2 = requests.get(url2, headers=headers, timeout=8)
                    if resp2.status_code == 200:
                        import re
                        titles = re.findall(r'class="news-link-right"[^>]*>(.*?)</a>', resp2.text)
                        articles = titles[:3]
                except:
                    pass

            news_data[sym] = articles if articles else ["Δεν βρέθηκαν νέα"]

        return news_data

    def fetch_market_news(self):
        """Παίρνει γενικά οικονομικά νέα"""
        headers = {'User-Agent': 'Mozilla/5.0'}
        general_news = []
        try:
            # Reuters Business RSS
            url = "https://feeds.reuters.com/reuters/businessNews"
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                import re
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', resp.text)
                general_news = titles[1:6]
        except:
            pass

        if not general_news:
            try:
                # CNBC RSS
                url2 = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
                resp2 = requests.get(url2, headers=headers, timeout=8)
                if resp2.status_code == 200:
                    import re
                    titles = re.findall(r'<title>(.*?)</title>', resp2.text)
                    general_news = [t for t in titles[1:6] if len(t) > 20]
            except:
                pass

        return general_news if general_news else ["Δεν βρέθηκαν γενικά νέα"]

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

        # Νέα για top 10 μετοχές
        top10_symbols = [d['symbol'] for d in market_data[:10]]
        logger.info("📰 Συλλογή νέων...")
        news_data = self.fetch_news(top10_symbols)
        general_news = self.fetch_market_news()

        # Φτιάξε news summary
        news_summary = "=== ΤΕΛΕΥΤΑΙΑ ΟΙΚΟΝΟΜΙΚΑ ΝΕΑ ===\n"
        news_summary += "📰 Γενικά νέα αγοράς:\n"
        for n in general_news:
            news_summary += f"  • {n}\n"
        news_summary += "\n📊 Νέα ανά μετοχή:\n"
        for sym, articles in news_data.items():
            name = next((d['name'] for d in market_data if d['symbol'] == sym), sym)
            news_summary += f"  {sym} ({name}):\n"
            for a in articles:
                news_summary += f"    - {a}\n"

        today_date = datetime.now().strftime("%d/%m/%Y")
        prompt = f"""Είσαι expert trading analyst για uFunded με μόχλευση $90,000 USD.
ΗΜΕΡΟΜΗΝΙΑ: {today_date} | ΣΤΟΧΟΣ: €250 ημερήσιο κέρδος | ΙΣΟΤΙΜΙΑ: 1 USD = 0.92 EUR

=== ΚΑΝΟΝΕΣ ΔΙΑΧΕΙΡΙΣΗΣ ΚΕΦΑΛΑΙΟΥ ===
ΣΥΝΟΛΙΚΟ ΚΕΦΑΛΑΙΟ: $90,000 (με μόχλευση uFunded)
ΜΕΓΙΣΤΟ ανά θέση: $20,000 (22% του χαρτοφυλακίου)
ΕΛΑΧΙΣΤΟ ανά θέση: $5,000 (5% του χαρτοφυλακίου)
ΜΕΓΙΣΤΟ ΡΙΣΚΟ ανά trade: 1.5% του κεφαλαίου = $1,350
Αριθμός μετοχών που αγοράζω = ποσό επένδυσης / τιμή entry

{news_summary}

=== ΤΕΧΝΙΚΗ ΑΝΑΛΥΣΗ - TOP MOVERS ({len(market_data)} μετοχές) ===
{chr(10).join(data_summary)}

=== ΕΝΤΟΛΗ ===
Επέλεξε τις ΚΑΛΥΤΕΡΕΣ 5 ευκαιρίες συνδυάζοντας:
1. ΤΕΧΝΙΚΗ ΑΝΑΛΥΣΗ (RSI, MACD, ATR, όγκος)
2. ΘΕΜΕΛΙΩΔΗ ΑΝΑΛΥΣΗ (τι λένε τα νέα για κάθε μετοχή)
3. SENTIMENT (θετικά/αρνητικά νέα = BUY/SELL ευκαιρία)

Αν τα νέα είναι ΑΡΝΗΤΙΚΑ για μια μετοχή → πρότεινε SELL
Αν τα νέα είναι ΘΕΤΙΚΑ → πρότεινε BUY
Αν υπάρχει υποψήφια S&P 500 με δυνατά σήματα, συμπερίλαβέ την!

ΥΠΟΛΟΓΙΣΕ για κάθε trade:
- Ποσό επένδυσης βάσει εμπιστοσύνης: Υψηλό=$18,000-20,000 / Μέτριο=$10,000-15,000 / Χαμηλό=$5,000-8,000
- Αριθμό μετοχών/units = ποσό / τιμή entry (στρογγυλοποίησε)
- Stop Loss βάσει ATR x 1.5
- Take Profit βάσει R:R = 2:1 minimum
- Αναμενόμενο κέρδος = (Take Profit - Entry) x αριθμός μετοχών

Format:
🎯 TOP 5 TRADING SIGNALS - {today_date}
📊 Ανάλυση: {len(market_data)} μετοχές + νέα αγοράς

═══════════════════════════
📌 1. [ΣΥΜΒΟΛΟ] - [ΟΝΟΜΑ] [[ΚΑΤΗΓΟΡΙΑ]]
[BUY 🟢 ή SELL 🔴]
📰 Νέα: [σχετικό νέο που επηρεάζει την απόφαση]
💰 Entry: $[τιμή]
🛑 Stop Loss: $[τιμή] (-[%]% | -$[ζημία] αν χτυπήσει)
🎯 Take Profit: $[τιμή] (+[%]% | +$[κέρδος] αν χτυπήσει)
💼 Επένδυση: $[ποσό] ([%]% του χαρτοφυλακίου)
📦 Αριθμός: [X] μετοχές/units
📈 Αναμ. Κέρδος: $[USD] ≈ €[EUR]
⚠️ Μέγιστη Ζημία: $[USD] ≈ €[EUR]
📊 Τεχνικά: [ανάλυση RSI/MACD/MA]
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
    # ΑΡΧΙΚΗ ΑΝΑΛΥΣΗ 16:00
    # ─────────────────────────────────────────────
    def run_daily_analysis(self):
        logger.info("🚀 ΑΝΑΛΥΣΗ 16:00")
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
                f"🔍 Monitoring κάθε 5λεπτα έως 17:00\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            self.send_telegram(header + analysis)
            logger.info("✅ Ανάλυση εστάλη!")

        except Exception as e:
            logger.error(f"❌ {e}")
            self.send_telegram(f"❌ Σφάλμα: {e}")

    # ─────────────────────────────────────────────
    # MONITORING 16:00-17:00
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
                    rsi = snap.get('rsi', 50)
                    atr = snap.get('atr', current_price * 0.02)

                    # Νέα για τη μετοχή
                    news_list = self.fetch_news([symbol])
                    news_text = news_list.get(symbol, ["Δεν βρέθηκαν νέα"])[0]

                    # Υπολογισμός SL/TP/Position Size
                    sl_pct = atr * 1.5 / current_price * 100
                    tp_pct = sl_pct * 2

                    if price_change > 0:
                        # BUY signal
                        action = "BUY 🟢"
                        sl_price = round(current_price * (1 - sl_pct/100), 4)
                        tp_price = round(current_price * (1 + tp_pct/100), 4)
                    else:
                        # SELL signal
                        action = "SELL 🔴"
                        sl_price = round(current_price * (1 + sl_pct/100), 4)
                        tp_price = round(current_price * (1 - tp_pct/100), 4)

                    # Position sizing
                    confidence = "Υψηλό" if abs(price_change) > 2 else "Μέτριο" if abs(price_change) > 1 else "Χαμηλό"
                    invest_amount = 18000 if confidence == "Υψηλό" else 12000 if confidence == "Μέτριο" else 7000
                    num_shares = round(invest_amount / current_price)
                    expected_profit = round(abs(tp_price - current_price) * num_shares, 2)
                    max_loss = round(abs(sl_price - current_price) * num_shares, 2)
                    expected_profit_eur = round(expected_profit * 0.92, 2)
                    max_loss_eur = round(max_loss * 0.92, 2)
                    invest_pct = round(invest_amount / 90000 * 100)

                    # AI ανάλυση
                    try:
                        advice_resp = self.client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=200,
                            messages=[{"role": "user", "content":
                                f"""Μετοχή: {symbol} ({name})
Αλλαγή: {price_change:+.2f}% | Τιμή: ${old_price} → ${round(current_price,4)}
RSI: {rsi} | ATR: {atr}
Νέο: {news_text}

Γράψε 1 πρόταση τεχνική ανάλυση και 1 πρόταση γιατί να αγοράσω/πουλήσω."""}]
                        )
                        technical_analysis = advice_resp.content[0].text
                    except:
                        rsi_text = "υπερπουλημένη - ευκαιρία BUY" if rsi < 30 else "υπεραγορασμένη - ευκαιρία SELL" if rsi > 70 else "ουδέτερο RSI"
                        technical_analysis = f"RSI {rsi} ({rsi_text}). Παρακολούθησε προσεκτικά."

                    alerts.append(
                        f"🚨 ALERT {now.strftime('%H:%M')} - {symbol}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 {symbol} - {name}\n"
                        f"{action}\n"
                        f"{direction} {price_change:+.2f}%\n"
                        f"📰 Νέα: {news_text}\n"
                        f"💰 Entry: ${round(current_price, 4)}\n"
                        f"🛑 Stop Loss: ${sl_price} (-{round(sl_pct,1)}% | -${max_loss} αν χτυπήσει)\n"
                        f"🎯 Take Profit: ${tp_price} (+{round(tp_pct,1)}% | +${expected_profit} αν χτυπήσει)\n"
                        f"💼 Επένδυση: ${invest_amount:,} ({invest_pct}% του χαρτοφυλακίου)\n"
                        f"📦 Αριθμός: {num_shares} μετοχές\n"
                        f"📈 Αναμ. Κέρδος: ${expected_profit} ≈ €{expected_profit_eur}\n"
                        f"⚠️ Μέγιστη Ζημία: ${max_loss} ≈ €{max_loss_eur}\n"
                        f"📊 Τεχνικά: {technical_analysis}\n"
                        f"⚡ Εμπιστοσύνη: {confidence}"
                    )
                    self.alerts_sent.add(alert_key)

            except Exception as e:
                logger.error(f"Monitor {symbol}: {e}")

        if alerts:
            for alert in alerts:
                self.send_telegram(alert)
                time.sleep(1)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def is_nyse_trading_day(date):
    """Ελέγχει αν είναι εργάσιμη μέρα του NYSE"""
    # Σαββατοκύριακο
    if date.weekday() >= 5:
        return False

    year = date.year
    month = date.month
    day = date.day

    # Αμερικανικές αργίες NYSE
    holidays = [
        # New Year's Day
        (1, 1),
        # MLK Day (3η Δευτέρα Ιανουαρίου)
        # Presidents Day (3η Δευτέρα Φεβρουαρίου)
        # Memorial Day (τελευταία Δευτέρα Μαΐου)
        # Juneteenth
        (6, 19),
        # Independence Day
        (7, 4),
        # Labor Day (1η Δευτέρα Σεπτεμβρίου)
        # Thanksgiving (4η Πέμπτη Νοεμβρίου)
        # Christmas
        (12, 25),
    ]

    # Σταθερές αργίες
    if (month, day) in holidays:
        return False

    # MLK Day - 3η Δευτέρα Ιανουαρίου
    if month == 1 and date.weekday() == 0:
        mondays = [d for d in range(1, 32) if
                   date.replace(day=d).weekday() == 0]
        if len(mondays) >= 3 and day == mondays[2]:
            return False

    # Presidents Day - 3η Δευτέρα Φεβρουαρίου
    if month == 2 and date.weekday() == 0:
        mondays = [d for d in range(1, 29) if
                   date.replace(day=d).weekday() == 0]
        if len(mondays) >= 3 and day == mondays[2]:
            return False

    # Memorial Day - τελευταία Δευτέρα Μαΐου
    if month == 5 and date.weekday() == 0:
        mondays = [d for d in range(1, 32) if
                   date.replace(day=d).weekday() == 0]
        if day == mondays[-1]:
            return False

    # Labor Day - 1η Δευτέρα Σεπτεμβρίου
    if month == 9 and date.weekday() == 0:
        mondays = [d for d in range(1, 31) if
                   date.replace(day=d).weekday() == 0]
        if day == mondays[0]:
            return False

    # Thanksgiving - 4η Πέμπτη Νοεμβρίου
    if month == 11 and date.weekday() == 3:
        thursdays = [d for d in range(1, 31) if
                     date.replace(day=d).weekday() == 3]
        if len(thursdays) >= 4 and day == thursdays[3]:
            return False

    # Αν αργία πέφτει Σάββατο → κλειστό Παρασκευή
    # Αν αργία πέφτει Κυριακή → κλειστό Δευτέρα
    for (m, d) in [(1,1), (6,19), (7,4), (12,25)]:
        try:
            holiday = date.replace(month=m, day=d)
            if holiday.weekday() == 5 and date == holiday - __import__('datetime').timedelta(days=1):
                return False
            if holiday.weekday() == 6 and date == holiday + __import__('datetime').timedelta(days=1):
                return False
        except:
            pass

    return True


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
        f"⏰ Ανάλυση Δευτ-Παρ στις 16:20\n"
        f"🔍 Monitoring 16:20-18:00\n"
        f"📅 Παρακάμπτει αργίες NYSE\n"
        f"🎯 Στόχος: €250/ημέρα"
    )

    logger.info("⏳ Agent έτοιμος!")

    analysis_done_today = False
    last_monitor_check = None

    while True:
        now_greece = datetime.now(greece_tz)
        current_hour = now_greece.hour
        current_minute = now_greece.minute
        today = now_greece.date()

        # Έλεγχος αν είναι εργάσιμη μέρα NYSE
        trading_day = is_nyse_trading_day(today)

        if not trading_day and current_hour == 9 and current_minute == 0:
            day_name = ["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή","Σάββατο","Κυριακή"][today.weekday()]
            logger.info(f"📅 {day_name} - Κλειστό NYSE, παραλείπεται")

        # Αρχική ανάλυση 16:20 — ΜΟΝΟ εργάσιμες
        if current_hour == 16 and current_minute == 20 and not analysis_done_today and trading_day:
            agent.run_daily_analysis()
            analysis_done_today = True

        # Reset για επόμενη μέρα
        if current_hour == 0 and current_minute == 0:
            analysis_done_today = False
            agent.load_all_symbols()  # Ενημέρωση λίστας κάθε μέρα

        # Monitoring κάθε 5 λεπτά 16:20-18:00 — ΜΟΝΟ εργάσιμες
        if current_hour == 16 and trading_day:
            if (last_monitor_check is None or
                    (now_greece - last_monitor_check).seconds >= MONITOR_INTERVAL_MINUTES * 60):
                agent.check_for_changes()
                last_monitor_check = now_greece

        time.sleep(20)


if __name__ == "__main__":
    main()
