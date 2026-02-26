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
# --- 1. CẤU HÌNH & BẢNG ĐIỀU KHIỂN ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAME = '1h'
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.8
MIN_SCORE = 4
MAX_BARS_LIMITS = {'15m': 35, '1h': 80, '4h': 55}

# ------------------------------------------
# 🔴 CÔNG TẮC BỘ LỌC ÉP BUỘC (HARD FILTERS)
# Nếu False: Chuyển sang chấm điểm mềm (Soft Score).
# Nếu True: Thiếu điều kiện này là hủy lệnh luôn.
# ------------------------------------------
ENABLE_FILTER_VOLUME   = False  # Ép nến đẩy phải có Volume > WHALE_VOL_MULTIPLIER * AvgVol
ENABLE_FILTER_SWEEP    = False  # Bắt buộc OB phải quét đỉnh/đáy 15 nến trước đó
ENABLE_FILTER_PD_ARRAY = False  # Bắt buộc điểm vào lệnh phải nằm ở Premium/Discount hợp lý

# ------------------------------------------
# CÔNG TẮC HỆ THỐNG
# ------------------------------------------
ENABLE_ORDER_ANTISPAM = True
ENABLE_HEARTBEAT = True
ENABLE_KILLZONES = False

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ==========================================
# --- HÀM TIỆN ÍCH (UTILS) ---
# ==========================================
def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for _ in range(retries):
                try: 
                    return func(*args, **kwargs)
                except Exception as e:
                    print(f"Lỗi API: {e}. Thử lại sau {delay}s...")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

# ==========================================
# --- 2. STATE MANAGER (Bộ nhớ GitHub Gist) ---
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
        
        # Load và ÉP KIỂU dứt khoát thành List để chống lỗi Dict append
        loaded_state = self.load()
        self.state = loaded_state if isinstance(loaded_state, list) else []
        
    def load(self):
        # Nếu không có Token GitHub, ưu tiên đọc file local (khi test trên máy tính)
        if not self.github_token or not self.gist_id:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except:
                    return []
            return []
            
        # Đọc dữ liệu từ GitHub Gist
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                gist_data = response.json()
                content = gist_data['files'].get(self.filename, {}).get('content', '[]')
                data = json.loads(content)
                return data if isinstance(data, list) else []
            else:
                print(f"Không thể đọc Gist. Status Code: {response.status_code}")
        except Exception as e:
            print(f"Lỗi kết nối Gist (Load): {e}")
        return []

    def save(self, new_item):
        # Bảo vệ kép
        if not isinstance(self.state, list):
            self.state = []
            
        self.state.append(new_item)
        
        # Giới hạn bộ nhớ 50 setup gần nhất để file Gist không bị phình to
        if len(self.state) > 50:
            self.state.pop(0)
            
        # Nếu không có Token, lưu file local
        if not self.github_token or not self.gist_id:
            with open(self.filename, 'w') as f:
                json.dump(self.state, f)
            return
            
        # Ghi đè dữ liệu lên GitHub Gist
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {
                "files": {
                    self.filename: {"content": json.dumps(self.state)}
                }
            }
            response = requests.patch(url, headers=self.headers, json=payload)
            if response.status_code != 200:
                print(f"Lỗi ghi Gist. Status Code: {response.status_code}")
        except Exception as e:
            print(f"Lỗi kết nối Gist (Save): {e}")

# ==========================================
# --- 3. MARKET REGIME AGENT ---
# ==========================================
class MarketRegimeAgent:
    def __init__(self, exchange_api):
        self.exchange = exchange_api

    @retry_api()
    def get_data(self, symbol, timeframe, limit=300):
        bars = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts','open','high','low','close','vol'])
        
        # Cắt bỏ nến hiện tại đang chạy để chống Repainting
        df = df.iloc[:-1].copy() 
        
        df['atr'] = self._calculate_atr(df)
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        return df

    def _calculate_atr(self, df, length=14):
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(length).mean()

    def analyze_trend(self, df):
        if df is None or len(df) < 200: return "UNKNOWN"
        close, ema200 = df['close'].iloc[-1], df['ema200'].iloc[-1]
        
        # Dùng buffer 0.2% lọc nhiễu sideway
        if close > ema200 * 1.002: return "UP"
        if close < ema200 * 0.998: return "DOWN"
        return "SIDEWAY"

