import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import json
from datetime import datetime
from functools import wraps

# ==========================================
# --- 1. CẤU HÌNH HỆ THỐNG ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

# ÉP LA BÀN 4H: Cả 15m và 1h đều phải thuận xu hướng của 4H
MTF_MAPPING = {'15m': '4h', '1h': '4h'}

SL_ATR_MULTIPLIER = 1.5
ENTRY_TOLERANCE = 0.5
WHALE_VOL_MULTIPLIER = 1.5
MAX_BARS_SINCE_OB = 22          

# --- BỘ LỌC DYNAMIC TP & SCORING ---
MIN_SCORE = 4         # Chỉ bắn lệnh từ 4 điểm trở lên
RR_TIER_2 = 1.67      # Kèo ngon (Score 4, 5)
RR_TIER_3 = 2.4       # Kèo Unicorn (Score 6, 7)

# --- CẤU HÌNH TÍNH NĂNG (BẬT/TẮT) ---
ENABLE_ORDER_ANTISPAM = True  # True: Chống bắn lặp lại cùng 1 lệnh. False: Bắn liên tục.
ENABLE_HEARTBEAT = False      # True: Báo bot "còn sống" khi không có kèo.
ENABLE_KILLZONES = False       # True: Chỉ trade phiên London (14h-17h VN) & New York (19h-22h VN).

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# ==========================================
# --- 2. QUẢN LÝ TRẠNG THÁI (GITHUB GIST) ---
# ==========================================
class GistStateManager:
    def __init__(self, filename='bot_state.json'):
        self.filename = filename
        self.github_token = os.getenv('GH_GIST_TOKEN')
        self.gist_id = os.getenv('GIST_ID')
        self.headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        } if self.github_token else {}
        self.state = self.load()

    def load(self):
        if not self.github_token or not self.gist_id: return {}
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                files = response.json().get('files', {})
                if self.filename in files:
                    return json.loads(files[self.filename]['content'])
        except Exception as e:
            print(f"Lỗi đọc Gist: {e}")
        return {}

    def save(self):
        if not self.github_token or not self.gist_id: return
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {"files": {self.filename: {"content": json.dumps(self.state, indent=2)}}}
            requests.patch(url, headers=self.headers, json=payload, timeout=10)
        except Exception as e:
            print(f"Lỗi lưu Gist: {e}")

    def is_alerted(self, key, cooldown=4000):
        now = time.time()
        if key in self.state and (now - self.state[key]) < cooldown:
            return True
        self.state[key] = now
        self.save()
        return False

state_manager = GistStateManager()

# ==========================================
# --- 3. TIỆN ÍCH & API RETRY ---
# ==========================================
def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    print(f"⚠️ API Error ({func.__name__}) - Attempt {attempt+1}/{retries}: {e}")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_api(retries=3)
def fetch_ohlcv_safe(symbol, tf, limit):
    return exchange.fetch_ohlcv(symbol, tf, limit=limit)

# ==========================================
# --- 4. HÀM CORE & CHỈ BÁO ---
# ==========================================
def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(window=length).mean()

def identify_fractals(df):
    df = df.copy()
    df['is_fractal_high'] = (df['high'].shift(2) > df['high'].shift(4)) & \
                            (df['high'].shift(2) > df['high'].shift(3)) & \
                            (df['high'].shift(2) > df['high'].shift(1)) & \
                            (df['high'].shift(2) > df['high'])
    df['is_fractal_low'] = (df['low'].shift(2) < df['low'].shift(4)) & \
                           (df['low'].shift(2) < df['low'].shift(3)) & \
                           (df['low'].shift(2) < df['low'].shift(1)) & \
                           (df['low'].shift(2) < df['low'])
    return df

def check_fvg(df, idx, direction):
    if idx + 2 >= len(df): return False
    if direction == "UP": return df['low'].iloc[idx + 2] > df['high'].iloc[idx]
    return df['high'].iloc[idx + 2] < df['low'].iloc[idx]

