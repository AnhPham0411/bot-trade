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

# Telegram Config (Lấy từ biến môi trường Github Actions)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_IDS = [os.getenv('TELEGRAM_CHAT_ID')] 

# Kết nối sàn MEXC (Hoặc Binance/Bybit tùy ý)
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# Biến toàn cục lưu trữ báo cáo cuối giờ
SUMMARY_REPORT = []

# ==========================================
# --- 2. HÀM TÍNH TOÁN INDICATOR ---
# ==========================================

def calculate_ema(series, length):
    """Tính đường trung bình lũy thừa (EMA)"""
    return series.ewm(span=length, adjust=False).mean()

def calculate_atr(df, length=14):
    """Tính độ biến động trung bình (ATR) để đặt SL/TP"""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=length).mean()

def find_swings(df, window=5):
    """Tìm đỉnh (Swing High) và đáy (Swing Low)"""
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
    Logic tìm điểm vào lệnh dựa trên nến ĐÃ ĐÓNG CỬA (iloc[-2]).
    Điều này giúp bot chạy ổn định trên Github Actions, không bị vẽ lại (repaint).
    """
    # Lấy nến vừa đóng cửa (Confirmed Candle)
    curr = df.iloc[-2] 
    prev = df.iloc[-3]
    
    # Xác định xu hướng hiện tại
    ema_200 = df['ema_200'].iloc[-2]
    trend_txt = "🟢 UP" if curr['close'] > ema_200 else "🔴 DOWN"
    
    signal = None
    strat_name = ""
    entry = 0
    sl = 0
    tp = 0

    # --- SETUP BUY (LONG) ---
    # Điều kiện: Giá nằm trên EMA 200 (Uptrend)
    if curr['close'] > ema_200:
        # Tìm vùng giá (Swing) gần nhất trong 60 nến quá khứ
        recent_data = df.iloc[-60:-2] 
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        # Nếu cấu trúc thị trường rõ ràng
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            # Vùng Discount (Giá rẻ): Dưới mức 50% của đợt tăng giá
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            # Nếu giá thấp nhất của nến tín hiệu chạm vào vùng Discount
            if curr['low'] <= fibo_05:
                # Trigger 1: Pinbar Bullish (Rút chân dưới dài)
                body = abs(curr['close'] - curr['open'])
                lower_wick = min(curr['close'], curr['open']) - curr['low']
                is_pinbar = lower_wick > (body * 2)
                
                # Trigger 2: Engulfing Bullish (Nhấn chìm tăng)
                is_engulfing = (curr['close'] > prev['open']) and (curr['open'] < prev['close'])
                
                if is_pinbar or is_engulfing:
                    signal = "BUY"
                    strat_name = "Pullback Discount"
                    entry = curr['close']
                    sl = min(curr['low'], last_low) * 0.995 # SL dưới đáy nến hoặc đáy cũ 0.5%
                    tp = last_high # TP về đỉnh cũ

    # --- SETUP SELL (SHORT) ---
    # Điều kiện: Giá nằm dưới EMA 200 (Downtrend)
    elif curr['close'] < ema_200:
        recent_data = df.iloc[-60:-2]
        last_low = recent_data[recent_data['is_low'] == True]['low'].min()
        last_high = recent_data[recent_data['is_high'] == True]['high'].max()
        
        if not pd.isna(last_low) and not pd.isna(last_high) and last_high > last_low:
            # Vùng Premium (Giá đắt): Trên mức 50% của đợt giảm giá
            fibo_05 = last_low + 0.5 * (last_high - last_low)
            
            if curr['high'] >= fibo_05:
                # Trigger 1: Pinbar Bearish (Rút chân trên dài)
                body = abs(curr['close'] - curr['open'])
                upper_wick = curr['high'] - max(curr['close'], curr['open'])
                is_pinbar = upper_wick > (body * 2)
                
                # Trigger 2: Engulfing Bearish (Nhấn chìm giảm)
                is_engulfing = (curr['close'] < prev['open']) and (curr['open'] > prev['close'])
                
                if is_pinbar or is_engulfing:
                    signal = "SELL"
                    strat_name = "Pullback Premium"
                    entry = curr['close']
                    sl = max(curr['high'], last_high) * 1.005 # SL trên đỉnh nến hoặc đỉnh cũ 0.5%
                    tp = last_low # TP về đáy cũ

    return signal, strat_name, entry, sl, tp, trend_txt

# ==========================================
# --- 4. HÀM GỬI TELEGRAM & XỬ LÝ ---
# ==========================================

def send_telegram(msg):
    """Gửi tin nhắn đến Telegram"""
    if not TELEGRAM_TOKEN: return
    # Lọc ID trùng lặp và rỗng
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
    """Phân tích 1 cặp tiền trên 1 khung thời gian"""
    print(f"🔎 Scanning: {symbol} ({tf})...", end="\r")
    
    try:
        # Lấy 500 nến để tính EMA và tìm đỉnh đáy chuẩn
        bars = exchange.fetch_ohlcv(symbol, tf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    except Exception as e:
        print(f"❌ Error getting data for {symbol}: {e}")
        SUMMARY_REPORT.append(f"⚠️ {symbol} ({tf}): Lỗi Data")
        return

    # Tính toán
    df['ema_200'] = calculate_ema(df['close'], 200)
    df = find_swings(df, window=5)

    # Chạy Logic
    signal, strat, entry, sl, tp, trend = check_sniper_setup(df)
    current_price = df.iloc[-1]['close'] # Giá realtime (để báo cáo)

    # 1. Nếu có tín hiệu MUA/BÁN -> Gửi ngay lập tức
    if signal:
        # Tính tỷ lệ Risk:Reward
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        # Chỉ báo kèo nếu R:R >= 1.5 (Lọc kèo rác)
        if rr >= 1.5:
            icon = "🚀 LONG MỚI" if signal == "BUY" else "🛑 SHORT MỚI"
            msg = (
                f"*{icon}: {symbol} ({tf})*\n"
                f"-------------------\n"
                f"Strategy: _{strat}_\n"
                f"Entry: `{entry}`\n"
                f"Stoploss: `{sl:.4f}`\n"
                f"Take Profit: `{tp:.4f}`\n"
                f"R:R: `1:{rr:.1f}`\n"
                f"Trend: {trend}\n"
                f"_Check chart trước khi vào lệnh!_"
            )
            print(f"\n🔥 FOUND SIGNAL: {symbol}")
            send_telegram(msg)
            # Thêm vào báo cáo tổng kết là có kèo
            SUMMARY_REPORT.append(f"🔥 *{symbol} ({tf})*: {signal} Signal!")
            return

    # 2. Nếu không có tín hiệu -> Lưu trạng thái để báo cáo cuối cùng
    # Format: [Icon Trend] Coin (Khung): Giá
    SUMMARY_REPORT.append(f"{trend} {symbol} ({tf}) | Price: {current_price}")

# ==========================================
# --- 5. MAIN LOOP ---
# ==========================================

if __name__ == "__main__":
    start_time = datetime.now().strftime('%H:%M')
    print(f"\n--- BOT STARTED AT {start_time} ---")
    
    SUMMARY_REPORT = [] # Reset báo cáo
    
    # Chạy vòng lặp qua từng coin và từng khung
    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1) # Nghỉ 1s để tránh spam API sàn
            
    # --- GỬI BÁO CÁO TỔNG KẾT (ALIVE MONITOR) ---
    # Bot sẽ gửi 1 tin nhắn duy nhất chứa danh sách trend của tất cả coin
    if SUMMARY_REPORT:
        report_msg = f"🤖 *STATUS REPORT ({start_time})*\n"
        report_msg += "-----------------------------\n"
        report_msg += "\n".join(SUMMARY_REPORT)
        
        print("\nSending Summary Report...")
        send_telegram(report_msg)
        
    print("\n🏁 Done.")
