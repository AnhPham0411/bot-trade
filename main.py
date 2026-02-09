import ccxt
import pandas as pd
import os
import requests
import time
from datetime import datetime, timezone

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAMES = ['1h', '4h'] 

# Chiến thuật (Tight SL)
RR_RATIO = 1.5      
SL_LOOKBACK = 3     

# --- CẤU HÌNH TELEGRAM ĐA KÊNH ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# 1. Lấy ID nhóm chính từ Secret (Nhóm cũ)
env_chat_id = os.getenv('TELEGRAM_CHAT_ID')

# 2. Tạo danh sách các nhóm cần gửi
# Đã thêm nhóm mới của bạn vào đây
CHAT_IDS = ['-5103508011']

# Nếu trong Secret có ID thì thêm vào danh sách luôn
if env_chat_id:
    CHAT_IDS.append(env_chat_id)

# Sàn MEXC
exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})

# Load Lib
try:
    import pandas_ta as ta
except ImportError:
    try:
        import pandas_ta_classic as ta
    except: pass

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    
    # --- VÒNG LẶP GỬI CHO TẤT CẢ CÁC NHÓM ---
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=5
            )
        except Exception as e:
            print(f"Lỗi gửi đến {chat_id}: {e}")

def get_data(symbol, tf):
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except: return None

def analyze(symbol, tf):
    # Logic chạy theo giờ
    current_hour_utc = datetime.now(timezone.utc).hour
    if tf == '4h' and current_hour_utc % 4 != 0:
        return 

    df = get_data(symbol, tf)
    if df is None: return

    # Indicator
    df['ema_trend'] = df.ta.ema(length=34) 
    df['ema_fast'] = df.ta.ema(length=13)  
    df['atr'] = df.ta.atr(length=14)

    c = df.iloc[-2] # Nến vừa đóng
    p = df.iloc[-3] 

    recent_low = df['low'].iloc[-4:-1].min()
    recent_high = df['high'].iloc[-4:-1].max()

    signal = None
    sl_price = 0.0

    # Logic Buy/Sell
    if c['close'] > c['ema_trend']:
        if c['close'] > c['ema_fast'] and c['close'] > c['open']:
            if c['close'] > p['high']:
                signal = "BUY"
                sl_price = recent_low - (c['atr'] * 0.1)

    elif c['close'] < c['ema_trend']:
        if c['close'] < c['ema_fast'] and c['close'] < c['open']:
            if c['close'] < p['low']:
                signal = "SELL"
                sl_price = recent_high + (c['atr'] * 0.1)

    # Gửi tin nhắn
    if signal:
        entry = c['close']
        risk = abs(entry - sl_price)
        
        min_risk = c['atr'] * 0.2
        if risk < min_risk: risk = min_risk
        
        sl_price = entry - risk if signal == "BUY" else entry + risk
        tp_price = entry + (risk * RR_RATIO) if signal == "BUY" else entry - (risk * RR_RATIO)

        icon = "🟢 LONG" if signal == "BUY" else "🔴 SHORT"
        
        msg = (
            f"{icon} #{symbol} ({tf})\n"
            f"Entry: {entry:.2f}\n"
            f"SL: {sl_price:.2f}\n"
            f"TP: {tp_price:.2f}\n"
            f"R:R: {RR_RATIO}"
        )
        print(msg)
        send_telegram(msg)
    else:
        print(f"⏳ {symbol} {tf}: Không có tín hiệu")

if __name__ == "__main__":
    print(f"--- Scan: {datetime.now().strftime('%H:%M')} ---")
    
    # Test thử gửi 1 tin nhắn xem cả 2 nhóm nhận được chưa (bỏ comment dòng dưới để test)
    # send_telegram(f"🤖 Bot Update: Đã thêm nhóm mới thành công!")

    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(0.5)
    
    print("✅ Hoàn tất.")
