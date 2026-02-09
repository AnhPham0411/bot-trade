import ccxt
import pandas as pd
import os
import requests
import time
from datetime import datetime, timezone

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAMES = ['1h', '4h'] 

# Cấu hình chiến thuật (Đánh nhanh thắng nhanh)
RR_RATIO = 1.5       # Ăn 1.5 là chốt
SL_LOOKBACK = 3      # Chỉ tìm đỉnh/đáy trong 3 nến gần nhất (SL ngắn)

# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Sàn MEXC (Không cần API Key để lấy giá)
exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})

# Load thư viện
try:
    import pandas_ta as ta
except ImportError:
    try:
        import pandas_ta_classic as ta
    except: pass

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except: pass

def get_data(symbol, tf):
    try:
        # Lấy 100 nến là đủ tính toán, nhẹ server
        bars = exchange.fetch_ohlcv(symbol, tf, limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except: return None

def analyze(symbol, tf):
    # --- LOGIC CHẠY THEO GIỜ (QUAN TRỌNG) ---
    # H1: Chạy mỗi tiếng.
    # H4: Chỉ chạy khi giờ UTC chia hết cho 4 (0, 4, 8, 12...)
    # Tương ứng giờ VN: 7h, 11h, 15h, 19h...
    current_hour_utc = datetime.now(timezone.utc).hour
    if tf == '4h' and current_hour_utc % 4 != 0:
        return 

    df = get_data(symbol, tf)
    if df is None: return

    # --- INDICATOR ---
    df['ema_trend'] = df.ta.ema(length=34) # Xu hướng chính
    df['ema_fast'] = df.ta.ema(length=13)  # Xu hướng ngắn
    df['atr'] = df.ta.atr(length=14)

    # --- QUAN TRỌNG: Dùng iloc[-2] là nến VỪA ĐÓNG CỬA ---
    # Tuyệt đối không dùng iloc[-1] (nến đang chạy)
    c = df.iloc[-2] 
    p = df.iloc[-3] 

    # Tìm SL ngắn: Đỉnh/Đáy trong 3 nến gần nhất
    recent_low = df['low'].iloc[-4:-1].min()
    recent_high = df['high'].iloc[-4:-1].max()

    signal = None
    sl_price = 0.0

    # --- LOGIC TÍN HIỆU ---
    # BUY: Giá trên EMA Trend + Giá trên EMA Fast + Break đỉnh nến trước
    if c['close'] > c['ema_trend']:
        if c['close'] > c['ema_fast'] and c['close'] > c['open']:
            if c['close'] > p['high']:
                signal = "BUY"
                sl_price = recent_low - (c['atr'] * 0.1) # Trừ 1 xíu spread

    # SELL: Giá dưới EMA Trend + Giá dưới EMA Fast + Break đáy nến trước
    elif c['close'] < c['ema_trend']:
        if c['close'] < c['ema_fast'] and c['close'] < c['open']:
            if c['close'] < p['low']:
                signal = "SELL"
                sl_price = recent_high + (c['atr'] * 0.1) # Cộng 1 xíu spread

    # --- TÍNH TP/SL & GỬI TIN ---
    if signal:
        entry = c['close']
        risk = abs(entry - sl_price)
        
        # SL tối thiểu = 20% cây ATR (để không bị quét râu quá ngắn)
        min_risk = c['atr'] * 0.2
        if risk < min_risk: risk = min_risk
        
        # Recalculate SL chuẩn
        sl_price = entry - risk if signal == "BUY" else entry + risk
        
        # TP theo tỷ lệ R:R
        tp_price = entry + (risk * RR_RATIO) if signal == "BUY" else entry - (risk * RR_RATIO)

        # Icon
        icon = "🟢 LONG" if signal == "BUY" else "🔴 SHORT"
        
        # Nội dung tin nhắn (Làm tròn 2 số cho ngắn)
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
    
    # Test telegram khi khởi động (bỏ comment nếu muốn test)
    # send_telegram("🤖 Bot started scanning...")

    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(0.5) # Nghỉ nhẹ để không bị ban IP
    
    print("✅ Hoàn tất.")
