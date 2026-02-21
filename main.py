import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
from datetime import datetime

# ==========================================
# --- 1. CẤU HÌNH ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

# Tham số chiến thuật
SL_ATR_MULTIPLIER = 0.8  # Nâng SL lên 0.8 ATR
ENTRY_TOLERANCE = 0.5    # Sai số chạm Zone

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN') # Đảm bảo đúng tên biến trong GitHub Secrets
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
CHAT_IDS = [CHAT_ID] if CHAT_ID else []

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})

# ==========================================
# --- 2. HÀM CORE (INDICATOR & FRACTAL) ---
# ==========================================
def calculate_ema(series, length): return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(window=length).mean()

def identify_fractals(df):
    df['is_fractal_high'] = (df['high'].shift(2) > df['high'].shift(4)) & (df['high'].shift(2) > df['high'].shift(3)) & \
                             (df['high'].shift(2) > df['high'].shift(1)) & (df['high'].shift(2) > df['high'])
    df['is_fractal_low'] = (df['low'].shift(2) < df['low'].shift(4)) & (df['low'].shift(2) < df['low'].shift(3)) & \
                            (df['low'].shift(2) < df['low'].shift(1)) & (df['low'].shift(2) < df['low'])
    return df

def check_fvg(df, idx, direction):
    try:
        if direction == "UP": return df['low'].iloc[idx + 2] > df['high'].iloc[idx]
        return df['high'].iloc[idx + 2] < df['low'].iloc[idx]
    except: return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-1]
        atr = calculate_atr(df).iloc[-1]
        if abs(df['close'].iloc[-1] - ema_200) < atr: return "SIDEWAY"
        return "UP" if df['close'].iloc[-1] > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC MITIGATION & ZONE ---
# ==========================================
def is_ob_fresh(df, ob_idx, sl, tp, trend):
    """Kiểm tra xem từ lúc tạo OB đến giờ giá đã chạm SL/TP chưa"""
    history = df.iloc[ob_idx + 1 : -1]
    if history.empty: return True
    if trend == "UP":
        if (history['low'] <= sl).any() or (history['high'] >= tp).any(): return False
    else:
        if (history['high'] >= sl).any() or (history['low'] <= tp).any(): return False
    return True

def find_quality_zone(df, trend, current_atr):
    """Giữ nguyên logic tìm nến ngược màu gần Fractal của bạn"""
    fractal_lows = df[df['is_fractal_low']]
    fractal_highs = df[df['is_fractal_high']]

    if trend == "UP" and not fractal_lows.empty:
        idx = df.index.get_loc(fractal_lows.index[-1])
        sub = df.iloc[max(0, idx-5):idx+3]
        red = sub[sub['close'] < sub['open']]
        if not red.empty:
            ob = red.loc[red['low'].idxmin()]
            pos = df.index.get_loc(ob.name)
            return (ob['high']+ob['low'])/2, ob['low']-(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "UP")
        return df['low'].iloc[idx], df['low'].iloc[idx]-(current_atr*SL_ATR_MULTIPLIER), idx, False

    elif trend == "DOWN" and not fractal_highs.empty:
        idx = df.index.get_loc(fractal_highs.index[-1])
        sub = df.iloc[max(0, idx-5):idx+3]
        green = sub[sub['close'] > sub['open']]
        if not green.empty:
            ob = green.loc[green['high'].idxmax()]
            pos = df.index.get_loc(ob.name)
            return (ob['high']+ob['low'])/2, ob['high']+(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "DOWN")
        return df['high'].iloc[idx], df['high'].iloc[idx]+(current_atr*SL_ATR_MULTIPLIER), idx, False
    return 0, 0, 0, False

# ==========================================
# --- 4. ENGINE CHẤM ĐIỂM (GIỮ NGUYÊN) ---
# ==========================================
def analyze_with_scoring(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = identify_fractals(pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol']))
        atr = calculate_atr(df).iloc[-1]
        
        entry, sl, ob_idx, has_fvg = find_quality_zone(df, htf_trend, atr)
        if entry == 0: return

        # Tính TP để check mitigation
        risk = abs(entry - sl)
        tp2 = (entry + risk * 1.618) if htf_trend == "UP" else (entry - risk * 1.618)

        # BƯỚC MỚI: Check Mitigation
        if not is_ob_fresh(df, ob_idx, sl, tp2, htf_trend): return

        curr, live = df.iloc[-2], df.iloc[-1]
        score, factors = 1, [f"Trend {htf_trend}"] # Point 1: Trend
        
        if has_fvg: score += 1; factors.append("SMC FVG")
        
        in_zone = False
        if htf_trend == "UP":
            if (curr['low'] <= entry + atr*ENTRY_TOLERANCE and curr['close'] > sl) or (live['low'] <= entry + atr*ENTRY_TOLERANCE):
                in_zone = True
        else:
            if (curr['high'] >= entry - atr*ENTRY_TOLERANCE and curr['close'] < sl) or (live['high'] >= entry - atr*ENTRY_TOLERANCE):
                in_zone = True

        if in_zone:
            score += 1; factors.append("Tap Zone") # Point 3: Tap
            
            # Point 4: Candle Trigger (Logic nguyên bản của bạn)
            is_trigger, body = False, abs(curr['close'] - curr['open'])
            if htf_trend == "UP":
                if (min(curr['open'], curr['close']) - curr['low'] > body * 1.5) or (curr['close'] > curr['open'] and curr['close'] > df.iloc[-3]['high']):
                    is_trigger = True
            else:
                if (curr['high'] - max(curr['open'], curr['close']) > body * 1.5) or (curr['close'] < curr['open'] and curr['close'] < df.iloc[-3]['low']):
                    is_trigger = True
            
            if is_trigger: score += 1; factors.append("Trigger 🔥")

            # Gửi tín hiệu nếu Score >= 2
            if score >= 2:
                tp1 = (entry + risk) if htf_trend == "UP" else (entry - risk)
                msg = (f"🚀 <b>SMC MAIN-1 SIGNAL</b>\n"
                       f"Cặp: {symbol} ({tf})\n"
                       f"Score: <b>{score}/4</b> ✅\n"
                       f"-----------------\n"
                       f"Entry: <code>{entry:.4f}</code>\n"
                       f"SL: <code>{sl:.4f}</code> (0.8 ATR)\n"
                       f"<b>TP1 (1R):</b> <code>{tp1:.4f}</code>\n"
                       f"<b>TP2 (1.6R):</b> <code>{tp2:.4f}</code>\n"
                       f"-----------------\n"
                       f"🔍 Hợp lưu:\n+ " + "\n+ ".join(factors))
                send_telegram(msg)
    except Exception as e: print(f"Error {symbol}: {e}")

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

if __name__ == "__main__":
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_with_scoring(symbol, tf)
            time.sleep(1)
