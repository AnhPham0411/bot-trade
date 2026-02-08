import ccxt
import pandas as pd
import os
import requests
import time
from datetime import datetime

# --- KHAI BÁO THƯ VIỆN PANDAS-TA ---
try:
    import pandas_ta as ta
    print("✅ Đã load thư viện: pandas_ta (Gốc)")
except ImportError:
    try:
        import pandas_ta_classic as ta
        print("✅ Đã load thư viện: pandas_ta_classic")
    except ImportError:
        print("❌ LỖI: Không tìm thấy thư viện. Hãy kiểm tra lại file YAML.")

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAME = '4h'
TREND_TF = '1d'
LIMIT = 200

# Lấy Token Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# --- THAY ĐỔI QUAN TRỌNG: DÙNG MEXC THAY VÌ BINANCE ---
# MEXC không chặn IP của GitHub Actions
exchange = ccxt.mexc({
    'enableRateLimit': True,
    # Chúng ta dùng giá Spot (Giao ngay) vì nó ổn định nhất trên GitHub
    # Giá Spot và Future chênh nhau không đáng kể ở khung H4
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
        # MEXC đôi khi cần đổi tên timeframe (4h -> 4h, nhưng 1d -> 1d vẫn ok)
        bars = exchange.fetch_ohlcv(symbol, tf, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        print(f"❌ Lỗi tải {symbol}: {e}")
        return None

def analyze(symbol):
    df_d1 = fetch_data(symbol, TREND_TF)
    df_h4 = fetch_data(symbol, TIMEFRAME)
    
    if df_d1 is None or df_h4 is None: return

    try:
        # 1. Tính toán Indicator
        df_d1['ema50'] = df_d1.ta.ema(length=50)
        
        df_h4['ema20'] = df_h4.ta.ema(length=20)
        df_h4['atr'] = df_h4.ta.atr(length=14)
        df_h4['swing_low'] = df_h4['low'].rolling(10).min()
        df_h4['swing_high'] = df_h4['high'].rolling(10).max()
        
        curr = df_h4.iloc[-1]
        prev = df_h4.iloc[-2]
        d1_last = df_d1.iloc[-2]

        # 2. Xu hướng D1
        trend = 1 if d1_last['close'] > d1_last['ema50'] else -1
        
        signal = None
        sl = 0.0
        
        # 3. Logic SMC
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
                    
        # 4. Gửi báo cáo
        if signal:
            risk = abs(curr['close'] - sl)
            tp = curr['close'] + (risk * 2) if signal == "BUY" else curr['close'] - (risk * 2)
            msg = (f"🚀 TÍN HIỆU H4 (MEXC Data): {symbol} - {signal}\nGiá: {curr['close']}\nSL: {sl:.2f}\nTP: {tp:.2f}")
            print(msg)
            send_telegram(msg)
        else:
            print(f"{symbol}: Waiting... (Trend {'UP' if trend==1 else 'DOWN'})")

    except Exception as e:
        print(f"⚠️ Lỗi tính toán {symbol}: {e}")

if __name__ == "__main__":
    print(f"🕒 Chạy quét lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for symbol in PAIRS:
        analyze(symbol)
        time.sleep(1) 
    print("✅ Hoàn tất.")
