import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import json
from datetime import datetime, timezone, timedelta
from functools import wraps

# ==========================================
# --- 1. CẤU HÌNH (GitHub Actions) ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'1h': '4h'}

SL_ATR_MULTIPLIER = 1.8
MIN_VOLATILITY = 0.003

MIN_SCORE = 4
RR_TIER_2 = 1.67
RR_TIER_3 = 2.4

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_IDS = [os.getenv('TELEGRAM_CHAT_ID')]

exchange = ccxt.mexc({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# ==========================================
# --- 2. STATE MANAGER (Gist) ---
# ==========================================
class GistStateManager:
    def __init__(self, filename='bot_state.json'):
        self.filename = filename
        self.github_token = os.getenv('GH_GIST_TOKEN')
        self.gist_id = os.getenv('GIST_ID')
        self.headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        } if self.github_token and self.gist_id else {}
        self.state = self.load()

    def load(self):
        if not self.headers: return {}
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code == 200:
                files = r.json().get('files', {})
                if self.filename in files:
                    return json.loads(files[self.filename]['content'])
        except: pass
        return {}

    def save(self):
        if not self.headers: return
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {"files": {self.filename: {"content": json.dumps(self.state, indent=2)}}}
            requests.patch(url, headers=self.headers, json=payload, timeout=10)
        except: pass

    def is_alerted(self, key, cooldown=3500):
        now = time.time()
        if key in self.state and (now - self.state[key]) < cooldown:
            return True
        self.state[key] = now
        self.save()
        return False

state_manager = GistStateManager()

# ==========================================
# --- 3. TIỆN ÍCH ---
# ==========================================
def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for _ in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    print(f"⚠️ API Error: {e}")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_api()
def fetch_ohlcv_safe(symbol, tf, limit=100):
    return exchange.fetch_ohlcv(symbol, tf, limit=limit)

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(window=length).mean()

def get_htf_trend(symbol, htf):
    bars = fetch_ohlcv_safe(symbol, htf, limit=300)  # FIX EMA 200
    if not bars: return "SIDEWAY"
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    ema_200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-2]
    atr = calculate_atr(df).iloc[-2]
    close_price = df['close'].iloc[-2]
    if abs(close_price - ema_200) < atr * 1.2: return "SIDEWAY"
    return "UP" if close_price > ema_200 else "DOWN"

# ==========================================
# --- 4. MODULE LOGIC ---
# ==========================================
def analyze_smc(closed_df, lookback=12):
    recent = closed_df.iloc[-lookback:]
    roll_high = recent['high'].rolling(3, center=True).max().dropna()
    roll_low  = recent['low'].rolling(3, center=True).min().dropna()
    swing_high = roll_high.iloc[-1] if not roll_high.empty else recent['high'].max()
    swing_low  = roll_low.iloc[-1]  if not roll_low.empty  else recent['low'].min()
    current = closed_df.iloc[-1]
    trend = "BULLISH" if current['close'] > swing_high else "BEARISH" if current['close'] < swing_low else "NEUTRAL"
    return {"trend": trend, "swing_high": swing_high, "swing_low": swing_low}

def analyze_ict(closed_df):
    now_utc = datetime.now(timezone.utc)
    est_hour = (now_utc - timedelta(hours=5)).hour
    is_killzone = (9 <= est_hour <= 11) or (2 <= est_hour <= 5)
    
    c1 = closed_df.iloc[-3]  # cũ
    c2 = closed_df.iloc[-2]  # giữa
    c3 = closed_df.iloc[-1]  # mới nhất
    is_bullish_fvg = c3['low'] > c1['high'] and c2['close'] > c2['open']
    is_bearish_fvg = c3['high'] < c1['low']  and c2['close'] < c2['open']
    fvg_status = "BULLISH" if is_bullish_fvg else "BEARISH" if is_bearish_fvg else "NONE"
    return {"is_killzone": is_killzone, "fvg": fvg_status}

def analyze_volume_pa(closed_df):
    current = closed_df.iloc[-1]
    prev = closed_df.iloc[-2]
    avg_vol = closed_df['vol'].iloc[-20:].mean()
    is_volume_spike = current['vol'] > avg_vol * 2
    is_bull_eng = (prev['close'] < prev['open'] and current['close'] > current['open'] and
                   current['close'] > prev['open'] and current['open'] < prev['close'])
    is_bear_eng = (prev['close'] > prev['open'] and current['close'] < current['open'] and
                   current['close'] < prev['open'] and current['open'] > prev['close'])
    pattern = "BULLISH_ENGULFING" if is_bull_eng else "BEARISH_ENGULFING" if is_bear_eng else "NONE"
    return {"is_volume_spike": is_volume_spike, "pattern": pattern}

