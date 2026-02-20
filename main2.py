import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import logging
import joblib
from datetime import datetime

# ==========================================
# --- 1. CẤU HÌNH HỆ THỐNG ---
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_CONFIG = {
    '15m': {'htf1': '1h', 'htf2': '4h'},
    '1h':  {'htf1': '4h', 'htf2': '1d'},
    '4h':  {'htf1': '1d', 'htf2': '1w'}
}

# Tham số chiến thuật
SL_ATR_MULTIPLIER = 0.8    # Buff SL 0.8 ATR (Giúp tín hiệu an toàn hơn)
SCORE_THRESHOLD = 7.5      # Ngưỡng điểm để báo lệnh
MODEL_PATH = "smc_model.pkl"

# Secrets từ GitHub
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN_1')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

exchange = ccxt.mexc({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'},
    'rateLimit': 1500
})

# ==========================================
# --- 2. CHỈ BÁO KỸ THUẬT NÂNG CAO ---
# ==========================================
def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(window=length).mean()

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calculate_quant_indicators(df):
    fast, slow, signal = 12, 26, 9
    exp1 = df['close'].ewm(span=fast, adjust=False).mean()
    exp2 = df['close'].ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    return macd, macd_signal

def identify_fractals(df):
    df['is_f_high'] = (df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(2)) & \
                      (df['high'] > df['high'].shift(-1)) & (df['high'] > df['high'].shift(-2))
    df['is_f_low'] = (df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(2)) & \
                     (df['low'] < df['low'].shift(-1)) & (df['low'] < df['low'].shift(-2))
    df.loc[df.index[-2:], ['is_f_high', 'is_f_low']] = False
    return df

# ==========================================
# --- 3. LÕI SMC & AI SCORING ---
# ==========================================
def find_ob_zone(df, trend, atr):
    f_col = 'is_f_low' if trend == "UP" else 'is_f_high'
    fractals = df[df[f_col] == True]
    if fractals.empty: return 0, 0, False, False
    
    idx = fractals.index[-1]
    pos = df.index.get_loc(idx)
    sub = df.iloc[max(0, pos-5):pos+3]
    
    candles = sub[sub['close'] < sub['open']] if trend == "UP" else sub[sub['close'] > sub['open']]
    if not candles.empty:
        ob = candles.loc[candles['low'].idxmin()] if trend == "UP" else candles.loc[candles['high'].idxmax()]
        p = df.index.get_loc(ob.name)
        ent = (ob['high'] + ob['low']) / 2
        # Sử dụng cơ chế SL theo ATR
        sl = (ob['low'] - atr * SL_ATR_MULTIPLIER) if trend == "UP" else (ob['high'] + atr * SL_ATR_MULTIPLIER)
        
        fvg = (df['low'].iloc[p+2] > ob['high']) if (trend == "UP" and p+2 < len(df)) else (df['high'].iloc[p+2] < ob['low']) if (trend == "DOWN" and p+2 < len(df)) else False
        vol = ob['vol'] > df['vol'].iloc[max(0, p-20):p].mean() * 1.3
        return ent, sl, fvg, vol
    return 0, 0, False, False

def analyze_with_scoring(symbol, tf, model=None):
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=200)
        df = identify_fractals(pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol']))
        
        # Xác định xu hướng HTF dựa trên EMA 200
        t1 = "UP" if df['close'].iloc[-1] > calculate_ema(df['close'], 200).iloc[-1] else "DOWN"
        
        atr = calculate_atr(df).iloc[-1]
        rsi = calculate_rsi(df).iloc[-2]
        macd, m_sig = calculate_quant_indicators(df)
        fib_50 = (df['high'].iloc[-50:].max() + df['low'].iloc[-50:].min()) / 2
        
        ent, sl, fvg, v_conf = find_ob_zone(df, t1, atr)
        if ent == 0: return False

        # Scoring Logic
        score, factors = 0.0, []
        if v_conf: score += 2.0; factors.append("Volume OB Spike (+2)")
        if fvg: score += 2.0; factors.append("FVG Imbalance (+2)")
        if (t1 == "UP" and ent < fib_50) or (t1 == "DOWN" and ent > fib_50): score += 1.5; factors.append("Discount/Premium (+1.5)")
        if (t1 == "UP" and macd.iloc[-2] > m_sig.iloc[-2]) or (t1 == "DOWN" and macd.iloc[-2] < m_sig.iloc[-2]): score += 1.0; factors.append("MACD Support (+1)")

        live = df.iloc[-1]
        in_zone = (t1 == "UP" and live['low'] <= ent + atr*0.3) or (t1 == "DOWN" and live['high'] >= ent - atr*0.3)
        if in_zone: score += 2.0; factors.append("Price in Zone (+2)")

        if model:
            feat = [[int(v_conf), int(fvg), int(ent < fib_50), int(macd.iloc[-2] > m_sig.iloc[-2]), int(in_zone), int(30 < rsi < 70)]]
            prob = model.predict_proba(feat)[0][1]
            if prob > 0.65: score += 2.0; factors.append(f"AI Power ({prob:.0%}) (+2)")

        if in_zone and score >= SCORE_THRESHOLD:
            rr = 3.0 if score >= 9 else 2.0
            tp = ent + abs(ent - sl) * rr if t1 == "UP" else ent - abs(ent - sl) * rr
            
            msg = (f"<b>💎 SMC v8.1 AI SIGNAL</b>\n"
                   f"Cặp: {symbol} ({tf})\n"
                   f"Điểm: <b>{score:.1f}/10</b>\n"
                   f"Lệnh: <b>{'BUY' if t1 == 'UP' else 'SELL'}</b>\n"
                   f"-------------------\n"
                   f"Entry: <code>{ent:.4f}</code>\n"
                   f"Stoploss: <code>{sl:.4f}</code>\n"
                   f"TakeProfit (R:R {rr}): <code>{tp:.4f}</code>\n"
                   f"-------------------\n"
                   f"🔍 <b>Hội tụ:</b>\n- " + "\n- ".join(factors))
            send_telegram(msg)
            return True
    except Exception as e: logger.error(f"Lỗi {symbol}: {e}")
    return False

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                       json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==========================================
# --- 4. MAIN ---
# ==========================================
if __name__ == "__main__":
    print(f"Bot v8.1 Start: {datetime.now().strftime('%H:%M:%S')}")
    model = joblib.load(MODEL_PATH) if os.path.exists(MODEL_PATH) else None
    
    for s in PAIRS:
        for t in MTF_CONFIG.keys():
            analyze_with_scoring(s, t, model)
            time.sleep(1.5)
