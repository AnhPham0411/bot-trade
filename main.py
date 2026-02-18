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
CHAT_IDS = [os.getenv('TELEGRAM_CHAT_ID')]

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
    loss = loss.replace(0, np.nan)
    return 100 - (100 / (1 + (gain / loss)))

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
        if (df['high'][i] > df['high'][i-1] and df['high'][i] > df['high'][i-2] and 
            df['high'][i] > df['high'][i+1] and df['high'][i] > df['high'][i+2]):
            df.at[i, 'is_fractal_high'] = True
        if (df['low'][i] < df['low'][i-1] and df['low'][i] < df['low'][i-2] and 
            df['low'][i] < df['low'][i+1] and df['low'][i] < df['low'][i+2]):
            df.at[i, 'is_fractal_low'] = True
    return df

def check_fvg(df, idx, direction):
    """
    Kiểm tra xem cây nến tại idx (hoặc ngay sau nó) có tạo ra FVG không.
    """
    if idx + 2 >= len(df): return False
    
    # FVG Bullish: Low[i+2] > High[i] (Có khoảng trống)
    if direction == "UP":
        candle_1_high = df['high'].iloc[idx]
        candle_3_low = df['low'].iloc[idx + 2]
        if candle_3_low > candle_1_high:
            return True # Có FVG Tăng
            
    # FVG Bearish: High[i+2] < Low[i]
    elif direction == "DOWN":
        candle_1_low = df['low'].iloc[idx]
        candle_3_high = df['high'].iloc[idx + 2]
        if candle_3_high < candle_1_low:
            return True # Có FVG Giảm
            
    return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-1]
        return "UP" if df['close'].iloc[-1] > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC TÌM OB + FVG (COMBINED) ---
# ==========================================

def find_quality_zone(df, trend):
    """
    Tìm vùng Buy/Sell tốt nhất:
    Trả về: Giá Entry, Giá SL, Có FVG không?
    """
    zone_price = 0
    zone_sl = 0
    has_fvg = False
    
    fractal_lows = df[df['is_fractal_low'] == True]
    fractal_highs = df[df['is_fractal_high'] == True]

    if trend == "UP":
        if fractal_lows.empty: return 0, 0, False
        last_low_idx = fractal_lows.index[-1]
        
        # Quét nến đỏ gần đáy nhất
        subset = df.iloc[max(0, last_low_idx-3):min(len(df), last_low_idx+3)]
        red_candles = subset[subset['close'] < subset['open']]
        
        if not red_candles.empty:
            best_ob = red_candles.loc[red_candles['low'].idxmin()]
            ob_idx = df.index.get_loc(best_ob.name)
            
            zone_price = best_ob['high']
            zone_sl = best_ob['low']
            
            # Check xem ngay sau OB có FVG không? (Tăng độ uy tín)
            has_fvg = check_fvg(df, ob_idx, "UP")
        else:
            # Fallback về đáy nếu không thấy nến đỏ
            zone_price = df['low'].iloc[last_low_idx]
            zone_sl = zone_price * 0.995

    elif trend == "DOWN":
        if fractal_highs.empty: return 0, 0, False
        last_high_idx = fractal_highs.index[-1]
        
        subset = df.iloc[max(0, last_high_idx-3):min(len(df), last_high_idx+3)]
        green_candles = subset[subset['close'] > subset['open']]
        
        if not green_candles.empty:
            best_ob = green_candles.loc[green_candles['high'].idxmax()]
            ob_idx = df.index.get_loc(best_ob.name)
            
            zone_price = best_ob['low']
            zone_sl = best_ob['high']
            
            has_fvg = check_fvg(df, ob_idx, "DOWN")
        else:
            zone_price = df['high'].iloc[last_high_idx]
            zone_sl = zone_price * 1.005

    return zone_price, zone_sl, has_fvg

# ==========================================
# --- 4. LOGIC CHẤM ĐIỂM (SCORING) ---
# ==========================================