def check_bos_choch(df, current_idx, trend, lookback_bars=30):
    if current_idx >= len(df): return False
    end_idx = min(current_idx + lookback_bars, len(df))
    check_range = df.iloc[current_idx : end_idx]
    if check_range.empty: return False

    if trend == "UP":
        prev_highs = df[df['is_fractal_high'] & (df.index < current_idx - 3)]
        if prev_highs.empty: return False
        return (check_range['close'] > prev_highs['high'].iloc[-1]).any()
    elif trend == "DOWN":
        prev_lows = df[df['is_fractal_low'] & (df.index < current_idx - 3)]
        if prev_lows.empty: return False
        return (check_range['close'] < prev_lows['low'].iloc[-1]).any()
    return False

def has_liquidity_sweep(df, ob_idx, trend):
    if trend == "UP":
        prev_fractals = df[df['is_fractal_low'] & (df.index < ob_idx)]
        if prev_fractals.empty: return False
        check_range = df.iloc[max(0, ob_idx-5):ob_idx]
        return (check_range['low'] < prev_fractals['low'].iloc[-1]).any()
    else:
        prev_fractals = df[df['is_fractal_high'] & (df.index < ob_idx)]
        if prev_fractals.empty: return False
        check_range = df.iloc[max(0, ob_idx-5):ob_idx]
        return (check_range['high'] > prev_fractals['high'].iloc[-1]).any()

def get_htf_trend(symbol, htf):
    bars = fetch_ohlcv_safe(symbol, htf, limit=500)
    if not bars: return "SIDEWAY"
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    ema_200 = calculate_ema(df['close'], 200).iloc[-2]
    atr = calculate_atr(df).iloc[-2]
    close_price = df['close'].iloc[-2]
    
    if abs(close_price - ema_200) < atr: return "SIDEWAY"
    return "UP" if close_price > ema_200 else "DOWN"

def find_quality_zone(df, trend, current_atr):
    fractal_lows = df[df['is_fractal_low']]
    fractal_highs = df[df['is_fractal_high']]
    
    def check_whale_vol(df, pos, ob_vol):
        avg_vol = df['vol'].iloc[max(0, pos-20):pos].mean()
        return ob_vol > (avg_vol * WHALE_VOL_MULTIPLIER) if avg_vol > 0 else False

    # NÂNG CẤP 1: TÌM STOPLOSS THEO SWING STRUCTURE THAY VÌ OB
    if trend == "UP" and not fractal_lows.empty:
        idx = df.index.get_loc(fractal_lows.index[-1])
        swing_low = df['low'].iloc[idx] # Lấy đáy thấp nhất của con sóng
        
        sub = df.iloc[max(0, idx-10):idx+2] 
        red_candles = sub[sub['close'] < sub['open']]
        if not red_candles.empty:
            ob = red_candles.loc[red_candles['low'].idxmin()]
            pos = df.index.get_loc(ob.name)
            
            if check_fvg(df, pos, "UP") and check_bos_choch(df, pos, "UP"):
                has_whale = check_whale_vol(df, pos, ob['vol'])
                # SL đặt dưới Swing Low, né quét râu thủng OB
                sl = min(ob['low'], swing_low) - (current_atr * 0.3)
                return (ob['high'] + ob['low']) / 2, sl, pos, True, has_whale

    elif trend == "DOWN" and not fractal_highs.empty:
        idx = df.index.get_loc(fractal_highs.index[-1])
        swing_high = df['high'].iloc[idx] # Lấy đỉnh cao nhất của con sóng
        
        sub = df.iloc[max(0, idx-10):idx+2]
        green_candles = sub[sub['close'] > sub['open']]
        if not green_candles.empty:
            ob = green_candles.loc[green_candles['high'].idxmax()]
            pos = df.index.get_loc(ob.name)
            
            if check_fvg(df, pos, "DOWN") and check_bos_choch(df, pos, "DOWN"):
                has_whale = check_whale_vol(df, pos, ob['vol'])
                # SL đặt trên Swing High
                sl = max(ob['high'], swing_high) + (current_atr * 0.3)
                return (ob['high'] + ob['low']) / 2, sl, pos, True, has_whale

    return 0, 0, 0, False, False