# ==========================================
# --- 4. SIGNAL AGENT (Lõi SMC + Scoring) ---
# ==========================================
class SignalAgent:
    def __init__(self):
        self.displacement_ratio = 0.45

    def check_displacement(self, df, idx, direction):
        candle = df.iloc[idx]
        body = abs(candle['close'] - candle['open'])
        total = candle['high'] - candle['low']
        if total == 0 or body < total * self.displacement_ratio: return False
        return (direction == "UP" and candle['close'] > candle['open']) or \
               (direction == "DOWN" and candle['close'] < candle['open'])

    def check_fvg(self, df, idx):
        if idx + 2 >= len(df): return False, None
        if df['low'].iloc[idx + 2] > df['high'].iloc[idx]: return True, "bullish"
        if df['high'].iloc[idx + 2] < df['low'].iloc[idx]: return True, "bearish"
        return False, None

    def scan_for_setups(self, symbol, df, trend, state_manager):
        if trend == "UNKNOWN": return None

        atr, current_close, last_idx = df['atr'].iloc[-1], df['close'].iloc[-1], len(df) - 1
        
        # Tính Premium/Discount dựa trên 50 nến gần nhất
        swing_high, swing_low = df['high'].iloc[-50:].max(), df['low'].iloc[-50:].min()
        swing_range = swing_high - swing_low
        eq = swing_high - (swing_range * 0.5) if swing_range > 0 else 0
        
        max_bars_lookback = MAX_BARS_LIMITS.get(TIMEFRAME, 50)
        
        for i in range(len(df) - max_bars_lookback, len(df) - 2):
            setup_id = f"{symbol}_{df['ts'].iloc[i]}_{trend}"
            
            # Anti-spam: Nếu đã ghi vào Gist thì bỏ qua, không báo lại nữa
            if ENABLE_ORDER_ANTISPAM and setup_id in state_manager.state:
                continue

            direction = None
            if trend in ["UP", "SIDEWAY"] and df['close'].iloc[i] < df['open'].iloc[i]:
                direction = "BUY"
            elif trend in ["DOWN", "SIDEWAY"] and df['close'].iloc[i] > df['open'].iloc[i]:
                direction = "SELL"

            if direction and self.check_displacement(df, i+1, direction):
                has_fvg, fvg_dir = self.check_fvg(df, i)
                if (direction == "BUY" and fvg_dir == "bullish") or (direction == "SELL" and fvg_dir == "bearish"):
                    
                    entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                    
                    # --- HỆ THỐNG CHẤM ĐIỂM (SCORING) ---
                    score = 3 # Điểm gốc cho Setup OB + FVG hợp lệ
                    
                    # 1. Điểm Sweep (Quét thanh khoản)
                    is_sweep = False
                    if direction == "BUY": is_sweep = df['low'].iloc[i] <= df['low'].iloc[max(0, i-15):i].min()
                    else: is_sweep = df['high'].iloc[i] >= df['high'].iloc[max(0, i-15):i].max()
                    
                    if is_sweep: score += 1
                    elif ENABLE_FILTER_SWEEP: continue # Bị chặn bởi Hard Filter
                    
                    # 2. Điểm Volume (Cá mập)
                    has_vol = False
                    avg_vol = df['vol'].iloc[max(0, i-19):i+1].mean()
                    if df['vol'].iloc[i+1] >= avg_vol * WHALE_VOL_MULTIPLIER: has_vol = True
                        
                    if has_vol: score += 1
                    elif ENABLE_FILTER_VOLUME: continue
                    
                    # 3. Điểm PD Array (Vị thế đẹp)
                    is_optimal_pd = False
                    if swing_range > 0:
                        if trend == "UP" and entry <= eq: is_optimal_pd = True
                        elif trend == "DOWN" and entry >= eq: is_optimal_pd = True
                        elif trend == "SIDEWAY":
                            if direction == "BUY" and entry <= swing_low + (swing_range * 0.3): is_optimal_pd = True
                            if direction == "SELL" and entry >= swing_high - (swing_range * 0.3): is_optimal_pd = True
                            
                    if is_optimal_pd: score += 1
                    elif ENABLE_FILTER_PD_ARRAY: continue

                    # Lọc theo MIN_SCORE
                    if score < MIN_SCORE: continue

                    # --- TÍNH TOÁN RỦI RO & TP ---
                    if direction == "BUY":
                        sl = df['low'].iloc[i] - (atr * SL_ATR_MULTIPLIER)
                        if (df['low'].iloc[i+2:] <= entry).any(): continue # Loại bỏ ngay nếu giá đã Mitigation
                    else:
                        sl = df['high'].iloc[i] + (atr * SL_ATR_MULTIPLIER)
                        if (df['high'].iloc[i+2:] >= entry).any(): continue

                    risk = abs(entry - sl)
                    tp1 = entry + risk * 1.5 if direction == "BUY" else entry - risk * 1.5
                    tp2 = entry + risk * 2.5 if direction == "BUY" else entry - risk * 2.5
                        
                    # Lưu bộ nhớ và xuất tín hiệu
                    state_manager.save(setup_id)
                    return {
                        "direction": direction, "entry": entry, "sl": sl, 
                        "tp1": tp1, "tp2": tp2, "score": score,
                        "type": f"{'Bullish' if direction=='BUY' else 'Bearish'} OB+FVG", 
                        "age": last_idx - i, "price": current_close
                    }
        return None

# ==========================================
# --- 5. EXECUTION AGENT (Telegram Báo Cáo) ---
# ==========================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN:
            print(f"[{symbol}] Có lệnh nhưng chưa cài TELEGRAM_TOKEN để gửi.")
            return

        icon = "🟢" if signal['direction'] == "BUY" else "🔴"
        
        # Đánh giá khoảng cách giá hiện tại đến Entry
        dist = abs(signal['price'] - signal['entry']) / signal['entry'] * 100
        dist_status = f"Cách Entry {dist:.2f}%"
        if dist < 0.1:
            dist_status = "🔥 Cực sát vùng Entry!"
            
        stars = "⭐" * signal['score']
        
        msg = f"""
{icon} <b>SMC SIGNAL {TIMEFRAME} | {symbol}</b>
───────────────
<b>Setup:</b> {signal['type']}
<b>Điểm chất lượng:</b> {signal['score']}/6 {stars}
<b>Action:</b> Limit {signal['direction']}
<b>Tuổi Setup:</b> {signal['age']} nến
<b>Trạng thái:</b> {dist_status}

<b>Entry:</b> {signal['entry']:.