def analyze_with_scoring(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    if not htf: return

    # 1. Check Trend HTF
    htf_trend = get_htf_trend(symbol, htf)
    
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    except: return

    df['rsi'] = calculate_rsi(df['close'])
    df = identify_fractals(df)

    # 2. Tìm Zone (OB + FVG)
    zone_entry, zone_sl, has_fvg = find_quality_zone(df, htf_trend)
    
    if zone_entry == 0: 
        print(f"{symbol} ({tf}): No Struct found.")
        return

    curr = df.iloc[-2] # Nến vừa đóng
    current_price = df.iloc[-1]['close']
    
    # --- BẮT ĐẦU CHẤM ĐIỂM ---
    score = 0
    factors = []
    
    # Điểm 1: Trend HTF (Mặc định lọc theo trend nên auto +1 nếu pass)
    score += 1 
    factors.append(f"Trend {htf_trend}")
    
    # Điểm 2: Chất lượng Zone (Có FVG không?)
    if has_fvg:
        score += 1
        factors.append("SMC Imbalance (FVG)")
    else:
        factors.append("Standard OB")

    # Điểm 3: Giá đã về vùng Entry chưa? (Price Action)
    # Chấp nhận sai số 0.3%
    in_zone = False
    tolerance = zone_entry * 0.003
    
    if htf_trend == "UP":
        dist = curr['low'] - zone_entry
        if dist <= tolerance and curr['close'] > zone_sl:
            in_zone = True
    elif htf_trend == "DOWN":
        dist = zone_entry - curr['high']
        if dist <= tolerance and curr['close'] < zone_sl:
            in_zone = True
            
    if in_zone:
        score += 1
        factors.append("Price Tap Zone")
        
        # Điểm 4: Trigger Nến (Chỉ tính khi đã vào Zone)
        is_trigger = False
        body = abs(curr['close'] - curr['open'])
        
        if htf_trend == "UP":
            # Pinbar hoặc Engulfing Tăng
            lower_wick = min(curr['open'], curr['close']) - curr['low']
            is_pinbar = lower_wick > body * 1.5
            is_engulfing = (curr['close'] > curr['open']) and (curr['close'] > df.iloc[-3]['high'])
            if is_pinbar or is_engulfing: is_trigger = True
            
        elif htf_trend == "DOWN":
            # Pinbar hoặc Engulfing Giảm
            upper_wick = curr['high'] - max(curr['open'], curr['close'])
            is_pinbar = upper_wick > body * 1.5
            is_engulfing = (curr['close'] < curr['open']) and (curr['close'] < df.iloc[-3]['low'])
            if is_pinbar or is_engulfing: is_trigger = True
            
        if is_trigger:
            score += 1
            factors.append("Candle Trigger 🔥")

    # --- IN KẾT QUẢ ---
    
    # Status hiển thị
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
    # Chỉ báo lệnh nếu Score >= 2 (Nới lỏng theo yêu cầu)
    # Nếu Score = 2: Cảnh báo (Weak)
    # Nếu Score >= 3: Tín hiệu Mạnh (Strong)
    
    if in_zone and score >= 2:
        signal_type = "BUY" if htf_trend == "UP" else "SELL"
        strength = "STRONG 🔥" if score >= 3 else "MODERATE ⚠️"
        
        risk = abs(zone_entry - zone_sl)
        tp = zone_entry + (risk * 3) if signal_type == "BUY" else zone_entry - (risk * 3)
        rr = abs(tp - zone_entry) / risk if risk > 0 else 0
        
        # In ra các yếu tố (Confluence)
        reasons_str = "\n   + ".join(factors)
        
        msg = (
            f"💎 *SMC PRO SIGNAL ({strength})*\n"
            f"Symbol: {symbol} ({tf})\n"
            f"Score: *{score}/4* ✅\n"
            f"-----------------\n"
            f"Signal: *{signal_type}*\n"
            f"Entry Zone: `{zone_entry}`\n"
            f"Stoploss: `{zone_sl}`\n"
            f"TP (Planned): `{tp}`\n"
            f"R:R: `1:{rr:.2f}`\n"
            f"-----------------\n"
            f"🔍 *Confluences:*\n   + {reasons_str}"
        )
        print(f"\n🚀 SIGNAL FOUND: {symbol} ({score} pts)")
        send_telegram(msg)

# ==========================================
# --- 5. HÀM GỬI TIN & MAIN ---
# ==========================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
        except: pass

if __name__ == "__main__":
    print(f"\n--- BOT SMC 9.5 (SCORING SYSTEM) ---")
    print("Criteria: Trend(1) + OB(1) + FVG(1) + Trigger(1)")
    print("Signal Condition: Score >= 2\n")
    
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_with_scoring(symbol, tf)
            time.sleep(1)
