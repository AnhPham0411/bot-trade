import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
from datetime import datetime, timezone

# ==========================================
# --- 1. CẤU HÌNH (CONFIGURATION) ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAMES = ['15m', '1h', '4h'] 

# Telegram Config
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_IDS = [os.getenv('TELEGRAM_CHAT_ID')] 

# Kết nối sàn MEXC
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

SUMMARY_REPORT = []

# ==========================================
# --- 2. HÀM TÍNH TOÁN INDICATOR ---
# ==========================================

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    """Tính RSI chuẩn"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    loss = loss.replace(0, np.nan) 
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, length=14):
    """Tính ATR đo biến động"""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=length).mean()

def find_swings(df, window=7):
    """Window = 7 để lọc nhiễu tốt hơn"""
    df['swing_high'] = df['high'].rolling(window=window*2+1, center=True).max()
    df['swing_low'] = df['low'].rolling(window=window*2+1, center=True).min()
    df['is_high'] = (df['high'] == df['swing_high'])
    df['is_low'] = (df['low'] == df['swing_low'])
    return df

# ==========================================
# --- 3. LOGIC SMC (MODIFIED: VOL > 1.5) ---
# ==========================================

def check_sniper_setup(df):
    """
    Logic SMC đã điều chỉnh:
    - Volume Filter: > 1.5 lần trung bình (Thay vì 2.0)
    - RR Filter: Sẽ check ở hàm analyze (> 1.5)
    """
    # Lấy nến vừa đóng cửa
    curr = df.iloc[-2] 
    prev = df.iloc[-3]
    
    ema_200 = df['ema_200'].iloc[-2]
    trend_txt = "🟢 UP" if curr['close'] > ema_200 else "🔴 DOWN"
    
    signal = None
    strat_name = ""
    entry = 0
    sl = 0
    tp = 0

    # Chỉ số tại nến đóng cửa
    atr_val = df['atr'].iloc[-2]
    rsi_val = df['rsi'].iloc[-2]
    vol_val = curr['vol']
    avg_vol = df['vol'].rolling(20).mean().iloc[-2]

    # --- 1. GLOBAL FILTER ---
    # Lọc Sideway (ATR quá thấp)
    avg_atr = df['atr'].rolling(50).mean().iloc[-2]
    if atr_val < avg_atr * 0.8: 
        return None, "", 0, 0, 0, trend_txt + " (Low Volatility)"

    # Lọc Volume: GIẢM XUỐNG 1.5 (Theo yêu cầu)
    # Chỉ cần Volume lớn hơn 1.5 lần trung bình 20 phiên
    if vol_val < avg_vol * 1.5:
        return None, "", 0, 0, 0, trend_txt + " (Weak Vol)"

    # --- SETUP BUY (LONG) ---
    if curr['close'] > ema_200 and rsi_val < 55: 
        recent_data = df.iloc[-60:-2] 
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            if curr['low'] <= fibo_05:
                # Pinbar Bullish
                body = abs(curr['close'] - curr['open'])
                lower_wick = min(curr['close'], curr['open']) - curr['low']
                is_pinbar = lower_wick > (body * 2)
                
                # Engulfing Bullish
                is_engulfing = (curr['close'] > prev['open']) and (curr['open'] < prev['close'])
                
                # Momentum: Nến Xanh
                is_green_candle = curr['close'] > curr['open']

                if (is_pinbar or is_engulfing) and is_green_candle:
                    signal = "BUY"
                    strat_name = "Pullback Discount"
                    entry = curr['close']
                    sl = min(curr['low'], last_low) - (atr_val * 0.5) 
                    tp = last_high

    # --- SETUP SELL (SHORT) ---
    elif curr['close'] < ema_200 and rsi_val > 45: 
        recent_data = df.iloc[-60:-2]
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            if curr['high'] >= fibo_05:
                # Pinbar Bearish
                body = abs(curr['close'] - curr['open'])
                upper_wick = curr['high'] - max(curr['close'], curr['open'])
                is_pinbar = upper_wick > (body * 2)
                
                # Engulfing Bearish
                is_engulfing = (curr['close'] < prev['open']) and (curr['open'] > prev['close'])
                
                # Momentum: Nến Đỏ
                is_red_candle = curr['close'] < curr['open']

                if (is_pinbar or is_engulfing) and is_red_candle:
                    signal = "SELL"
                    strat_name = "Pullback Premium"
                    entry = curr['close']
                    sl = max(curr['high'], last_high) + (atr_val * 0.5)
                    tp = last_low

    return signal, strat_name, entry, sl, tp, trend_txt

# ==========================================
# --- 4. GỬI TELEGRAM & XỬ LÝ ---
# ==========================================

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    unique_ids = list(set(filter(None, CHAT_IDS)))
    for chat_id in unique_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=5
            )
        except Exception as e:
            print(f"❌ Lỗi gửi tele: {e}")

def analyze(symbol, tf):
    print(f"🔎 Scanning: {symbol} ({tf})...", end="\r")
    
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    except Exception as e:
        print(f"❌ Error getting data for {symbol}: {e}")
        SUMMARY_REPORT.append(f"⚠️ {symbol} ({tf}): Lỗi Data")
        return

    # Tính Indicators
    df['ema_200'] = calculate_ema(df['close'], 200)
    df['rsi'] = calculate_rsi(df['close'], 14) 
    df['atr'] = calculate_atr(df, 14)          
    df = find_swings(df, window=7)             

    # Chạy Logic
    signal, strat, entry, sl, tp, trend = check_sniper_setup(df)
    current_price = df.iloc[-1]['close']

    # Xử lý Tín hiệu
    if signal:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        # Tính Volume Ratio để hiển thị
        cur_vol = df.iloc[-2]['vol']
        avg_vol = df['vol'].rolling(20).mean().iloc[-2]
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0

        # --- ĐIỀU KIỆN RR: GIẢM XUỐNG >= 1.5 ---
        if rr >= 1.5:
            icon = "🟢 LONG" if signal == "BUY" else "🔴 SHORT"
            
            # Tin nhắn hiển thị đầy đủ Volume và RR
            msg = (
                f"*{icon}: {symbol} ({tf})*\n"
                f"-------------------\n"
                f"Strategy: _{strat}_\n"
                f"Entry: `{entry}`\n"
                f"Stoploss: `{sl:.4f}`\n"
                f"Take Profit: `{tp:.4f}`\n"
                f"Risk/Reward: `1:{rr:.2f}` ✅\n"
                f"Volume: `{vol_ratio:.2f}x` Avg 📊\n"
                f"Trend: {trend}\n"
            )
            print(f"\n🔥 SIGNAL: {symbol} (Vol: {vol_ratio:.1f}x, RR: {rr:.1f})")
            send_telegram(msg)
            SUMMARY_REPORT.append(f"🔥 *{symbol} ({tf})*: {signal} (RR 1:{rr:.1f})")
            return

    # Nếu không có tín hiệu
    SUMMARY_REPORT.append(f"{trend} {symbol} ({tf}) | P: {current_price}")

# ==========================================
# --- 5. MAIN LOOP ---
# ==========================================

if __name__ == "__main__":
    start_time = datetime.now().strftime('%H:%M')
    print(f"\n--- BOT STARTED AT {start_time} ---")
    
    SUMMARY_REPORT = []
    
    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1) 
            
    # Gửi báo cáo Alive
    if SUMMARY_REPORT:
        report_msg = f"🤖 *BOT STATUS ({start_time})*\n"
        report_msg += "-----------------------------\n"
        report_msg += "\n".join(SUMMARY_REPORT[:15]) 
        
        print("\nSending Summary Report...")
        send_telegram(report_msg)
        
    print("\n🏁 Done.")