def get_swing_range(df):
    lookback = 300  # Đã mở rộng lên 300 nến để góc nhìn tổng quan hơn
    return df['high'].iloc[-lookback:].max(), df['low'].iloc[-lookback:].min()

def is_premium_discount(entry, trend, swing_high, swing_low):
    mid = (swing_high + swing_low) / 2
    return entry < mid if trend == "UP" else entry > mid

def is_trigger_candle(df, idx, signal_type):
    candle = df.iloc[idx]
    prev = df.iloc[idx-1]
    body = abs(candle['close'] - candle['open'])
    upper = candle['high'] - max(candle['close'], candle['open'])
    lower = min(candle['close'], candle['open']) - candle['low']
    
    if signal_type == "BUY":
        if lower > body * 2 and lower > upper: return True, "Bullish Pinbar"
        if prev['close'] < prev['open'] and candle['close'] > candle['open'] and \
           candle['close'] > prev['open'] and candle['open'] < prev['close']: return True, "Bullish Engulfing"
    else:
        if upper > body * 2 and upper > lower: return True, "Bearish Pinbar"
        if prev['close'] > prev['open'] and candle['close'] < candle['open'] and \
           candle['close'] < prev['open'] and candle['open'] > prev['close']: return True, "Bearish Engulfing"
    return False, ""

