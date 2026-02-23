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
# --- 1. CẤU HÌNH HỆ THỐNG VÀ CHIẾN THUẬT ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'15m': '4h', '1h': '4h'} # Ép La Bàn 4H

# --- QUẢN TRỊ RỦI RO & BẮT LỆNH ---
SL_ATR_MULTIPLIER = 1.5
MAX_BARS_SINCE_OB = 22

# --- TÙY CHỌN ENTRY & MITIGATION (MỚI THEO V6.3) ---
ENTRY_MODE = "EDGE"             # Chọn "EDGE" (Bắt mép OB) hoặc "MID" (Bắt ở 50% OB)
MITIGATION_LEVEL = "50_PCT"     # Chọn "50_PCT" (Hủy nếu lủng 50%) hoặc "EXTREME" (Hủy nếu lủng hẳn râu OB)

# --- BỘ LỌC DYNAMIC TP & SCORING ---
MIN_SCORE = 4         # Chỉ bắn lệnh từ 4 điểm trở lên
RR_TIER_2 = 1.67      # Kèo ngon (Score 4, 5)
RR_TIER_3 = 2.4       # Kèo Unicorn (Score 6, 7)

# Thông tin API
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
            pass
        return {}

    def save(self):
        if not self.github_token or not self.gist_id: return
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {"files": {self.filename: {"content": json.dumps(self.state, indent=2)}}}
            requests.patch(url, headers=self.headers, json=payload, timeout=10)
        except Exception as e:
            pass

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
    df['is_f_high'] = (df['high'].shift(2) > df['high'].shift(4)) & \
                      (df['high'].shift(2) > df['high'].shift(3)) & \
                      (df['high'].shift(2) > df['high'].shift(1)) & \
                      (df['high'].shift(2) > df['high'])
    df['is_f_low']  = (df['low'].shift(2)  < df['low'].shift(4)) & \
                      (df['low'].shift(2)  < df['low'].shift(3)) & \
                      (df['low'].shift(2)  < df['low'].shift(1)) & \
                      (df['low'].shift(2)  < df['low'])
    return df

def get_htf_trend(symbol, htf):
    bars = fetch_ohlcv_safe(symbol, htf, limit=300)
    if not bars: return "SIDEWAY"
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    ema_200 = calculate_ema(df['close'], 200).iloc[-2]
    atr = calculate_atr(df).iloc[-2]
    close_price = df['close'].iloc[-2]
    
    if abs(close_price - ema_200) < atr: return "SIDEWAY"
    return "UP" if close_price > ema_200 else "DOWN"

def get_swing_range(df):
    lookback = 120  
    return df['high'].iloc[-lookback:].max(), df['low'].iloc[-lookback:].min()

def is_premium_discount(entry, trend, swing_high, swing_low):
    mid = (swing_high + swing_low) / 2
    return entry < mid if trend == "UP" else entry > mid

