import ccxt
import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    # Nếu không tìm thấy bản gốc, thử import bản Classic
    import pandas_ta_classic as ta
import os
import requests
from datetime import datetime

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAME = '4h'
TREND_TF = '1d'
LIMIT = 200

# Lấy Key từ biến môi trường (Bảo mật tuyệt đối trên GitHub)
API_KEY = os.getenv('BINANCE_API_KEY')
SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
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
        bars = exchange.fetch_ohlcv(symbol, tf, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except:
        return None

def analyze(symbol):
    df_d1 = fetch_data(symbol, TREND_TF)
    df_h4 = fetch_data(symbol, TIMEFRAME)
    
    if df_d1 is None or df_h4 is None: return

    # 1. Trend D1
    df_d1['ema50'] = df_d1.ta.ema(length=50)
    trend = 1 if df_d1.iloc[-2]['close'] > df_d1.iloc[-2]['ema50'] else -1
    
    # 2. Entry H4
    df_h4['ema20'] = df_h4.ta.ema(length=20)
    df_h4['atr'] = df_h4.ta.atr(length=14)
    df_h4['swing_low'] = df_h4['low'].rolling(10).min()
    df_h4['swing_high'] = df_h4['high'].rolling(10).max()
    
    curr = df_h4.iloc[-1]
    prev = df_h4.iloc[-2]
    
    signal = None
    sl = 0.0
    
    if trend == 1: # UP
        if curr['close'] > curr['ema20'] and curr['close'] > curr['open']:
            if curr['close'] > prev['high']:
                signal = "BUY"
                sl = prev['swing_low'] - (curr['atr'] * 0.5)
                
    elif trend == -1: # DOWN
        if curr['close'] < curr['ema20'] and curr['close'] < curr['open']:
            if curr['close'] < prev['low']:
                signal = "SELL"
                sl = prev['swing_high'] + (curr['atr'] * 0.5)
                
    if signal:
        tp = curr['close'] + abs(curr['close'] - sl)*2 if signal == "BUY" else curr['close'] - abs(curr['close'] - sl)*2
        msg = (f"🚀 TÍN HIỆU H4: {symbol} - {signal}\n"
               f"Giá: {curr['close']}\n"
               f"SL: {sl:.2f}\n"
               f"TP: {tp:.2f}")
        print(msg)
        send_telegram(msg)
    else:
        print(f"{symbol}: Không có tín hiệu.")

if __name__ == "__main__":
    print(f"🕒 Chạy quét lúc: {datetime.now()}")
    for symbol in PAIRS:
        analyze(symbol)
    print("✅ Hoàn tất.")
