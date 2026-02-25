import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import json
from datetime import datetime
from functools import wraps

# ==========================================
# --- 1. CẤU HÌNH ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.8
MIN_SCORE = 5

MAX_BARS_LIMITS = {'15m': 35, '1h': 80, '4h': 55}

ENABLE_ORDER_ANTISPAM = True
ENABLE_HEARTBEAT = True
ENABLE_KILLZONES = False

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ==========================================
# --- 2. STATE MANAGER (giữ nguyên) ---
# ==========================================
class GistStateManager:
    def __init__(self, filename='bot_state.json'):
        self.filename = filename
        self.github_token = os.getenv('GH_GIST_TOKEN')
        self.gist_id = os.getenv('GIST_ID')
        self.headers = {"Authorization": f"token {self.github_token}", "Accept": "application/vnd.github.v3+json"} if self.github_token else {}
        self.state = self.load()

    def load(self):
        if not self.github_token or not self.gist_id: return {}
        try:
            r = requests.get(f"https://api.github.com/gists/{self.gist_id}", headers=self.headers, timeout=10)
            if r.status_code == 200:
                files = r.json().get('files', {})
                if self.filename in files:
                    return json.loads(files[self.filename]['content'])
        except: pass
        return {}

    def save(self):
        if not self.github_token or not self.gist_id: return
        try:
            payload = {"files": {self.filename: {"content": json.dumps(self.state, indent=2)}}}
            requests.patch(f"https://api.github.com/gists/{self.gist_id}", headers=self.headers, json=payload, timeout=10)
        except: pass

    def is_alerted(self, key, cooldown=4000):
        now = time.time()
        if key in self.state and (now - self.state[key]) < cooldown:
            return True
        self.state[key] = now
        self.save()
        return False

state_manager = GistStateManager()

# ==========================================
# --- 3. UTILS ---
# ==========================================
def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for _ in range(retries):
                try: return func(*args, **kwargs)
                except: time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_api()
def fetch_ohlcv_safe(symbol, tf, limit=500):
    return exchange.fetch_ohlcv(symbol, tf, limit=limit)

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(length).mean()
    loss = -delta.where(delta < 0, 0).rolling(length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))

def identify_fractals(df):
    df = df.copy()
    df['is_fractal_high'] = (df['high'].shift(2) > df['high'].shift(4)) & (df['high'].shift(2) > df['high'].shift(3)) & \
                            (df['high'].shift(2) > df['high'].shift(1)) & (df['high'].shift(2) > df['high'])
    df['is_fractal_low'] = (df['low'].shift(2) < df['low'].shift(4)) & (df['low'].shift(2) < df['low'].shift(3)) & \
                           (df['low'].shift(2) < df['low'].shift(1)) & (df['low'].shift(2) < df['low'])
    return df

# ==========================================
# --- 4. SMC CORE - ĐÃ FIX HOÀN TOÀN ---
# ==========================================
def has_fvg(df, idx):
    if idx + 2 >= len(df): return False, None
    if df['low'].iloc[idx+2] > df['high'].iloc[idx]: return True, "bullish"
    if df['high'].iloc[idx+2] < df['low'].iloc[idx]: return True, "bearish"
    return False, None

def is_strong_displacement(df, idx, direction):
    if idx >= len(df): return False
    c = df.iloc[idx]
    body = abs(c['close'] - c['open'])
    if body < (c['high'] - c['low']) * 0.65: return False
    return (direction == "UP" and c['close'] > c['open']) or (direction == "DOWN" and c['close'] < c['open'])

def find_quality_zone(df, trend, atr):
    df = df.reset_index(drop=True)
    for i in range(25, len(df)-8):
        if trend == "UP":
            if df['close'].iloc[i] < df['open'].iloc[i]:  # last bearish candle
                if is_strong_displacement(df, i+1, "UP"):
                    fvg_ok, fvg_dir = has_fvg(df, i)
                    if fvg_ok and fvg_dir == "bullish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        sl = df['low'].iloc[i] - atr * SL_ATR_MULTIPLIER
                        whale = df['vol'].iloc[i+1:i+5].max() > df['vol'].iloc[max(0,i-25):i].mean() * WHALE_VOL_MULTIPLIER
                        sweep = df['low'].iloc[i-6:i+1].min() < df['low'].iloc[i-15:i-6].min() if i > 15 else False
                        return entry, sl, i, True, whale, sweep
        else:  # DOWN
            if df['close'].iloc[i] > df['open'].iloc[i]:
                if is_strong_displacement(df, i+1, "DOWN"):
                    fvg_ok, fvg_dir = has_fvg(df, i)
                    if fvg_ok and fvg_dir == "bearish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        sl = df['high'].iloc[i] + atr * SL_ATR_MULTIPLIER
                        whale = df['vol'].iloc[i+1:i+5].max() > df['vol'].iloc[max(0,i-25):i].mean() * WHALE_VOL_MULTIPLIER
                        sweep = df['high'].iloc[i-6:i+1].max() > df['high'].iloc[i-15:i-6].max() if i > 15 else False
                        return entry, sl, i, True, whale, sweep
    return 0, 0, 0, False, False, False

def is_mitigated(df, ob_idx, trend):
    recent = df.iloc[ob_idx+1:]
    if trend == "UP":
        return (recent['low'] < df['low'].iloc[ob_idx]).any()
    return (recent['high'] > df['high'].iloc[ob_idx]).any()

