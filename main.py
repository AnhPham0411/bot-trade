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
    """Tính RSI chuẩn để lọc Overbought/Oversold"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    # Tránh chia cho 0
    loss = loss.replace(0, np.nan) 
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, length=14):
    """Tính ATR để đo độ biến động"""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=length).mean()

def find_swings(df, window=7):
    """
    NÂNG CẤP: Window = 7 (thay vì 5)
    Giúp xác định cấu trúc Swing High/Low rõ ràng hơn, lọc nhiễu trong Crypto.
    """
    df['swing_high'] = df['high'].rolling(window=window*2+1, center=True).max()
    df['swing_low'] = df['low'].rolling(window=window*2+1, center=True).min()
    df['is_high'] = (df['high'] == df['swing_high'])
    df['is_low'] = (df['low'] == df['swing_low'])
    return df

# ==========================================
# --- 3. LOGIC SMC "SNIPER" (CORE) ---
# ==========================================

def check_sniper_setup(df):
    """
    Logic SMC Nâng cao:
    1. Trend: EMA 200
    2. Structure: Swing (Window 7)
    3. Area: Fibo 50% (Premium/Discount)
    4. Filter: RSI, Volume > 2x, ATR (Volatility), Momentum Candle
    """
    # Lấy nến vừa đóng cửa (Confirmed Candle)
    curr = df.iloc[-2] 
    prev = df.iloc[-3]
    
    # Xác định xu hướng
    ema_200 = df['ema_200'].iloc[-2]
    trend_txt = "🟢 UP" if curr['close'] > ema_200 else "🔴 DOWN"
    
    signal = None
    strat_name = ""
    entry = 0
    sl = 0
    tp = 0

    # Lấy giá trị Indicator tại nến đóng cửa
    atr_val = df['atr'].iloc[-2]
    rsi_val = df['rsi'].iloc[-2]
    vol_val = curr['vol']
    avg_vol = df['vol'].rolling(20).mean().iloc[-2]

    # --- GLOBAL FILTER (BỘ LỌC CHUNG) ---
    # 1. Lọc Sideway: Nếu ATR quá thấp (thị trường ngủ đông) -> Bỏ qua
    # So sánh ATR hiện tại với ATR trung bình 50 cây nến trước
    avg_atr = df['atr'].rolling(50).mean().iloc[-2]
    if atr_val < avg_atr * 0.8: # Nới lỏng chút so với 0.5 để không bị miss quá nhiều
        return None, "", 0, 0, 0, trend_txt + " (Low Volatility)"

    # 2. Lọc Volume: Yêu cầu Volume nến tín hiệu phải đột biến (Gấp 2 lần trung bình)
    if vol_val < avg_vol * 2.0:
        return None, "", 0, 0, 0, trend_txt + " (Weak Vol)"

    # --- SETUP BUY (LONG) ---
    # Trend Tăng + RSI < 50 (Giá đã hồi sâu về vùng Discount)
    if curr['close'] > ema_200 and rsi_val < 55: # Cho phép RSI < 55 để bắt sớm hơn chút
        
        # Tìm cấu trúc Swing (60 nến)
        recent_data = df.iloc[-60:-2] 
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            # Fibo Discount Check
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            if curr['low'] <= fibo_05:
                # Trigger 1: Pinbar Bullish (Rút chân dưới)
                body = abs(curr['close'] - curr['open'])
                lower_wick = min(curr['close'], curr['open']) - curr['low']
                is_pinbar = lower_wick > (body * 2)
                
                # Trigger 2: Engulfing Bullish
                is_engulfing = (curr['close'] > prev['open']) and (curr['open'] < prev['close'])
                
                # MOMENTUM CHECK: Nến tín hiệu bắt buộc phải là NẾN XANH (Close > Open)
                is_green_candle = curr['close'] > curr['open']

                if (is_pinbar or is_engulfing) and is_green_candle:
                    signal = "BUY"
                    strat_name = "Sniper Discount (RSI+Vol)"
                    entry = curr['close']
                    # SL đặt dưới đáy cũ hoặc râu nến + 1 chút buffer bằng ATR
                    sl = min(curr['low'], last_low) - (atr_val * 0.5) 
                    tp = last_high # Target đỉnh cũ

    # --- SETUP SELL (SHORT) ---
    # Trend Giảm + RSI > 50 (Giá đã hồi lên vùng Premium)
    elif curr['close'] < ema_200 and rsi_val > 45: # Cho phép RSI > 45
        
        recent_data = df.iloc[-60:-2]
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            # Fibo Premium Check
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            if curr['high'] >= fibo_05:
                # Trigger 1: Pinbar Bearish
                body = abs(curr['close'] - curr['open'])
                upper_wick = curr['high'] - max(curr['close'], curr['open'])
                is_pinbar = upper_wick > (body * 2)
                
                # Trigger 2: Engulfing Bearish
                is_engulfing = (curr['close'] < prev['open']) and (curr['open'] > prev['close'])
                
                # MOMENTUM CHECK: Nến tín hiệu bắt buộc phải là NẾN ĐỎ
                is_red_candle = curr['close'] < curr['open']

                if (is_pinbar or is_engulfing) and is_red_candle:
                    signal = "SELL"
                    strat_name = "Sniper Premium (RSI+Vol)"
                    entry = curr['close']
                    # SL đặt trên đỉnh cũ hoặc râu nến + buffer ATR
                    sl = max(curr['high'], last_high) + (atr_val * 0.5)
                    tp = last_low # Target đáy cũ

    return signal, strat_name, entry, sl, tp, trend_txt

# ==========================================
# --- 4. HÀM GỬI TELEGRAM & XỬ LÝ ---
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
        # Tăng limit lên 500 để đủ dữ liệu tính RSI, EMA, Swing chuẩn
        bars = exchange.fetch_ohlcv(symbol, tf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    except Exception as e:
        print(f"❌ Error getting data for {symbol}: {e}")
        SUMMARY_REPORT.append(f"⚠️ {symbol} ({tf}): Lỗi Data")
        return

    # Tính toán Indicators
    df['ema_200'] = calculate_ema(df['close'], 200)
    df['rsi'] = calculate_rsi(df['close'], 14) # Thêm RSI
    df['atr'] = calculate_atr(df, 14)          # Thêm ATR
    df = find_swings(df, window=7)             # Window=7

    # Chạy Logic Sniper
    signal, strat, entry, sl, tp, trend = check_sniper_setup(df)
    current_price = df.iloc[-1]['close']

    # Xử lý Tín hiệu
    if signal:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        # NÂNG CẤP: Chỉ báo kèo nếu RR >= 2.0 (Kèo chất lượng cao)
        if rr >= 2.0:
            icon = "🎯 SNIPER LONG" if signal == "BUY" else "🎯 SNIPER SHORT"
            
            # Format tin nhắn gọn gàng, chuyên nghiệp
            msg = (
                f"*{icon}: {symbol} ({tf})*\n"
                f"-------------------\n"
                f"Strategy: _{strat}_\n"
                f"Entry: `{entry}`\n"
                f"Stoploss: `{sl:.4f}`\n"
                f"Take Profit: `{tp:.4f}`\n"
                f"Risk/Reward: `1:{rr:.2f}`\n"
                f"Trend: {trend}\n"
                f"Volume: 2x Avg ✅ | RSI Filter ✅\n"
            )
            print(f"\n🔥 FOUND SIGNAL: {symbol}")
            send_telegram(msg)
            SUMMARY_REPORT.append(f"🔥 *{symbol} ({tf})*: {signal} (RR 1:{rr:.1f})")
            return

    # Nếu không có tín hiệu, lưu report
    SUMMARY_REPORT.append(f"{trend} {symbol} ({tf}) | P: {current_price}")

# ==========================================
# --- 5. MAIN LOOP ---
# ==========================================

if __name__ == "__main__":
    start_time = datetime.now().strftime('%H:%M')
    print(f"\n--- SNIPER BOT STARTED AT {start_time} ---")
    
    SUMMARY_REPORT = []
    
    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1) 
            
    # Gửi báo cáo trạng thái Alive
    if SUMMARY_REPORT:
        report_msg = f"🤖 *BOT STATUS ({start_time})*\n"
        report_msg += "-----------------------------\n"
        # Chỉ lấy 6 dòng đầu tiên để tránh spam quá dài nếu list nhiều coin
        report_msg += "\n".join(SUMMARY_REPORT[:15]) 
        
        print("\nSending Summary Report...")
        send_telegram(report_msg)
        
    print("\n🏁 Done.")