# ==========================================
# --- 5. ENGINE SMC PRO v5.8 ---
# ==========================================
def analyze_pair(symbol, tf):
    # NÂNG CẤP 3: LỌC NHIỄU BẰNG KILLZONES (PHIÊN GIAO DỊCH)
    if ENABLE_KILLZONES:
        current_utc = datetime.utcnow().hour
        # UTC hours cho London (14h-17h VN) và NY (19h-22h VN)
        if current_utc not in [7, 8, 9, 10, 12, 13, 14, 15]:
            return False

    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return False

    bars = fetch_ohlcv_safe(symbol, tf, limit=1000) # Đã nâng cấp lên 1000 nến
    if not bars: return False

    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    df = identify_fractals(df)
    df['atr'] = calculate_atr(df, length=14)
    df['rsi'] = calculate_rsi(df['close'])
    atr = df['atr'].iloc[-2]

    entry, sl, ob_idx, has_fvg, has_whale_vol = find_quality_zone(df, htf_trend, atr)
    if entry == 0: return False

    risk = abs(entry - sl)
    setup_score = 3  
    factors = [f"HTF Trend 4H: {htf_trend} ✅", "Valid BOS 🏗️", "Displacement (FVG) ⚡"]

    if has_whale_vol:
        setup_score += 1
        factors.append("Whale Volume 🐳")
        
    rsi = df['rsi'].iloc[-2]
    signal_type = "BUY" if htf_trend == "UP" else "SELL"
    if (signal_type == "BUY" and rsi < 45) or (signal_type == "SELL" and rsi > 55):
        setup_score += 1
        factors.append(f"RSI Momentum ({rsi:.1f})")
        
    if is_premium_discount(entry, htf_trend, *get_swing_range(df)):
        setup_score += 1
        factors.append("Premium/Discount Zone 💎")

    if has_liquidity_sweep(df, ob_idx, htf_trend):
        setup_score += 1
        factors.append("Liquidity Sweep (Inducement) 🦈")

    if setup_score < MIN_SCORE: return False
    model_name = "🦄 UNICORN SETUP" if setup_score >= 6 else "🔥 STRONG SETUP"

    # NÂNG CẤP 2: TP THEO THANH KHOẢN CẤU TRÚC (STRUCTURAL LIQUIDITY)
    search_range = df.iloc[ob_idx + 1 : -1] # Khoảng từ OB đến nến hiện tại
    if search_range.empty: return False
    
    if signal_type == "BUY":
        struct_tp = search_range['high'].max() # Đỉnh cao nhất hút thanh khoản
    else:
        struct_tp = search_range['low'].min() # Đáy thấp nhất hút thanh khoản

    # Tính R:R thực tế dựa trên điểm TP cấu trúc
    struct_rr = abs(struct_tp - entry) / risk if risk > 0 else 0

    # Lọc nhiễu: TP cấu trúc quá hẹp, RR không đạt 1.2 -> Bỏ kèo
    if struct_rr < 1.2:
        return False

    tp = struct_tp
    dyn_rr = round(struct_rr, 2)
    tp1 = entry + risk if signal_type == "BUY" else entry - risk # Mốc 1R

    current_price = df['close'].iloc[-2]
    distance_atr = abs(current_price - entry) / atr
    bars_since_ob = len(df) - 2 - ob_idx
    if distance_atr > 3.0 or bars_since_ob > MAX_BARS_SINCE_OB: return False

    key = f"{symbol}_{tf}_{ob_idx}"
    if ENABLE_ORDER_ANTISPAM and state_manager.is_alerted(key): return False

    last_idx = len(df) - 2
    has_trigger, trigger_name = is_trigger_candle(df, last_idx, signal_type)

    tapped = False
    if signal_type == "BUY" and df.iloc[-2]['low'] <= (entry + ENTRY_TOLERANCE * atr): tapped = True
    elif signal_type == "SELL" and df.iloc[-2]['high'] >= (entry - ENTRY_TOLERANCE * atr): tapped = True

    if tapped and has_trigger: execution = "CE Triggered ⚡"
    elif tapped: execution = "Tapped Zone 👀"
    else: execution = "Waiting Limit ⏳"

    trigger_info = f"🕯️ Trigger: <b>{trigger_name}</b>\n" if has_trigger else ""

    msg = (f"🚀 <b>SMC PRO v5.8 - {signal_type} {model_name}</b>\n"
           f"Symbol: <b>{symbol}</b> ({tf}) | Age: {bars_since_ob} bars\n"
           f"-----------------\n"
           f"Score: <b>{setup_score}/7</b>\n"
           f"Execution: <b>{execution}</b>\n"
           f"{trigger_info}"
           f"Entry: <code>{entry:.4f}</code>\n"
           f"Swing SL: <code>{sl:.4f}</code> 🛡️\n"
           f"Struct TP ({dyn_rr}R): <code>{tp:.4f}</code> 🎯\n"
           f"-----------------\n"
           f"🛡️ <b>Quản trị Lệnh:</b>\n"
           f"1. Cài Limit tại Entry.\n"
           f"2. Chạm 1R (<code>{tp1:.4f}</code>) -> Chốt 50% & Dời SL Hòa.\n"
           f"3. Thả trôi tới Struct TP.\n"
           f"-----------------\n"
           f"🔍 Confluences:\n + " + "\n + ".join(factors))

    send_telegram(msg)
    print(f">>> {symbol} {tf}: {execution} ({model_name} - Target {dyn_rr}R)")
    return True

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            print(f"Lỗi gửi Telegram: {e}")

# ==========================================
# --- 6. MAIN CHẠY ---
# ==========================================
if __name__ == "__main__":
    scan_time = datetime.now().strftime('%H:%M:%S')
    print(f"🚀 SMC PRO v5.8 Started: {scan_time}")
    
    signals_found = 0
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            if analyze_pair(symbol, tf):
                signals_found += 1
            time.sleep(1.2)
            
    if signals_found == 0 and ENABLE_HEARTBEAT:
        alive_msg = (f"🤖 <b>SMC Bot Status 4: ALIVE 🟢</b>\n"
                     f"Time: <code>{scan_time}</code>\n"
                     f"La bàn 4H đang không đồng thuận hoặc chưa có Setup >= 4 điểm.\n"
                     f"<i>P/s: Chờ Cá mập dọn đường nhé! ☕🦈</i>")
        send_telegram(alive_msg)
        print(">>> Bot Alive: Sent heartbeat.")
        
    print(f"✅ Finished at {datetime.now().strftime('%H:%M:%S')}")
