import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import logging
import joblib  # pip install scikit-learn joblib
from datetime import datetime

# ==========================================
# --- CẤU HÌNH HỆ THỐNG ---
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_CONFIG = {
    '15m': {'htf1': '1h', 'htf2': '4h'},
    '1h':  {'htf1': '4h', 'htf2': '1d'},
    '4h':  {'htf1': '1d', 'htf2': '1w'}
}

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"

CHAT_IDS = []
if chat_id: CHAT_IDS.append(chat_id)
if group_chat_id not in CHAT_IDS: CHAT_IDS.append(group_chat_id)

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}, 'rateLimit': 1500})
SCORE_THRESHOLD = 7.5 # Nâng ngưỡng do có thêm AI & Volume Profile

MODEL_PATH = "smc_model.pkl"

# ==========================================
# --- CHỈ BÁO KỸ THUẬT NÂNG CAO ---
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
    """Tính MACD & Stochastic"""
    fast, slow, signal = 12, 26, 9
    exp1 = df['close'].ewm(span=fast, adjust=False).mean()
    exp2 = df['close'].ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    
    low_k = df['low'].rolling(window=14).min()
    high_k = df['high'].rolling(window=14).max()
    stoch_k = 100 * ((df['close'] - low_k) / (high_k - low_k))
    stoch_d = stoch_k.rolling(window=3).mean()
    return macd, macd_signal, stoch_k, stoch_d

def volume_profile_poc(df, bins=30):
    """Tối ưu hóa tính POC (Point of Control)"""
    if len(df) < 50: return np.nan
    df_recent = df.iloc[-100:]
    price_bins = np.linspace(df_recent['low'].min(), df_recent['high'].max(), bins)
    vprofile = df_recent.groupby(pd.cut(df_recent['close'], bins=price_bins))['vol'].sum()
    return (vprofile.idxmax().left + vprofile.idxmax().right) / 2

def identify_fractals(df):
    df['is_f_high'] = (df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(2)) & \
                      (df['high'] > df['high'].shift(-1)) & (df['high'] > df['high'].shift(-2))
    df['is_f_low'] = (df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(2)) & \
                     (df['low'] < df['low'].shift(-1)) & (df['low'] < df['low'].shift(-2))
    df.loc[df.index[-2:], ['is_f_high', 'is_f_low']] = False
    return df

# ==========================================
# --- LÕI SMC & AI ---
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
        sl = (ob['low'] - atr*0.3) if trend == "UP" else (ob['high'] + atr*0.3)
        fvg = (df['low'].iloc[p+2] > ob['high']) if (trend == "UP" and p+2 < len(df)) else (df['high'].iloc[p+2] < ob['low']) if (trend == "DOWN" and p+2 < len(df)) else False
        vol = ob['vol'] > df['vol'].iloc[max(0, p-20):p].mean() * 1.3
        return ent, sl, fvg, vol
    return 0, 0, False, False

def analyze_with_scoring(symbol, tf, model=None):
    h1, h2 = MTF_CONFIG[tf]['htf1'], MTF_CONFIG[tf]['htf2']
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=200)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = identify_fractals(df)
        
        t1 = "UP" if df['close'].iloc[-1] > calculate_ema(df['close'], 200).iloc[-1] else "DOWN"
        if t1 == "SIDEWAY": return False # Logic sideway đã được xử lý ngầm
        
        atr, rsi = calculate_atr(df).iloc[-1], calculate_rsi(df).iloc[-2]
        macd, m_sig, s_k, s_d = calculate_quant_indicators(df)
        poc = volume_profile_poc(df)
        fib_50 = (df['high'].iloc[-50:].max() + df['low'].iloc[-50:].min()) / 2
        
        ent, sl, fvg, v_conf = find_ob_zone(df, t1, atr)
        if ent == 0: return False

        # Scoring & Confluences
        score, factors = 0.0, []
        if v_conf: score += 2.0; factors.append("Volume OB Spike (+2)")
        if fvg: score += 2.0; factors.append("FVG Imbalance (+2)")
        if (t1 == "UP" and ent < fib_50) or (t1 == "DOWN" and ent > fib_50): score += 1.5; factors.append("Discount/Premium (+1.5)")
        if (t1 == "UP" and macd.iloc[-2] > m_sig.iloc[-2]) or (t1 == "DOWN" and macd.iloc[-2] < m_sig.iloc[-2]): score += 1.0; factors.append("MACD Support (+1)")
        if not np.isnan(poc) and ((t1 == "UP" and ent < poc) or (t1 == "DOWN" and ent > poc)): score += 1.0; factors.append("POC Support (+1)")

        # Check Price in Zone & AI
        live = df.iloc[-1]
        in_zone = (t1 == "UP" and live['low'] <= ent + atr*0.3) or (t1 == "DOWN" and live['high'] >= ent - atr*0.3)
        if in_zone: score += 2.0; factors.append("Price in Zone (+2)")

        if model:
            # Feature nhị phân chuẩn hóa cho AI
            feat = [[int(v_conf), int(fvg), int(ent < fib_50), int(macd.iloc[-2] > m_sig.iloc[-2]), int(in_zone), int(30 < rsi < 70)]]
            prob = model.predict_proba(feat)[0][1]
            if prob > 0.65: score += 2.0; factors.append(f"AI Power ({prob:.0%}) (+2)")

        if in_zone and score >= SCORE_THRESHOLD:
            rr = 3.0 if score >= 9 else 2.0
            tp = ent + abs(ent - sl) * rr if t1 == "UP" else ent - abs(ent - sl) * rr
            
            msg = (f"<b>💎 SMC v8.1 (AI + QUANT MASTER)</b>\n"
                   f"Cặp: {symbol} ({tf})\n"
                   f"Điểm: <b>{score:.1f}/10</b>\n"
                   f"Lệnh: <b>{'BUY' if t1 == 'UP' else 'SELL'}</b>\n"
                   f"Entry: <code>{ent:.4f}</code>\n"
                   f"TP (R:R {rr}): <code>{tp:.4f}</code>\n"
                   f"🔍 <b>Hội tụ:</b>\n- " + "\n- ".join(factors))
            send_telegram(msg)
            return True
    except Exception as e: logger.error(f"Lỗi {symbol}: {e}")
    return False

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    for cid in CHAT_IDS:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

if __name__ == "__main__":
    logger.info("=== SMC ANHALGO v8.1 ACTIVE ===")
    model = joblib.load(MODEL_PATH) if os.path.exists(MODEL_PATH) else None
    for s in PAIRS:
        for t in MTF_CONFIG.keys():
            analyze_with_scoring(s, t, model)
            time.sleep(1.5)