# ==========================================
# --- 5. HỆ THỐNG LỌC EXTREME OB VÀ MITIGATION ---
# ==========================================
def find_extreme_ob_and_score(df, trend, current_atr):
    fractals = df[df['is_f_low']] if trend == "UP" else df[df['is_f_high']]
    if fractals.empty: return None

    # Lấy vị trí nến Fractal cuối cùng
    idx = df.index.get_loc(fractals.index[-1])
    
    # Giới hạn vùng quét nến (Từ idx-10 đến idx+2)
    start_scan = max(0, idx - 10)
    end_scan = min(len(df) - 1, idx + 2)
    
    best_ob_idx = -1
    
    if trend == "UP":
        # 1. TÌM EXTREME OB (Nến Đỏ thấp nhất)
        min_low = float('inf')
        for i in range(start_scan, end_scan + 1):
            if df['close'].iloc[i] < df['open'].iloc[i] and df['low'].iloc[i] < min_low:
                min_low = df['low'].iloc[i]
                best_ob_idx = i
                
        if best_ob_idx == -1: return None
        ob = df.iloc[best_ob_idx]

        # 2. KIỂM TRA FVG VÀ BOS (Base Requirements)
        has_fvg = (best_ob_idx + 2 < len(df)) and (df['low'].iloc[best_ob_idx + 2] > ob['high'])
        if not has_fvg: return None
        
        # Check BOS
        prev_highs = df[df['is_f_high'] & (df.index < best_ob_idx - 3)]
        if prev_highs.empty: return None
        recent_high = prev_highs['high'].iloc[-1]
        has_bos = (df.iloc[best_ob_idx : best_ob_idx + 30]['close'] > recent_high).any()
        if not has_bos: return None

        # 3. MITIGATION CHECK (Kiểm tra đã bị quét sâu chưa)
        ob_50_pct = (ob['high'] + ob['low']) / 2
        invalid_level = ob_50_pct if MITIGATION_LEVEL == "50_PCT" else ob['low']
        
        # Quét từ nến ngay sau OB đến nến hiện tại
        is_mitigated = False
        for j in range(best_ob_idx + 1, len(df) - 1):
            if df['low'].iloc[j] <= invalid_level:
                is_mitigated = True
                break
        
        if is_mitigated: return None # Gạch bỏ kèo này

        # 4. CHẤM ĐIỂM (SCORING)
        score = 3
        factors = [f"HTF Trend 4H: UP ✅", "BOS 🏗️", "FVG ⚡"]
        
        avg_vol = df['vol'].iloc[max(0, best_ob_idx-20):best_ob_idx].mean()
        if ob['vol'] > (avg_vol * 1.5):
            score += 1
            factors.append("Whale Volume 🐳")
            
        prev_low = df['low'].iloc[max(0, best_ob_idx-6):best_ob_idx].min()
        if ob['low'] < prev_low:
            score += 1
            factors.append("Liquidity Sweep 🦈")
            
        entry = ob['high'] if ENTRY_MODE == "EDGE" else ob_50_pct
        swing_h, swing_l = get_swing_range(df)
        if is_premium_discount(entry, "UP", swing_h, swing_l):
            score += 1
            factors.append("Discount Zone 💎")
            
        if df['rsi'].iloc[best_ob_idx] < 45:
            score += 1
            factors.append("RSI Oversold")

        sl = ob['low'] - (current_atr * SL_ATR_MULTIPLIER)
        return {"entry": entry, "sl": sl, "idx": best_ob_idx, "score": score, "factors": factors}

    else: # TREND DOWN
        # 1. TÌM EXTREME OB (Nến Xanh cao nhất)
        max_high = 0.0
        for i in range(start_scan, end_scan + 1):
            if df['close'].iloc[i] > df['open'].iloc[i] and df['high'].iloc[i] > max_high:
                max_high = df['high'].iloc[i]
                best_ob_idx = i
                
        if best_ob_idx == -1: return None
        ob = df.iloc[best_ob_idx]

        # 2. KIỂM TRA FVG VÀ BOS (Base Requirements)
        has_fvg = (best_ob_idx + 2 < len(df)) and (df['high'].iloc[best_ob_idx + 2] < ob['low'])
        if not has_fvg: return None
        
        # Check BOS
        prev_lows = df[df['is_f_low'] & (df.index < best_ob_idx - 3)]
        if prev_lows.empty: return None
        recent_low = prev_lows['low'].iloc[-1]
        has_bos = (df.iloc[best_ob_idx : best_ob_idx + 30]['close'] < recent_low).any()
        if not has_bos: return None

        # 3. MITIGATION CHECK (Kiểm tra đã bị quét sâu chưa)
        ob_50_pct = (ob['high'] + ob['low']) / 2
        invalid_level = ob_50_pct if MITIGATION_LEVEL == "50_PCT" else ob['high']
        
        # Quét từ nến ngay sau OB đến nến hiện tại
        is_mitigated = False
        for j in range(best_ob_idx + 1, len(df) - 1):
            if df['high'].iloc[j] >= invalid_level:
                is_mitigated = True
                break
        
        if is_mitigated: return None

        # 4. CHẤM ĐIỂM (SCORING)
        score = 3
        factors = [f"HTF Trend 4H: DOWN ✅", "BOS 🏗️", "FVG ⚡"]
        
        avg_vol = df['vol'].iloc[max(0, best_ob_idx-20):best_ob_idx].mean()
        if ob['vol'] > (avg_vol * 1.5):
            score += 1
            factors.append("Whale Volume 🐳")
            
        prev_high = df['high'].iloc[max(0, best_ob_idx-6):best_ob_idx].max()
        if ob['high'] > prev_high:
            score += 1
            factors.append("Liquidity Sweep 🦈")
            
        entry = ob['low'] if ENTRY_MODE == "EDGE" else ob_50_pct
        swing_h, swing_l = get_swing_range(df)
        if is_premium_discount(entry, "DOWN", swing_h, swing_l):
            score += 1
            factors.append("Premium Zone 💎")
            
        if df['rsi'].iloc[best_ob_idx] > 55:
            score += 1
            factors.append("RSI Overbought")

        sl = ob['high'] + (current_atr * SL_ATR_MULTIPLIER)
        return {"entry": entry, "sl": sl, "idx": best_ob_idx, "score": score, "factors": factors}

