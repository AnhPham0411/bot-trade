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

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
# group_chat_id = "-5213535598"
CHAT_IDS = []
if user_chat_id:
    CHAT_IDS.append(user_chat_id)
if group_chat_id not in CHAT_IDS:
    CHAT_IDS.append(group_chat_id)
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

SUMMARY_REPORT = []

# ==========================================
# --- 2. HÀM CORE (INDICATOR, FRACTAL, FVG) ---
# ==========================================

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(100) 

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
    if idx + 2 >= len(df): return False

    if direction == "UP":
        candle_1_high = df['high'].iloc[idx]
        candle_3_low = df['low'].iloc[idx + 2]
        if candle_3_low > candle_1_high:
            return True 
    elif direction == "DOWN":
        candle_1_low = df['low'].iloc[idx]
        candle_3_high = df['high'].iloc[idx + 2]
        if candle_3_high < candle_1_low:
            return True 
    return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-1]
        
        # FINAL FIX 1: Detect SIDEWAY thực tế bằng Price vs EMA200 + ATR
        df['atr'] = calculate_atr(df, length=14)
        htf_atr = df['atr'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # Nếu giá cách EMA200 nhỏ hơn 1 ATR -> Thị trường đi ngang tích lũy
        if abs(current_price - ema_200) < htf_atr:
            return "SIDEWAY"
            
        return "UP" if current_price > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC TÌM OB + FVG (COMBINED) ---
# ==========================================

# FINAL FIX 2: Truyền current_atr vào để xử lý buffer ngay bên trong hàm
def find_quality_zone(df, trend, current_atr):
    zone_price = 0
    zone_sl = 0
    has_fvg = False

    fractal_lows = df[df['is_fractal_low'] == True]
    fractal_highs = df[df['is_fractal_high'] == True]

    if trend == "UP":
        if fractal_lows.empty: return 0, 0, False
        last_low_idx = fractal_lows.index[-1]

        # FINAL FIX 3: Mở rộng vùng tìm nến đỏ lên 5 nến trước đáy
        subset = df.iloc[max(0, last_low_idx-5):min(len(df), last_low_idx+3)]
        red_candles = subset[subset['close'] < subset['open']]

        if not red_candles.empty:
            best_ob = red_candles.loc[red_candles['low'].idxmin()]
            ob_idx = df.index.get_loc(best_ob.name)

            zone_price = (best_ob['high'] + best_ob['low']) / 2
            zone_sl = best_ob['low'] - (current_atr * 0.5) # Merge ATR Buffer
            has_fvg = check_fvg(df, ob_idx, "UP")
        else:
            zone_price = df['low'].iloc[last_low_idx]
            zone_sl = zone_price - (current_atr * 0.5)

    elif trend == "DOWN":
        if fractal_highs.empty: return 0, 0, False
        last_high_idx = fractal_highs.index[-1]

        # FINAL FIX 3: Mở rộng vùng tìm nến xanh lên 5 nến trước đỉnh
        subset = df.iloc[max(0, last_high_idx-5):min(len(df), last_high_idx+3)]
        green_candles = subset[subset['close'] > subset['open']]

        if not green_candles.empty:
            best_ob = green_candles.loc[green_candles['high'].idxmax()]
            ob_idx = df.index.get_loc(best_ob.name)

            zone_price = (best_ob['high'] + best_ob['low']) / 2
            zone_sl = best_ob['high'] + (current_atr * 0.5) # Merge ATR Buffer
            has_fvg = check_fvg(df, ob_idx, "DOWN")
        else:
            zone_price = df['high'].iloc[last_high_idx]
            zone_sl = zone_price + (current_atr * 0.5)

    return zone_price, zone_sl, has_fvg

# ==========================================
# --- 4. LOGIC CHẤM ĐIỂM (SCORING) ---
# ==========================================

def analyze_with_scoring(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    if not htf: return

    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": 
        return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    except: return

    df['rsi'] = calculate_rsi(df['close'])
    df['atr'] = calculate_atr(df, length=14) 
    df = identify_fractals(df)

    # Lấy giá trị ATR hiện tại TRƯỚC khi gọi hàm Zone
    current_atr = df['atr'].iloc[-1]
    # Fallback an toàn nếu dính nến lỗi
    if pd.isna(current_atr) or current_atr == 0: 
        current_atr = df['close'].iloc[-1] * 0.005 

    zone_entry, zone_sl, has_fvg = find_quality_zone(df, htf_trend, current_atr)

    if zone_entry == 0: 
        print(f"{symbol} ({tf}): No Struct found.")
        return

    curr = df.iloc[-2] 
    live = df.iloc[-1] 
    current_price = live['close']

    score = 0
    factors = []

    score += 1 
    factors.append(f"Trend {htf_trend}")

    if has_fvg:
        score += 1
        factors.append("SMC Imbalance (FVG)")
    else:
        factors.append("Standard OB")

    in_zone = False
    tolerance = current_atr * 0.5 

    if htf_trend == "UP":
        if (curr['low'] <= zone_entry + tolerance and curr['close'] > zone_sl) or \
           (live['low'] <= zone_entry + tolerance and live['close'] > zone_sl):
            in_zone = True
    elif htf_trend == "DOWN":
        if (curr['high'] >= zone_entry - tolerance and curr['close'] < zone_sl) or \
           (live['high'] >= zone_entry - tolerance and live['close'] < zone_sl):
            in_zone = True

    if in_zone:
        score += 1
        factors.append("Price Tap Zone (ATR Adjusted)")

        is_trigger = False
        body = abs(curr['close'] - curr['open'])

        if htf_trend == "UP":
            lower_wick = min(curr['open'], curr['close']) - curr['low']
            is_pinbar = lower_wick > body * 1.5
            is_engulfing = (curr['close'] > curr['open']) and (curr['close'] > df.iloc[-3]['high'])
            if is_pinbar or is_engulfing: is_trigger = True

        elif htf_trend == "DOWN":
            upper_wick = curr['high'] - max(curr['open'], curr['close'])
            is_pinbar = upper_wick > body * 1.5
            is_engulfing = (curr['close'] < curr['open']) and (curr['close'] < df.iloc[-3]['low'])
            if is_pinbar or is_engulfing: is_trigger = True

        if is_trigger:
            score += 1
            factors.append("Candle Trigger 🔥")

    # --- IN KẾT QUẢ ---
    status_msg = ""
    if in_zone:
        status_msg = f"⚡ IN ZONE (Score: {score}/4)"
    else:
        dist_percent = abs(current_price - zone_entry) / current_price * 100
        status_msg = f"Waiting: {zone_entry:.4f} (Away {dist_percent:.2f}%)"
        if has_fvg: status_msg += " [FVG+]"

    print(f"{symbol:<8} ({tf}) | {htf_trend:<4} | Score: {score}/4 | {status_msg}")
    SUMMARY_REPORT.append(f"{symbol} {tf}: {status_msg}")

    # --- RA QUYẾT ĐỊNH ---
    if in_zone and score >= 2:
        signal_type = "BUY" if htf_trend == "UP" else "SELL"
        strength = "STRONG 🔥" if score >= 3 else "MODERATE ⚠️"

        risk = abs(zone_entry - zone_sl)
        fib_multiplier = 1.618 
        tp_type = "" 
        tp = 0

        if signal_type == "BUY":
            tp_fib = zone_entry + (risk * fib_multiplier)
            fractal_highs = df[df['is_fractal_high'] == True]
            if not fractal_highs.empty:
                tp_struct = fractal_highs['high'].iloc[-1]
                rr_struct = (tp_struct - zone_entry) / risk if risk > 0 else 0
                if rr_struct < 1.5:
                    tp = tp_fib
                    tp_type = "Fib 1.618"
                else:
                    tp = tp_struct
                    tp_type = "Fractal High"
            else:
                tp = tp_fib
                tp_type = "Fib 1.618"
        else: 
            tp_fib = zone_entry - (risk * fib_multiplier)
            fractal_lows = df[df['is_fractal_low'] == True]
            if not fractal_lows.empty:
                tp_struct = fractal_lows['low'].iloc[-1]
                rr_struct = (zone_entry - tp_struct) / risk if risk > 0 else 0
                if rr_struct < 1.5:
                    tp = tp_fib
                    tp_type = "Fib 1.618"
                else:
                    tp = tp_struct
                    tp_type = "Fractal Low"
            else:
                tp = tp_fib
                tp_type = "Fib 1.618"

        rr = abs(tp - zone_entry) / risk if risk > 0 else 0
        reasons_str = "\n   + ".join(factors)

        msg = (
            f"💎 *SMC PRO SIGNAL ({strength})*\n"
            f"Symbol: {symbol} ({tf})\n"
            f"Score: *{score}/4* ✅\n"
            f"-----------------\n"
            f"Signal: *{signal_type}*\n"
            f"Entry Zone: `{zone_entry:.4f}`\n"
            f"Stoploss: `{zone_sl:.4f}` (ATR Buffered)\n"
            f"TP ({tp_type}): `{tp:.4f}`\n"
            f"R:R Thực tế: `1:{rr:.2f}`\n"
            f"-----------------\n"
            f"🔍 *Confluences:*\n   + {reasons_str}"
        )
        print(f"\n🚀 SIGNAL FOUND: {symbol} ({score} pts)")
        send_telegram(msg)

# ==========================================
# --- 5. HÀM GỬI TIN & MAIN ---
# ==========================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
        except: pass

if __name__ == "__main__":
    print(f"\n--- BOT SMC 10/10 (MASTERPIECE) ---")
    print("Criteria: Trend(1) + OB(1) + FVG(1) + Trigger(1)")
    print("Signal Condition: Score >= 2\n")

    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_with_scoring(symbol, tf)
            time.sleep(1)
