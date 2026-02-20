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
SL_ATR_MULTIPLIER = 0.8  # Tăng buff SL từ 0.5 lên 0.8 ATR để an toàn hơn
ENTRY_TOLERANCE = 0.5    # Khoảng sai số để khớp lệnh Limit (0.5 ATR)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'ĐIỀN_TOKEN_CỦA_BẠN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'ĐIỀN_CHAT_ID_CỦA_BẠN')

CHAT_IDS = [CHAT_ID] if CHAT_ID and CHAT_ID != 'ĐIỀN_CHAT_ID_CỦA_BẠN' else []

exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# BỘ NHỚ CHỐNG SPAM: Ghi nhớ các OB đã báo tín hiệu
ALERTED_OBS = {pair: {tf: {'buy': 0, 'sell': 0} for tf in MTF_MAPPING.keys()} for pair in PAIRS}

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
    if idx + 2 >= len(df): return False
    if direction == "UP":
        candle_1_high = df['high'].iloc[idx]
        candle_3_low = df['low'].iloc[idx + 2]
        return candle_3_low > candle_1_high 
    elif direction == "DOWN":
        candle_1_low = df['low'].iloc[idx]
        candle_3_high = df['high'].iloc[idx + 2]
        return candle_3_high < candle_1_low 
    return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=250)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Khử ảo MTF: Dùng nến quá khứ [-2] để không nhìn trộm tương lai
        ema_200 = calculate_ema(df['close'], 200).iloc[-2]
        df['atr'] = calculate_atr(df, length=14)
        htf_atr = df['atr'].iloc[-2]
        past_price = df['close'].iloc[-2]
        
        if abs(past_price - ema_200) < htf_atr:
            return "SIDEWAY"
        return "UP" if past_price > ema_200 else "DOWN"
    except: return "SIDEWAY"

# ==========================================
# --- 3. LOGIC TÌM OB + FVG (KHÔNG LỌC SWEEP) ---
# ==========================================

def find_quality_zone(df, trend, current_atr):
    zone_price, zone_sl = 0, 0
    has_fvg = False

    fractal_lows = df[df['is_fractal_low'] == True]
    fractal_highs = df[df['is_fractal_high'] == True]

    if trend == "UP":
        if fractal_lows.empty: return 0, 0, False
        last_low_idx = fractal_lows.index[-1]

        subset = df.iloc[max(0, last_low_idx-5):min(len(df), last_low_idx+3)]
        red_candles = subset[subset['close'] < subset['open']]

        if not red_candles.empty:
            best_ob = red_candles.loc[red_candles['low'].idxmin()]
            ob_idx = df.index.get_loc(best_ob.name)
            zone_price = (best_ob['high'] + best_ob['low']) / 2
            zone_sl = best_ob['low'] - (current_atr * SL_ATR_MULTIPLIER) # Áp dụng hệ số SL mới
            has_fvg = check_fvg(df, ob_idx, "UP")
        else:
            zone_price = df['low'].iloc[last_low_idx]
            zone_sl = zone_price - (current_atr * SL_ATR_MULTIPLIER)

    elif trend == "DOWN":
        if fractal_highs.empty: return 0, 0, False
        last_high_idx = fractal_highs.index[-1]

        subset = df.iloc[max(0, last_high_idx-5):min(len(df), last_high_idx+3)]
        green_candles = subset[subset['close'] > subset['open']]

        if not green_candles.empty:
            best_ob = green_candles.loc[green_candles['high'].idxmax()]
            ob_idx = df.index.get_loc(best_ob.name)
            zone_price = (best_ob['high'] + best_ob['low']) / 2
            zone_sl = best_ob['high'] + (current_atr * SL_ATR_MULTIPLIER) # Áp dụng hệ số SL mới
            has_fvg = check_fvg(df, ob_idx, "DOWN")
        else:
            zone_price = df['high'].iloc[last_high_idx]
            zone_sl = zone_price + (current_atr * SL_ATR_MULTIPLIER)

    return zone_price, zone_sl, has_fvg

# ==========================================
# --- 4. ENGINE QUÉT LỆNH LIVE ---
# ==========================================