# ==========================================
# --- 6. ENGINE CHÍNH ---
# ==========================================
def analyze_pair(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return False

    bars = fetch_ohlcv_safe(symbol, tf, limit=300)
    if not bars: return False

    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    df = identify_fractals(df)
    df['atr'] = calculate_atr(df, length=14)
    df['rsi'] = calculate_rsi(df['close'])
    atr = df['atr'].iloc[-2]

    # Nhận dữ liệu từ hàm xử lý Extreme OB
    result = find_extreme_ob_and_score(df, htf_trend, atr)
    if not result: return False
    
    entry = result['entry']
    sl = result['sl']
    ob_idx = result['idx']
    setup_score = result['score']
    factors = result['factors']

    # Lọc điểm số tối thiểu
    if setup_score < MIN_SCORE: return False

    risk = abs(entry - sl)
    signal_type = "BUY" if htf_trend == "UP" else "SELL"

    # DYNAMIC TP LOGIC
    if setup_score >= 6:
        dyn_rr = RR_TIER_3
        model_name = "🦄 UNICORN SETUP"
    else:
        dyn_rr = RR_TIER_2
        model_name = "🔥 STRONG SETUP"

    tp = entry + (risk * dyn_rr) if signal_type == "BUY" else entry - (risk * dyn_rr)
    tp1 = entry + risk if signal_type == "BUY" else entry - risk # Mốc 1R

    # Kiểm tra tuổi OB
    current_price = df['close'].iloc[-2]
    distance_atr = abs(current_price - entry) / atr
    bars_since_ob = len(df) - 2 - ob_idx
    if distance_atr > 3.0 or bars_since_ob > MAX_BARS_SINCE_OB: return False

    # Chống Spam Telegram
    key = f"{symbol}_{tf}_{ob_idx}"
    if state_manager.is_alerted(key): return False

    # Check Trạng thái giá hiện tại
    tapped = False
    if signal_type == "BUY" and df['low'].iloc[-2] <= (entry + 0.5 * atr): tapped = True
    elif signal_type == "SELL" and df['high'].iloc[-2] >= (entry - 0.5 * atr): tapped = True

    execution = "Tapped Zone 👀" if tapped else "Waiting Limit ⏳"

    msg = (f"🚀 <b>SMC PRO v6.3 main5- {signal_type} {model_name}</b>\n"
           f"Symbol: <b>{symbol}</b> ({tf}) | Age: {bars_since_ob} bars\n"
           f"-----------------\n"
           f"Score: <b>{setup_score}/7</b>\n"
           f"Execution: <b>{execution}</b>\n"
           f"Entry ({ENTRY_MODE}): <code>{entry:.4f}</code>\n"
           f"Stoploss: <code>{sl:.4f}</code>\n"
           f"Target ({dyn_rr}R): <code>{tp:.4f}</code>\n"
           f"-----------------\n"
           f"🛡️ <b>Quản trị Lệnh:</b>\n"
           f"1. Limit tại Entry.\n"
           f"2. Chạm 1R (<code>{tp1:.4f}</code>) -> Chốt 50% & Dời SL Hòa.\n"
           f"3. Thả trôi tới Target {dyn_rr}R.\n"
           f"<i>(Đã qua bộ lọc Mitigation: An toàn)</i>\n"
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
            pass

# ==========================================
# --- 7. MAIN RUN ---
# ==========================================
if __name__ == "__main__":
    scan_time = datetime.now().strftime('%H:%M:%S')
    print(f"🚀 SMC PRO v6.3 Started: {scan_time}")
    
    signals_found = 0
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            if analyze_pair(symbol, tf):
                signals_found += 1
            time.sleep(1.2)
            
    if signals_found == 0:
        alive_msg = (f"🤖 <b>SMC Bot Status: ALIVE 🟢</b>\n"
                     f"Time: <code>{scan_time}</code>\n"
                     f"Khung 4H đang không đồng thuận hoặc chưa có Setup xịn.\n"
                     f"<i>P/s: Chờ Cá mập dọn đường nhé! ☕🦈</i>")
        send_telegram(alive_msg)
        print(">>> Bot Alive: Sent heartbeat.")
        
    print(f"✅ Finished at {datetime.now().strftime('%H:%M:%S')}")
