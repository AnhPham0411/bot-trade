import ccxt
import pandas as pd
import os
import requests
import time
from datetime import datetime, timezone

# --- CẤU HÌNH ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT']
TIMEFRAMES = ['1h', '4h'] 

# Cấu hình Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
env_chat_id = os.getenv('TELEGRAM_CHAT_ID')

CHAT_IDS = ['-5103508011'] # Nhóm mới
if env_chat_id:
    CHAT_IDS.append(env_chat_id) # Thêm nhóm cũ nếu có

# Sàn MEXC
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# Load Lib
try:
    import pandas_ta as ta
except ImportError:
    try:
        import pandas_ta_classic as ta
    except: pass

# --- HÀM GỬI TELEGRAM ---
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
            print(f"❌ Lỗi gửi đến {chat_id}: {e}")

# --- HÀM LẤY DATA ---
def get_data(symbol, tf):
    try:
        # Tăng limit lên 300 để tính EMA 200 chính xác
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Lỗi lấy data {symbol}: {e}")
        return None

# --- HÀM PHÂN TÍCH (CHIẾN THUẬT HIGH WINRATE) ---
def analyze(symbol, tf):
    # In ra màn hình để biết đang quét
    print(f"🔎 Đang quét: {symbol} ({tf})...", end="\r")

    # Logic chạy theo giờ (chỉ chạy khung 4h vào lúc 0h, 4h, 8h...)
    current_hour_utc = datetime.now(timezone.utc).hour
    if tf == '4h' and current_hour_utc % 4 != 0:
        # Cho phép chạy du di trong 5 phút đầu của nến 4h
        if datetime.now(timezone.utc).minute > 5:
            return 

    df = get_data(symbol, tf)
    if df is None: return

    # --- 1. INDICATORS ---
    # EMA 200: Xu hướng dài
    df['ema_200'] = df.ta.ema(length=200)
    
    # Supertrend: Xu hướng ngắn & SL
    st = df.ta.supertrend(length=10, multiplier=3)
    df['st_dir'] = st['SUPERTd_10_3.0'] # 1=Xanh, -1=Đỏ
    df['st_val'] = st['SUPERTl_10_3.0'] # Giá trị để đặt SL
    
    # MACD: Động lượng
    macd = df.ta.macd(fast=12, slow=26, signal=9)
    df['macd_hist'] = macd['MACDh_12_26_9'] 
    
    # ATR & RSI
    df['atr'] = df.ta.atr(length=14)
    df['rsi'] = df.ta.rsi(length=14)

    # Lấy nến tín hiệu (Vừa đóng cửa)
    c = df.iloc[-2] 
    p = df.iloc[-3] 

    signal = None
    sl_price = 0.0
    tp_price = 0.0
    
    # --- 2. LỌC NẾN (Price Action) ---
    body_size = abs(c['close'] - c['open'])
    full_size = c['high'] - c['low']
    is_strong_candle = body_size > (full_size * 0.5) # Thân nến > 50%

    # --- 3. LOGIC BUY/SELL (Strict Mode) ---
    
    # === LONG ===
    if c['close'] > c['ema_200']: # Trend Tăng
        if c['st_dir'] == 1 and p['st_dir'] == -1: # Supertrend đổi xanh
            if c['macd_hist'] > 0: # MACD dương
                if is_strong_candle: # Nến dứt khoát
                    if c['rsi'] < 70: # Chưa quá mua
                        signal = "BUY"
                        sl_price = c['st_val']
                        risk = c['close'] - sl_price
                        min_reward = c['atr'] * 2
                        reward = max(risk * 1.5, min_reward)
                        tp_price = c['close'] + reward

    # === SHORT ===
    elif c['close'] < c['ema_200']: # Trend Giảm
        if c['st_dir'] == -1 and p['st_dir'] == 1: # Supertrend đổi đỏ
            if c['macd_hist'] < 0: # MACD âm
                if is_strong_candle:
                    if c['rsi'] > 30: # Chưa quá bán
                        signal = "SELL"
                        sl_price = c['st_val']
                        risk = sl_price - c['close']
                        min_reward = c['atr'] * 2
                        reward = max(risk * 1.5, min_reward)
                        tp_price = c['close'] - reward

    # --- 4. XỬ LÝ KẾT QUẢ ---
    if signal:
        entry = c['close']
        rr_display = abs(tp_price - entry) / abs(entry - sl_price) if abs(entry - sl_price) > 0 else 0
        
        icon = "🚀 LONG MẠNH" if signal == "BUY" else "📉 SHORT MẠNH"
        msg = (
            f"{icon} #{symbol} ({tf})\n"
            f"Entry: {entry:.4f}\n"
            f"SL: {sl_price:.4f} (Supertrend)\n"
            f"TP: {tp_price:.4f} (ATR Dynamic)\n"
            f"R:R: {rr_display:.2f}\n"
            f"----------------\n"
            f"✅ Hợp lưu: EMA200 + ST + MACD"
        )
        print(f"\n🔥 {msg}") # In ra console
        send_telegram(msg)
    else:
        # In trạng thái "Không có tín hiệu" để biết Bot đang chạy
        # Format ngắn gọn để dễ nhìn
        trend_status = "Tăng" if c['close'] > c['ema_200'] else "Giảm"
        st_status = "Xanh" if c['st_dir'] == 1 else "Đỏ"
        
        print(f"✅ {symbol} {tf}: Chờ tín hiệu (Giá: {c['close']:.2f} | Trend: {trend_status} | ST: {st_status})    ")

# --- MAIN ---
if __name__ == "__main__":
    print(f"\n--- 🤖 BOT KHỞI ĐỘNG: {datetime.now().strftime('%H:%M:%S')} ---")
    print(f"--- Chiến thuật: EMA200 + Supertrend + MACD + ATR ---")
    
    # Test thử gửi 1 tin nhắn (Bỏ comment dòng dưới nếu muốn test)
    # send_telegram("🤖 Bot đã khởi động lại!")

    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1) # Nghỉ 1s giữa các cặp để tránh spam API
    
    print("\n🏁 Hoàn tất lượt quét. Bot sẽ ngủ chờ lượt sau.")
