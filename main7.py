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
# 🔴 CÔNG TẮC BỘ LỌC (TẮT ĐỂ DEBUG 0 LỆNH) 🔴
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
        self.state = self.load()
        
    def load(self):
        # Nếu không có Token GitHub, ưu tiên đọc file local (khi test trên máy tính)
        if not self.github_token or not self.gist_id:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        return json.load(f)
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
                return json.loads(content)
            else:
                print(f"Không thể đọc Gist. Status Code: {response.status_code}")
        except Exception as e:
            print(f"Lỗi kết nối Gist (Load): {e}")
        return []

    def save(self, new_item):
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
# --- 4. SIGNAL AGENT (Lõi SMC + Toggles) ---
# ==========================================
class SignalAgent:
    def __init__(self):
        self.displacement_ratio = 0.45

    def check_displacement(self, df, idx, direction):
        candle = df.iloc[idx]
        body = abs(candle['close'] - candle['open'])
        total = candle['high'] - candle['low']
        if total == 0 or body < total * self.displacement_ratio: return False
            
        if ENABLE_FILTER_VOLUME:
            avg_vol = df['vol'].iloc[max(0, idx-20):idx].mean()
            if candle['vol'] < avg_vol * WHALE_VOL_MULTIPLIER: return False

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

            # ================= SETUP BUY =================
            if trend in ["UP", "SIDEWAY"] and df['close'].iloc[i] < df['open'].iloc[i]:
                # Bộ lọc Sweep
                if ENABLE_FILTER_SWEEP and df['low'].iloc[i] > df['low'].iloc[max(0, i-15):i].min():
                    continue

                if self.check_displacement(df, i+1, "UP"):
                    has_fvg, fvg_dir = self.check_fvg(df, i)
                    if has_fvg and fvg_dir == "bullish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        
                        # Bộ lọc Vị thế (Premium/Discount Array)
                        if ENABLE_FILTER_PD_ARRAY and swing_range > 0:
                            if trend == "SIDEWAY" and entry > swing_low + (swing_range * 0.3): continue
                            if trend == "UP" and entry > eq: continue
                        
                        sl = df['low'].iloc[i] - (atr * SL_ATR_MULTIPLIER)
                        risk = entry - sl
                        
                        # Loại bỏ ngay lập tức nếu giá đã Mitigation (chạm vào entry rồi)
                        recent_lows = df['low'].iloc[i+2:]
                        if (recent_lows <= entry).any(): continue 
                            
                        # Lưu bộ nhớ và xuất tín hiệu
                        state_manager.save(setup_id)
                        return {
                            "direction": "BUY", "entry": entry, "sl": sl, 
                            "tp1": entry + risk * 1.5, "tp2": entry + risk * 2.5,
                            "type": "Bullish OB + FVG", "age": last_idx - i, "price": current_close
                        }

            # ================= SETUP SELL =================
            elif trend in ["DOWN", "SIDEWAY"] and df['close'].iloc[i] > df['open'].iloc[i]:
                # Bộ lọc Sweep
                if ENABLE_FILTER_SWEEP and df['high'].iloc[i] < df['high'].iloc[max(0, i-15):i].max():
                    continue

                if self.check_displacement(df, i+1, "DOWN"):
                    has_fvg, fvg_dir = self.check_fvg(df, i)
                    if has_fvg and fvg_dir == "bearish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        
                        # Bộ lọc Vị thế (Premium/Discount Array)
                        if ENABLE_FILTER_PD_ARRAY and swing_range > 0:
                            if trend == "SIDEWAY" and entry < swing_high - (swing_range * 0.3): continue
                            if trend == "DOWN" and entry < eq: continue
                        
                        sl = df['high'].iloc[i] + (atr * SL_ATR_MULTIPLIER)
                        risk = sl - entry
                        
                        # Loại bỏ ngay lập tức nếu giá đã Mitigation
                        recent_highs = df['high'].iloc[i+2:]
                        if (recent_highs >= entry).any(): continue
                            
                        # Lưu bộ nhớ và xuất tín hiệu
                        state_manager.save(setup_id)
                        return {
                            "direction": "SELL", "entry": entry, "sl": sl, 
                            "tp1": entry - risk * 1.5, "tp2": entry - risk * 2.5,
                            "type": "Bearish OB + FVG", "age": last_idx - i, "price": current_close
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
        
        msg = f"""
{icon} <b>SMC SIGNAL {TIMEFRAME} | {symbol}</b>
───────────────
<b>Action:</b> Limit {signal['direction']}
<b>Tuổi Setup:</b> {signal['age']} nến
<b>Trạng thái:</b> {dist_status}

<b>Entry:</b> {signal['entry']:.4f}
<b>Stoploss:</b> {signal['sl']:.4f}
───────────────
<b>🎯 TP1 (RR 1:1.5):</b> {signal['tp1']:.4f} (Chốt 1/2 vị thế)
<b>🎯 TP2 (RR 1:2.5):</b> {signal['tp2']:.4f}

<b>Giá hiện tại:</b> {signal['price']:.4f}
<i>Bot Trigger: {signal['type']}</i>
"""
        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                response = requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
                if response.status_code != 200:
                    print(f"Lỗi khi gửi cho ID {chat_id}: {response.text}")
            except Exception as e:
                print(f"Lỗi gửi Tele đến {chat_id}: {e}")

# ==========================================
# --- 6. HỆ THỐNG VẬN HÀNH CHÍNH (MAIN) ---
# ==========================================
def main():
    run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{run_time}] Khởi chạy Bot SMC Signal Đa Tác Vụ...")
    
    state_manager = GistStateManager()
    regime_agent = MarketRegimeAgent(exchange)
    signal_agent = SignalAgent()
    execution_agent = ExecutionAgent()
    
    signals_found = 0

    for symbol in PAIRS:
        df = regime_agent.get_data(symbol, TIMEFRAME)
        if df is None:
            continue
            
        trend = regime_agent.analyze_trend(df)
        print(f"[{symbol}] Đang quét... Trend 4H/1D: {trend}")
        
        signal = signal_agent.scan_for_setups(symbol, df, trend, state_manager)
        
        if signal:
            signals_found += 1
            print(f">>> Vừa nổ tín hiệu {signal['direction']} cho {symbol}. Đang gửi Telegram...")
            execution_agent.send_telegram(symbol, signal)
            
    if signals_found == 0:
        print("Không có tín hiệu nào thỏa mãn lúc này.")
        
    if ENABLE_HEARTBEAT:
        print(f"[{run_time}] Hệ thống vẫn đang chạy ổn định (Heartbeat OK).")

if __name__ == "__main__":
    main()
