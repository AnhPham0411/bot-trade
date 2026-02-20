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

# Mapping MTF
MTF_MAPPING = {
    '15m': '1h',
    '1h':  '4h',
    '4h':  '1d'
}

# --- CẤU HÌNH QUẢN LÝ VỐN & BUFFER ---
SL_ATR_MULTIPLIER = 0.8  # Giữ nguyên Buff SL 0.8 ATR theo yêu cầu
ENTRY_TOLERANCE = 0.5    # Khoảng sai số để khớp lệnh Limit

# Nhận Token từ GitHub Secrets (Sửa theo tên biến trong file YAML của bạn)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN_1') 
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

CHAT_IDS = [CHAT_ID] if CHAT_ID else []

exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# ==========================================
# --- 2. HÀM CORE (INDICATOR, FRACTAL, FVG) ---
# ==========================================

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_atr(df, length=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    return np.max(ranges, axis=1).rolling(window=length).mean()

def identify_fractals(df):
    """Tìm đỉnh đáy Fractal 5 nến"""
    df['is_fractal_high'] = False
    df['is_fractal_low'] = False
    for i in range(2, len(df) - 2):
        if (df['high'].iloc[i] > df['high'].iloc[i-1] and df['high'].iloc[i] > df['high'].iloc[i-2] and 
            df['high'].iloc[i] > df['high'].iloc[i+1] and df['high'].iloc[i] > df['high'].iloc[i+2]):
            df.at[df.index[i], 'is_fractal_high'] = True
        if (df['low'].iloc[i] < df['low'].iloc[i-1] and df['low'].iloc[i] < df['low'].iloc[i-2] and 
            df['low'].iloc[i] < df['low'].iloc[i+1] and df['low'].iloc[i] < df['low'].iloc[i+2]):
            df.at[df.index[i], 'is_fractal_low'] = True
    return df

def check_fvg(df, idx, direction):
    try:
        if idx + 2 >= len(df): return False
        if direction == "UP":
            return df['low'].iloc[idx + 2] > df['high'].iloc[idx]
        elif direction == "DOWN":
            return df['high'].iloc[idx + 2] < df['low'].iloc[idx]
    except: return False
    return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=250)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-2]
        atr = calculate_atr(df, length=14).iloc[-2]
        past_price = df['close'].iloc[-2]
        if abs(past_price - ema_200) < atr: return "SIDEWAY"
        return "UP" if past_price > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC TÌM OB ---
# ==========================================

def find_quality_zone(df, trend, current_atr):
    zone_price, zone_sl, has_fvg = 0, 0, False
    fractal_lows = df[df['is_fractal_low'] == True]
    fractal_highs = df[df['is_fractal_high'] == True]

    if trend == "UP" and not fractal_lows.empty:
        idx = fractal_lows.index[-1]
        sub = df.iloc[max(0, idx-5):min(len(df), idx+3)]
        red = sub[sub['close'] < sub['open']]
        if not red.empty:
            ob = red.loc[red['low'].idxmin()]
            zone_price = (ob['high'] + ob['low']) / 2
            zone_sl = ob['low'] - (current_atr * SL_ATR_MULTIPLIER)
            has_fvg = check_fvg(df, df.index.get_loc(ob.name), "UP")
        else:
            zone_price = df['low'].iloc[idx]
            zone_sl = zone_price - (current_atr * SL_ATR_MULTIPLIER)

    elif trend == "DOWN" and not fractal_highs.empty:
        idx = fractal_highs.index[-1]
        sub = df.iloc[max(0, idx-5):min(len(df), idx+3)]
        green = sub[sub['close'] > sub['open']]
        if not green.empty:
            ob = green.loc[green['high'].idxmax()]
            zone_price = (ob['high'] + ob['low']) / 2
            zone_sl = ob['high'] + (current_atr * SL_ATR_MULTIPLIER)
            has_fvg = check_fvg(df, df.index.get_loc(ob.name), "DOWN")
        else:
            zone_price = df['high'].iloc[idx]
            zone_sl = zone_price + (current_atr * SL_ATR_MULTIPLIER)

    return zone_price, zone_sl, has_fvg

# ==========================================
# --- 4. ENGINE PHÂN TÍCH ---
# ==========================================

def analyze_with_scoring(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=200)
        df = identify_fractals(pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol']))
        atr = calculate_atr(df).iloc[-1]
        if pd.isna(atr) or atr == 0: atr = df['close'].iloc[-1] * 0.005

        entry, sl, fvg = find_quality_zone(df, htf_trend, atr)
        if entry == 0: return

        live = df.iloc[-1]
        in_zone = False
        if htf_trend == "UP":
            in_zone = live['low'] <= entry + (atr * ENTRY_TOLERANCE) and live['close'] > sl
        else:
            in_zone = live['high'] >= entry - (atr * ENTRY_TOLERANCE) and live['close'] < sl

        if in_zone:
            side = "BUY" if htf_trend == "UP" else "SELL"
            risk = abs(entry - sl)
            tp1, tp2 = (entry + risk, entry + risk * 1.618) if side == "BUY" else (entry - risk, entry - risk * 1.618)
            
            msg = (f"💎 <b>SMC LIMIT ALERT</b>\n"
                   f"Cặp: {symbol} ({tf})\n"
                   f"Lệnh: <b>{side} Limit</b>\n"
                   f"-----------------\n"
                   f"Entry: <code>{entry:.4f}</code>\n"
                   f"Stoploss: <code>{sl:.4f}</code>\n"
                   f"TP1 (1R): <code>{tp1:.4f}</code>\n"
                   f"TP2 (1.6R): <code>{tp2:.4f}</code>")
            send_telegram(msg)
            print(f"Signal sent for {symbol} {tf}")
    except Exception as e: print(f"Error {symbol}: {e}")

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# ==========================================
# --- 5. MAIN EXECUTION (KHÔNG VÒNG LẶP) ---
# ==========================================
if __name__ == "__main__":
    print(f"Bot 3 (SMC PRO) Started at {datetime.now()}")
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_with_scoring(symbol, tf)
            time.sleep(1) # Chống rate limit
    print("Bot 3 Finished.")
