import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt  # Đã thêm import ccxt
from datetime import datetime, timezone

# ==========================================
# --- CẤU HÌNH ---
# ==========================================
# Chỉ chạy 4 coin theo yêu cầu
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
TIMEFRAMES = ['15m', '1h', '4h'] # Thêm 15m để bắt entry SMC chuẩn hơn

# Telegram Config
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN') 
# Nếu bạn chạy local, hãy thay trực tiếp token vào đây: "123456:ABC-DEF..."
CHAT_IDS = ['-5103508011'] # Thay ID nhóm của bạn vào đây

# Kết nối sàn MEXC
exchange = ccxt.mexc({
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

# Biến lưu trữ báo cáo
REPORT_DATA = []

# ==========================================
# PHẦN 1: HÀM TÍNH TOÁN CƠ BẢN
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

# Hàm tìm Pivot (Đỉnh/Đáy) để xác định OB và BOS
def find_pivots(df, window=5):
    # Pivot High: Đỉnh cao nhất trong window nến trái và phải
    df['pivot_high'] = df['high'].rolling(window=window*2+1, center=True).max()
    df['is_pivot_high'] = (df['high'] == df['pivot_high'])
    
    # Pivot Low: Đáy thấp nhất trong window nến trái và phải
    df['pivot_low'] = df['low'].rolling(window=window*2+1, center=True).min()
    df['is_pivot_low'] = (df['low'] == df['pivot_low'])
    return df

# ==========================================
# PHẦN 2: LOGIC SMC (OB, BOS, FVG)
# ==========================================

def check_smc_strategy(df):
    """
    Hàm này kiểm tra 3 điều kiện SMC: OB, BOS, FVG
    Trả về: Signal (BUY/SELL), Type (OB/BOS/FVG), Entry, SL, TP
    """
    # Lấy dữ liệu nến hiện tại và quá khứ
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    # --- 1. CHIẾN LƯỢC BREAK OF STRUCTURE (BOS) ---
    # Logic: Giá đóng cửa phá vỡ đỉnh/đáy gần nhất (Swing High/Low trong 20 nến)
    # Tìm Swing High/Low gần nhất (không tính nến hiện tại)
    recent_high = df['high'].iloc[-20:-1].max()
    recent_low = df['low'].iloc[-20:-1].min()
    
    # BOS BULLISH (Phá đỉnh cũ)
    if prev['close'] < recent_high and curr['close'] > recent_high:
        if curr['vol'] > df['vol'].iloc[-20:].mean() * 1.5: # Volume spike
            sl = curr['low']
            tp = curr['close'] + (curr['close'] - sl) * 3 # RR 1:3
            return "BUY", "BOS Breakout", curr['close'], sl, tp

    # BOS BEARISH (Phá đáy cũ)
    if prev['close'] > recent_low and curr['close'] < recent_low:
        if curr['vol'] > df['vol'].iloc[-20:].mean() * 1.5:
            sl = curr['high']
            tp = curr['close'] - (sl - curr['close']) * 3
            return "SELL", "BOS Breakout", curr['close'], sl, tp

    # --- 2. CHIẾN LƯỢC FAIR VALUE GAP (FVG) ---
    # FVG Bullish: High[i-2] < Low[i] (Có khoảng trống giá ở giữa)
    # Kiểm tra FVG được tạo ra ở nến CÁCH ĐÂY 1-2 phiên và giá hiện tại đang fill
    # Đơn giản hóa: Check nến i-1 là nến mạnh tạo FVG, nến i (curr) đang nhúng vào
    candle_gap_bull = df.iloc[-3]['high']
    candle_post_gap_bull = df.iloc[-1]['low'] # Giá thấp nhất hiện tại
    
    # Nếu có Gap tăng giá (Nến -2 tăng mạnh, để lại gap với nến -3)
    # Và giá hiện tại (nến -1) đang retest vùng gap đó
    if df.iloc[-2]['low'] > df.iloc[-4]['high']: # Xác nhận có FVG tăng ở nến trước
        fvg_zone_top = df.iloc[-2]['low']
        fvg_zone_bot = df.iloc[-4]['high']
        # Nếu giá hiện tại nhúng vào vùng này
        if fvg_zone_bot < curr['close'] < fvg_zone_top:
             sl = fvg_zone_bot * 0.995 # SL dưới FVG
             tp = curr['close'] * 1.02 # TP 2%
             return "BUY", "FVG Retest", curr['close'], sl, tp

    # --- 3. CHIẾN LƯỢC ORDER BLOCK (OB) ---
    # OB Bullish: Vùng giá thấp nhất (Pivot Low) trước đó, giờ giá quay lại test
    # Tìm Pivot Low gần nhất
    last_pivots = df[df['is_pivot_low'] == True].iloc[-5:] # 5 pivot gần nhất
    if not last_pivots.empty:
        ob_candle = last_pivots.iloc[-1] # Lấy OB gần nhất
        ob_low = ob_candle['low']
        ob_high = ob_candle['high']
        
        # Điều kiện: Giá hiện tại chạm vùng OB này (Retest)
        # Và OB này phải thấp hơn giá hiện tại (đang trong uptrend hoặc pullback)
        if ob_low <= curr['low'] <= ob_high: 
            # Confirmation: Nến hiện tại rút chân (Pinbar) hoặc xanh
            if curr['close'] > curr['open']: 
                sl = ob_low * 0.99 # SL dưới OB 1%
                tp = curr['close'] + (curr['close'] - sl) * 2 # RR 1:2
                return "BUY", "Order Block Test", curr['close'], sl, tp

    return None, None, 0, 0, 0

# ==========================================
# PHẦN 3: LOGIC CHÍNH & GỬI TIN
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
        bars = exchange.fetch_ohlcv(symbol, tf, limit=100) # Lấy 100 nến
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Lỗi data {symbol}: {e}")
        return None

def analyze(symbol, tf):
    print(f"🔎 Scanning: {symbol} ({tf})...", end="\r")

    df = get_data(symbol, tf)
    if df is None: return

    # Tính toán cơ bản
    df['rsi'] = calculate_rsi(df['close'], 14)
    df = find_pivots(df, window=3) # Tìm đỉnh đáy cho SMC

    # --- CHẠY LOGIC SMC ---
    signal, strat_name, entry, sl, tp = check_smc_strategy(df)

    if signal:
        rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        icon = "💎" if signal == "BUY" else "🩸"
        
        msg = (
            f"{icon} SMC SIGNAL: {symbol} ({tf})\n"
            f"Strategy: {strat_name}\n"
            f"Type: {signal}\n"
            f"Entry: {entry:.4f}\n"
            f"SL: {sl:.4f} | TP: {tp:.4f}\n"
            f"R:R: 1:{rr:.1f}\n"
            f"Vol: {df.iloc[-1]['vol']:.2f}"
        )
        print(f"\n{msg}")
        send_telegram(msg)
        REPORT_DATA.append(f"{icon} {symbol} ({tf}): {strat_name}")
    else:
        # Nếu không có kèo SMC, lưu trạng thái xu hướng cơ bản
        trend = "Bullish" if df.iloc[-1]['close'] > df.iloc[-1]['open'] else "Bearish"
        REPORT_DATA.append(f"Analyzing {symbol} ({tf}): {trend} (No Setup)")

# ==========================================
# MAIN LOOP
# ==========================================

if __name__ == "__main__":
    print(f"\n--- SMC BOT START: {datetime.now().strftime('%H:%M')} ---")
    
    REPORT_DATA = []
    
    for symbol in PAIRS:
        for tf in TIMEFRAMES:
            analyze(symbol, tf)
            time.sleep(1) # Tránh rate limit
            
    # Gửi báo cáo tổng kết ngắn gọn (Optional)
    # if REPORT_DATA:
    #     summary = "📊 SCAN COMPLETED:\n" + "\n".join(REPORT_DATA)
    #     send_telegram(summary)
        
    print("\n🏁 Done.")
