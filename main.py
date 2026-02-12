import ccxt
import pandas as pd
import numpy as np
import os
import requests
import time
from datetime import datetime, timezone

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT']
TIMEFRAMES = ['1h', '4h'] 

# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
env_chat_id = os.getenv('TELEGRAM_CHAT_ID')

CHAT_IDS = ['-5103508011'] 
if env_chat_id:
    CHAT_IDS.append(env_chat_id)

# Sàn MEXC
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# ==========================================
# PHẦN 1: HÀM TÍNH TOÁN (KHÔNG CẦN PANDAS-TA)
# ==========================================

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=length-1, min_periods=length).mean()
    avg_loss = loss.ewm(com=length-1, min_periods=length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, length=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=length).mean()

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    hist = macd_line - signal_line
    return hist

def calculate_supertrend(df, length=10, multiplier=3):
    atr = calculate_atr(df, length)
    hl2 = (df['high'] + df['low']) / 2
    upper_basic = hl2 + (multiplier * atr)
    lower_basic = hl2 - (multiplier * atr)
    
    upper_band = [0.0] * len(df)
    lower_band = [0.0] * len(df)
    supertrend = [0.0] * len(df)
    direction = [1] * len(df) # 1: Tăng, -1: Giảm
    
    # Logic Supertrend
    close = df['close'].values
    
    for i in range(1, len(df)):
        # Upper Band
        if upper_basic.iloc[i] < upper_band[i-1] or close[i-1] > upper_band[i-1]:
            upper_band[i] = upper_basic.iloc[i]
        else:
            upper_band[i] = upper_band[i-1]
            
        # Lower Band
        if lower_basic.iloc[i] > lower_band[i-1] or close[i-1] < lower_band[i-1]:
            lower_band[i] = lower_basic.iloc[i]
        else:
            lower_band[i] = lower_band[i-1]
            
        # Direction
        if close[i] > upper_band[i-1]:
            direction[i] = 1
        elif close[i] < lower_band[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
            
        # Supertrend Value
        if direction[i] == 1:
            supertrend[i] = lower_band[i]
        else:
            supertrend[i] = upper_band[i]
            
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)

# ==========================================
# PHẦN 2: LOGIC CHÍNH
# ==========================================

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=5
            )
        except Exception as e:
            print(f"❌ Lỗi gửi tele: {e}")

def get_data(symbol, tf):
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Lỗi data {symbol}: {e}")
        return None

def analyze(symbol, tf):
    print(f"🔎 Scanning: {symbol} ({tf})...", end="\r")

    current_hour_utc = datetime.now(timezone.utc).hour
    if tf == '4h' and current_hour_utc % 4 != 0:
        if datetime.now(timezone.utc).minute > 5: return 

    df = get_data(symbol, tf)
    if df is None: return

    # --- TÍNH TOÁN INDICATOR (Dùng hàm tự viết) ---
    df['ema_200'] = calculate_ema(df['close'], 200)
    df['rsi'] = calculate_rsi(df['close'], 14)
    df['atr'] = calculate_atr(df, 14)
    df['macd_hist'] = calculate_macd(df['close'], 12, 26, 9)
    
    st_val, st_dir = calculate_supertrend(df, 10, 3)
    df['st_val'] = st_val
    df['st_dir'] = st_dir

    # Nến tín hiệu
    c = df.iloc[-2] 
    p = df.iloc[-3] 

    signal = None
    sl_price = 0.0
    tp_price = 0.0
    
    # --- LỌC NẾN ---
    body_size = abs(c['close'] - c['open'])
    full_size = c['high'] - c['low']
    is_strong_candle = body_size > (full_size * 0.5)

    # --- LOGIC MUA/BÁN ---
    # LONG
    if c['close'] > c['ema_200']:
        if c['st_dir'] == 1 and p['st_dir'] == -1: # Đổi màu
            if c['macd_hist'] > 0:
                if is_strong_candle and c['rsi'] < 70:
                    signal = "BUY"
                    sl_price = c['st_val']
                    risk = c['close'] - sl_price
                    min_reward = c['atr'] * 2
                    tp_price = c['close'] + max(risk * 1.5, min_reward)

    # SHORT
    elif c['close'] < c['ema_200']:
        if c['st_dir'] == -1 and p['st_dir'] == 1: # Đổi màu
            if c['macd_hist'] < 0:
                if is_strong_candle and c['rsi'] > 30:
                    signal = "SELL"
                    sl_price = c['st_val']
                    risk = sl_price - c['close']
                    min_reward = c['atr'] * 2
                    tp_price = c['close'] - max(risk * 1.5, min_reward)

    # --- GỬI TIN NHẮN ---
    if signal:
        entry = c['close']
        rr = abs(tp_price - entry) / abs(entry - sl_price) if abs(entry - sl_price) > 0 else 0
        
        icon = "🚀 LONG MẠNH" if signal == "BUY" else "📉 SHORT MẠNH"
        msg = (
            f"{icon} #{symbol} ({tf})\n"
            f"Entry: {entry:.4f}\n"
            f"SL: {sl_price:.4f}\n"
            f"TP: {tp_price:.4f}\n"
            f"R:R: {rr:.2f}\n"
            f"--- No-Lib Mode ---"
        )
        print(f"\n🔥 {msg}")
        send_telegram(msg)
    else:
        trend = "Tăng" if c['close'] > c['ema_200'] else "Giảm"
        st = "Xanh" if c['st_dir'] == 1 else "Đỏ"
        print(f"✅ {symbol} {tf}: Wait... (Price:{c['close']:.2f} | Trend:{trend} | ST:{st})    ")

if __name__ == "__main__":
    print(f"\n--- BOT START (NO-LIB): {datetime.now().strftime('%H:%M')} ---")
    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1)
    print("\n🏁 Done.")