def analyze_with_scoring(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    if not htf: return

    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=200)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    except: return

    df['atr'] = calculate_atr(df, length=14) 
    df = identify_fractals(df)

    current_atr = df['atr'].iloc[-1]
    if pd.isna(current_atr) or current_atr == 0: 
        current_atr = df['close'].iloc[-1] * 0.005 

    zone_entry, zone_sl, has_fvg = find_quality_zone(df, htf_trend, current_atr)
    if zone_entry == 0: return

    live = df.iloc[-1] 
    current_price = live['close']
    
    # Tính điểm
    score = 1 # Có Trend MTF
    factors = [f"MTF Trend {htf_trend}"]
    if has_fvg:
        score += 1
        factors.append("Kèm FVG Imbalance")
        
    # Check Limit Order (Giá quẹt trúng ranh giới)
    in_zone = False
    tolerance = current_atr * ENTRY_TOLERANCE 

    if htf_trend == "UP":
        if live['low'] <= zone_entry + tolerance and live['close'] > zone_sl:
            in_zone = True
    elif htf_trend == "DOWN":
        if live['high'] >= zone_entry - tolerance and live['close'] < zone_sl:
            in_zone = True

    if in_zone:
        score += 1
        factors.append("Giá quẹt trúng Zone (Limit Fill)")

    # --- IN KẾT QUẢ ---
    dist_percent = abs(current_price - zone_entry) / current_price * 100
    status_msg = f"⚡ Chạm LIMIT (Score: {score}/3)" if in_zone else f"Đợi: {zone_entry:.4f} (Cách {dist_percent:.2f}%)"
    print(f"{symbol:<8} ({tf}) | {htf_trend:<4} | Score: {score}/3 | {status_msg}")

    # --- RA QUYẾT ĐỊNH & GỬI TELEGRAM ---
    if in_zone and score >= 2:
        signal_type = "BUY" if htf_trend == "UP" else "SELL"
        risk = abs(zone_entry - zone_sl)
        
        # Tính TP1 (1R - Dời SL về hòa) và TP2 (1.618R)
        if signal_type == "BUY":
            tp1 = zone_entry + risk
            tp2 = zone_entry + (risk * 1.618)
        else:
            tp1 = zone_entry - risk
            tp2 = zone_entry - (risk * 1.618)
            
        # Cơ chế chống Spam (Chỉ gửi 1 lần cho 1 mức giá OB)
        alert_key = 'buy' if signal_type == "BUY" else 'sell'
        if ALERTED_OBS[symbol][tf][alert_key] == zone_entry:
            return # Đã báo OB này rồi thì bỏ qua

        # Ghi nhớ OB vừa báo
        ALERTED_OBS[symbol][tf][alert_key] = zone_entry

        reasons_str = "\n   + ".join(factors)
        msg = (
            f"💎 *SMC PRO (LIMIT ENTRY)* 💎\n"
            f"Cặp: {symbol} ({tf})\n"
            f"Lệnh: *{signal_type} Limit*\n"
            f"-----------------\n"
            f"Entry: `{zone_entry:.4f}`\n"
            f"Stoploss: `{zone_sl:.4f}`\n"
            f"TP1 (1R - Kéo SL): `{tp1:.4f}`\n"
            f"TP2 (1.6R): `{tp2:.4f}`\n"
            f"-----------------\n"
            f"🔍 *Hợp lưu:*\n   + {reasons_str}"
        )
        print(f"\n🚀 ĐÃ BẮN LỆNH {signal_type}: {symbol} ({tf})")
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
        except Exception as e: 
            print(f"Lỗi gửi Tele: {e}")

if __name__ == "__main__":
    print(f"\n--- BOT SMC PYTHON LIVE ---")
    print(f"Cấu hình Buff SL: {SL_ATR_MULTIPLIER} ATR")
    print("Logic: MTF Trend + OB Limit + Không lọc thời gian")
    send_telegram(f"🟢 *Hệ thống SMC Python Live khởi động!*\nBuff SL: {SL_ATR_MULTIPLIER} ATR. Đang quét kèo...")
    
    while True:
        try:
            for symbol in PAIRS:
                for tf in MTF_MAPPING.keys():
                    analyze_with_scoring(symbol, tf)
                    time.sleep(1.5) # Tránh rate limit của MEXC
            
            print("--- Đợi 30s quét lại ---")
            time.sleep(30)
            os.system('cls' if os.name == 'nt' else 'clear') # Xóa terminal cho gọn
            
        except KeyboardInterrupt:
            print("\nĐã tắt Bot.")
            break
        except Exception as e:
            print(f"Lỗi vòng lặp chính: {e}")
            time.sleep(10)