# ==========================================
# --- 5. ENGINE ---
# ==========================================
def analyze_pair(symbol, tf):
    htf = MTF_MAPPING[tf]
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return False

    bars = fetch_ohlcv_safe(symbol, tf, limit=100)
    if not bars: return False

    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    df['atr'] = calculate_atr(df)

    closed_df = df.iloc[:-1]
    current_atr = closed_df['atr'].iloc[-1]
    entry = closed_df['close'].iloc[-1]

    if current_atr < entry * MIN_VOLATILITY: return False

    smc = analyze_smc(closed_df)
    ict = analyze_ict(closed_df)
    vol_pa = analyze_volume_pa(closed_df)

    signal_type = None
    if htf_trend == "UP" and smc['trend'] == "BULLISH": signal_type = "BUY"
    elif htf_trend == "DOWN" and smc['trend'] == "BEARISH": signal_type = "SELL"
    if not signal_type: return False

    score = 3
    factors = [f"HTF 4H: {htf_trend} ✅", "BOS H1 🏗️"]
    if ict['is_killzone']: score += 1; factors.append("Killzone ⏱️")
    if (signal_type == "BUY" and ict['fvg'] == "BULLISH") or (signal_type == "SELL" and ict['fvg'] == "BEARISH"):
        score += 1; factors.append("FVG ⚡")
    if vol_pa['is_volume_spike']: score += 1; factors.append("Volume Spike 🐳")
    if (signal_type == "BUY" and vol_pa['pattern'] == "BULLISH_ENGULFING") or \
       (signal_type == "SELL" and vol_pa['pattern'] == "BEARISH_ENGULFING"):
        score += 1; factors.append("Engulfing 🔫")

    if score < MIN_SCORE: return False

    if signal_type == "BUY":
        sl = entry - current_atr * SL_ATR_MULTIPLIER
    else:
        sl = entry + current_atr * SL_ATR_MULTIPLIER

    risk = abs(entry - sl)
    dyn_rr = RR_TIER_3 if score >= 6 else RR_TIER_2
    model_name = "🦄 UNICORN" if score >= 6 else "🔥 STRONG"
    tp = entry + risk * dyn_rr if signal_type == "BUY" else entry - risk * dyn_rr

    key = f"{symbol}_{tf}_{closed_df['ts'].iloc[-1]}"
    if state_manager.is_alerted(key): return False

    msg = (f"🚀 <b>SMC Action Bot v6.6 - {signal_type} {model_name}</b>\n"
           f"Symbol: <b>{symbol}</b> ({tf})\n"
           f"Score: <b>{score}/7</b>\n"
           f"Entry: <code>{entry:.4f}</code>\n"
           f"SL: <code>{sl:.4f}</code> <i>({SL_ATR_MULTIPLIER}ATR)</i>\n"
           f"TP ({dyn_rr}R): <code>{tp:.4f}</code>\n"
           f"────────────────\n"
           f"🔍 Confluences:\n + " + "\n + ".join(factors))
    send_telegram(msg)
    print(f">>> {symbol} {tf}: TÍN HIỆU {signal_type} (Score {score})")
    return True

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    for cid in CHAT_IDS:
        if not cid: continue
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            print(f"Lỗi Telegram: {e}")

# ==========================================
# --- 6. MAIN ---
# ==========================================
if __name__ == "__main__":
    start_time = datetime.now().strftime('%H:%M:%S')
    print(f"🚀 SMC Action Bot v6.6 started at {start_time}")

    signals_found = 0
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            if analyze_pair(symbol, tf):
                signals_found += 1
            time.sleep(0.8)

    # Heartbeat luôn gửi
    heartbeat = (f"🤖 <b>SMC Action Bot v6.6 - ALIVE 🟢</b>\n"
                 f"Time: <code>{start_time}</code>\n"
                 f"Quét {len(PAIRS)} pair • Tìm thấy <b>{signals_found}</b> signal")
    send_telegram(heartbeat)

    print(f"✅ Finished at {datetime.now().strftime('%H:%M:%S')} - {signals_found} signal(s)")