def get_htf_trend(symbol, htf):
    bars = fetch_ohlcv_safe(symbol, htf, limit=300)
    if not bars: return "SIDEWAY"
    df = pd.DataFrame(bars, columns=['ts','open','high','low','close','vol'])
    df = identify_fractals(df)
    recent_high = df['high'].iloc[-25:].max()
    recent_low = df['low'].iloc[-25:].min()
    price = df['close'].iloc[-1]
    if price > recent_high * 0.997: return "UP"
    if price < recent_low * 1.003: return "DOWN"
    return "SIDEWAY"

def get_swing_range(df):
    return df['high'].iloc[-250:].max(), df['low'].iloc[-250:].min()

def is_premium_discount(entry, trend, sh, sl):
    mid = (sh + sl) / 2
    return (trend == "UP" and entry < mid) or (trend == "DOWN" and entry > mid)

def is_trigger_candle(df, idx, sig):
    if idx < 1: return False, ""
    c, p = df.iloc[idx], df.iloc[idx-1]
    body = abs(c['close'] - c['open'])
    upper = c['high'] - max(c['close'], c['open'])
    lower = min(c['close'], c['open']) - c['low']
    if sig == "BUY":
        if lower > body * 2.2 and lower > upper: return True, "Bullish Pinbar"
        if p['close'] < p['open'] and c['close'] > p['open'] and c['close'] > c['open']: return True, "Bullish Engulfing"
    else:
        if upper > body * 2.2 and upper > lower: return True, "Bearish Pinbar"
        if p['close'] > p['open'] and c['close'] < p['open'] and c['close'] < c['open']: return True, "Bearish Engulfing"
    return False, ""

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# ==========================================
# --- 5. ANALYZE ---
# ==========================================
def analyze_pair(symbol, tf):
    if ENABLE_KILLZONES and datetime.utcnow().hour not in [7,8,9,10,12,13,14,15]:
        return False

    htf = MTF_MAPPING[tf]
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return False

    bars = fetch_ohlcv_safe(symbol, tf, 500)
    if not bars: return False

    df = pd.DataFrame(bars, columns=['ts','open','high','low','close','vol'])
    df = identify_fractals(df)
    df['atr'] = calculate_atr(df)
    df['rsi'] = calculate_rsi(df['close'])

    atr = df['atr'].iloc[-2]
    entry, sl, ob_idx, has_fvg, has_whale, has_sweep = find_quality_zone(df, htf_trend, atr)
    if not has_fvg or entry == 0: return False
    if is_mitigated(df, ob_idx, htf_trend): return False

    risk = abs(entry - sl)
    score = 4
    factors = [f"HTF {htf_trend}", "Valid OB + FVG + Displacement"]

    if has_whale:
        score += 1
        factors.append("Whale Volume 🐳")
    if has_sweep:
        score += 1
        factors.append("Liquidity Sweep 🦈")
    if is_premium_discount(entry, htf_trend, *get_swing_range(df)):
        score += 1
        factors.append("Premium/Discount 💎")

    rsi = df['rsi'].iloc[-2]
    if (htf_trend == "UP" and rsi < 48) or (htf_trend == "DOWN" and rsi > 52):
        score += 1
        factors.append(f"RSI {rsi:.1f}")

    if score < MIN_SCORE: return False

    # TP
    search = df.iloc[ob_idx+5:-1]
    tp = search['high'].max() if htf_trend == "UP" else search['low'].min()
    rr = abs(tp - entry) / risk if risk > 0 else 0
    if rr < 1.5: return False

    bars_since = len(df) - 2 - ob_idx
    if bars_since > MAX_BARS_LIMITS[tf] or abs(df['close'].iloc[-2] - entry) / atr > 6: return False

    key = f"{symbol}_{tf}_{ob_idx}"
    if ENABLE_ORDER_ANTISPAM and state_manager.is_alerted(key): return False

    sig = "BUY" if htf_trend == "UP" else "SELL"
    has_trig, trig_name = is_trigger_candle(df, len(df)-2, sig)

    tol = ENTRY_TOLERANCE * atr
    tapped = (sig == "BUY" and df.iloc[-2]['low'] <= entry + tol) or (sig == "SELL" and df.iloc[-2]['high'] >= entry - tol)

    exec_status = "CE Triggered ⚡" if tapped and has_trig else "Tapped 👀" if tapped else "Waiting ⏳"

    model = "🦄 UNICORN" if score >= 7 else "🔥 STRONG"

    msg = f"""🚀 <b>SMC v6.1 REAL EDGE</b> - {sig} {model}
{symbol} ({tf}) | Age {bars_since}/{MAX_BARS_LIMITS[tf]}
Score: <b>{score}/8</b> | {exec_status}

Entry: <code>{entry:.4f}</code>
SL: <code>{sl:.4f}</code> ({SL_ATR_MULTIPLIER}ATR)
TP: <code>{tp:.4f}</code> ({rr:.2f}R)

Confluences:
• {'\n• '.join(factors)}"""

    send_telegram(msg)
    print(f">>> {symbol} {tf} | {exec_status} | Score {score} | {rr:.2f}R")
    return True

# ==========================================
# --- MAIN ---
# ==========================================
if __name__ == "__main__":
    print(f"SMC Screener v6.1 REAL EDGE Started {datetime.now().strftime('%H:%M:%S')}")
    signals = 0
    for sym in PAIRS:
        for tf in MTF_MAPPING:
            if analyze_pair(sym, tf):
                signals += 1
            time.sleep(1.3)

    if signals == 0 and ENABLE_HEARTBEAT:
        send_telegram(f"🤖 SMC v6.1 ALIVE 🟢\nNo high-quality setup at {datetime.now().strftime('%H:%M:%S')}")
    print("Scan xong.")
