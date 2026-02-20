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
SL_ATR_MULTIPLIER = 0.8  # Buff SL 0.8 ATR
ENTRY_TOLERANCE = 0.5    # Sai số khớp Limit 0.5 ATR

# Lấy Token từ GitHub Secrets
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN_1') 
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
CHAT_IDS = [CHAT_ID] if CHAT_ID else []

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})

# ==========================================
# --- 2. HÀM CHỈ BÁO KỸ THUẬT ---
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
    """Tìm đỉnh đáy Fractal 5 nến chuẩn xác"""
    df['is_fractal_high'] = (df['high'].shift(2) > df['high'].shift(4)) & \
                             (df['high'].shift(2) > df['high'].shift(3)) & \
                             (df['high'].shift(2) > df['high'].shift(1)) & \
                             (df['high'].shift(2) > df['high'])
    df['is_fractal_low'] = (df['low'].shift(2) < df['low'].shift(4)) & \
                            (df['low'].shift(2) < df['low'].shift(3)) & \
                            (df['low'].shift(2) < df['low'].shift(1)) & \
                            (df['low'].shift(2) < df['low'])
    return df

def check_fvg(df, idx, direction):
    """Kiểm tra khoảng trống giá (Fair Value Gap)"""
    try:
        if direction == "UP": return df['low'].iloc[idx + 2] > df['high'].iloc[idx]
        return df['high'].iloc[idx + 2] < df['low'].iloc[idx]
    except: return False

def get_htf_trend(symbol, htf):
    """Xác định xu hướng khung lớn (HTF)"""
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=250)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-2]
        atr = calculate_atr(df).iloc[-2]
        # Nếu giá dao động quanh EMA trong khoảng 1 ATR thì coi là Sideway
        if abs(df['close'].iloc[-2] - ema_200) < atr: return "SIDEWAY"
        return "UP" if df['close'].iloc[-2] > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC MITIGATION CHECK (CHỐNG BÁO LẠI) ---
# ==========================================
def is_ob_fresh(df, ob_idx, entry, sl, tp, trend):
    """Kiểm tra OB đã bị xuyên thủng SL hoặc chạm TP trong quá khứ chưa"""
    # Lấy dữ liệu từ sau cây nến OB đến nến trước nến hiện tại
    history = df.iloc[ob_idx + 1 : -1]
    if history.empty: return True 
    
    if trend == "UP":
        # Vi phạm SL hoặc đã chạm mục tiêu TP2
        if (history['low'] <= sl).any() or (history['high'] >= tp).any():
            return False
    else: # DOWN
        if (history['high'] >= sl).any() or (history['low'] <= tp).any():
            return False
    return True

def find_quality_zone(df, trend, current_atr):
    """Tìm Order Block gần nhất và trả về thông số kèm vị trí nến"""
    if trend == "UP":
        fractals = df[df['is_fractal_low']]
        if fractals.empty: return 0, 0, 0, False
        idx = fractals.index[-1]
        sub = df.iloc[max(0, idx-5):idx+3]
        red = sub[sub['close'] < sub['open']]
        if not red.empty:
            ob = red.loc[red['low'].idxmin()]
            ob_pos = df.index.get_loc(ob.name)
            return (ob['high']+ob['low'])/2, ob['low']-(current_atr*SL_ATR_MULTIPLIER), ob_pos, check_fvg(df, ob_pos, "UP")
    else:
        fractals = df[df['is_fractal_high']]
        if fractals.empty: return 0, 0, 0, False
        idx = fractals.index[-1]
        sub = df.iloc[max(0, idx-5):idx+3]
        green = sub[sub['close'] > sub['open']]
        if not green.empty:
            ob = green.loc[green['high'].idxmax()]
            ob_pos = df.index.get_loc(ob.name)
            return (ob['high']+ob['low'])/2, ob['high']+(current_atr*SL_ATR_MULTIPLIER), ob_pos, check_fvg(df, ob_pos, "DOWN")
    return 0, 0, 0, False

# ==========================================
# --- 4. ENGINE PHÂN TÍCH VÀ BẮN TIN ---
# ==========================================
def analyze_pair(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = identify_fractals(pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol']))
        atr = calculate_atr(df).iloc[-1]
        if pd.isna(atr) or atr == 0: atr = df['close'].iloc[-1] * 0.005

        entry, sl, ob_idx, has_fvg = find_quality_zone(df, htf_trend, atr)
        if entry == 0: return

        # Tính TP2 để check mitigation
        risk = abs(entry - sl)
        tp2 = (entry + risk * 1.6) if htf_trend == "UP" else (entry - risk * 1.6)

        # CHỐNG BÁO LỆNH CŨ: Nếu OB đã bị 'mitigated' thì dừng
        if not is_ob_fresh(df, ob_idx, entry, sl, tp2, htf_trend):
            return 

        # Kiểm tra nến hiện tại có đang chạm Entry không (Limit Order Fill)
        live = df.iloc[-1]
        in_zone = False
        if htf_trend == "UP":
            in_zone = live['low'] <= entry + (atr * ENTRY_TOLERANCE) and live['close'] > sl
        else:
            in_zone = live['high'] >= entry - (atr * ENTRY_TOLERANCE) and live['close'] < sl

        if in_zone:
            tp1 = (entry + risk) if htf_trend == "UP" else (entry - risk)
            side = "BUY" if htf_trend == "UP" else "SELL"
            fvg_msg = "✅ Kèm FVG" if has_fvg else "❌ Không FVG"
            
            msg = (f"💎 <b>SMC LIMIT ALERT (FRESH)</b>\n"
                   f"Cặp: {symbol} ({tf})\n"
                   f"Lệnh: <b>{side} Limit</b>\n"
                   f"Hợp lưu: {fvg_msg}\n"
                   f"-----------------\n"
                   f"Entry: <code>{entry:.4f}</code>\n"
                   f"Stoploss: <code>{sl:.4f}</code>\n"
                   f"TP1 (1R): <code>{tp1:.4f}</code>\n"
                   f"TP2 (1.6R): <code>{tp2:.4f}</code>")
            send_telegram(msg)
            print(f">>> {symbol} {tf}: Signal Sent!")
    except Exception as e: 
        print(f"Error analyzing {symbol}: {e}")

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# ==========================================
# --- 5. MAIN ---
# ==========================================
if __name__ == "__main__":
    print(f"Bot 3 Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_pair(symbol, tf)
            time.sleep(1) # Chống bị sàn khóa IP
    print("Bot 3 Finished.")
