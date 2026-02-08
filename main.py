import ccxt
import pandas as pd
import pandas_ta as ta
import os
import requests
import time
from datetime import datetime

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAME = '4h'
TREND_TF = '1d'
LIMIT = 200

# Lấy Token Telegram (API Key sàn KHÔNG CẦN nữa)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Cấu hình Binance chế độ Public (Không cần Key)
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'} 
})

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Chưa cấu hình Telegram")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

def fetch_data(symbol, tf):
    try:
        # Lấy dữ liệu nến
        bars = exchange.fetch_ohlcv(symbol, tf, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        # In lỗi ra để biết tại sao (ví dụ: Network Error, Rate Limit...)
        print(f"❌ Lỗi tải dữ liệu {symbol} ({tf}): {e}")
        return None

def analyze(symbol):
    # 1. Lấy dữ liệu
    df_d1 = fetch_data(symbol, TREND_TF)
    df_h4 = fetch_data(symbol, TIMEFRAME)
    
    if df_d1 is None or df_h4 is None:
        return

    try:
        # 2. Tính toán Indicator
        # Trend D1
        df_d1['ema50'] = df_d1.ta.ema(length=50)
        
        # Entry H4
        df_h4['ema20'] = df_h4.ta.ema(length=20)
        df_h4['atr'] = df_h4.ta.atr(length=14)
        df_h4['swing_low'] = df_h4['low'].rolling(10).min()
        df_h4['swing_high'] = df_h4['high'].rolling(10).max()
        
        # Lấy nến hiện tại và nến trước đó
        curr = df_h4.iloc[-1]
        prev = df_h4.iloc[-2]
        d1_last = df_d1.iloc[-2] # Nến D1 hôm qua

        # Xác định xu hướng D1
        trend = 1 if d1_last['close'] > d1_last['ema50'] else -1
        
        signal = None
        sl = 0.0
        
        # Logic Vào Lệnh
        if trend == 1: # UPTREND
            if curr['close'] > curr['ema20'] and curr['close'] > curr['open']:
                if curr['close'] > prev['high']:
                    signal = "BUY"
                    sl = prev['swing_low'] - (curr['atr'] * 0.5)
                    
        elif trend == -1: # DOWN
            if curr['close'] < curr['ema20'] and curr['close'] < curr['open']:
                if curr['close'] < prev['low']:
                    signal = "SELL"
                    sl = prev['swing_high'] + (curr['atr'] * 0.5)
                    
        # Gửi thông báo
        if signal:
            risk_dist = abs(curr['close'] - sl)
            # TP = 2R (Lợi nhuận gấp đôi rủi ro)
            tp = curr['close'] + (risk_dist * 2) if signal == "BUY" else curr['close'] - (risk_dist * 2)
            
            msg = (f"🚀 TÍN HIỆU H4: {symbol} - {signal}\n"
                   f"Giá: {curr['close']}\n"
                   f"SL: {sl:.2f}\n"
                   f"TP: {tp:.2f}")
            print(msg)
            send_telegram(msg)
        else:
            print(f"{symbol}: Không có tín hiệu (Trend D1: {'UP' if trend==1 else 'DOWN'}).")

    except Exception as e:
        print(f"⚠️ Lỗi tính toán {symbol}: {e}")

if __name__ == "__main__":
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🕒 Chạy quét lúc: {now_str}")
    
    for symbol in PAIRS:
        analyze(symbol)
        time.sleep(1) # Nghỉ 1 giây để tránh spam sàn
        
    print("✅ Hoàn tất.")
