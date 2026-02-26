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

SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.5
MIN_SCORE = 4
MAX_BARS_LIMITS = {'15m': 35, '1h': 80, '4h': 55}

# ------------------------------------------
# 🔴 CÔNG TẮC BỘ LỌC ÉP BUỘC (HARD FILTERS)
# Nếu False: Chuyển sang chấm điểm mềm (Soft Score).
# Nếu True: Thiếu điều kiện này là hủy lệnh luôn.
# ------------------------------------------
ENABLE_FILTER_VOLUME   = False  
ENABLE_FILTER_SWEEP    = False  
ENABLE_FILTER_PD_ARRAY = False  

# ------------------------------------------
# CÔNG TẮC HỆ THỐNG
# ------------------------------------------
ENABLE_ORDER_ANTISPAM = True
ENABLE_HEARTBEAT = True

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

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
        
        # FIX LỖI DICT: Ép kiểu đảm bảo luôn là List
        loaded_state = self.load()
        self.state = loaded_state if isinstance(loaded_state, list) else []
        
    def load(self):
        if not self.github_token or not self.gist_id:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except: return []
            return []
            
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                gist_data = response.json()
                content = gist_data['files'].get(self.filename, {}).get('content', '[]')
                data = json.loads(content)
                return data if isinstance(data, list) else []
        except Exception as e:
            print(f"Lỗi Load Gist: {e}")
        return []

    def save(self, new_item):
        if not isinstance(self.state, list): self.state = []
        self.state.append(new_item)
        if len(self.state) > 50: self.state.pop(0)
            
        if not self.github_token or not self.gist_id:
            with open(self.filename, 'w') as f:
                json.dump(self.state, f)
            return
            
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {"files": {self.filename: {"content": json.dumps(self.state)}}}
            requests.patch(url, headers=self.headers, json=payload)
        except Exception as e:
            print(f"Lỗi Save Gist: {e}")

# ==========================================
# --- 3. MARKET REGIME AGENT ---
# ==========================================
class MarketRegimeAgent:
    def __init__(self, exchange_api):
        self.exchange = exchange_api

    def get_data(self, symbol, timeframe, limit=300):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=['ts','open','high','low','close','vol'])
            df = df.iloc[:-1].copy() # Chống repainting
            df['atr'] = self._calculate_atr(df)
            df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
            return df
        except Exception as e:
            print(f"Lỗi tải data {symbol}: {e}")
            return None

    def _calculate_atr(self, df, length=14):
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(length).mean()

    def analyze_trend(self, df):
        if df is None or len(df) < 200: return "UNKNOWN"
        close, ema200 = df['close'].iloc[-1], df['ema200'].iloc[-1]
        if close > ema200 * 1.002: return "UP"
        if close < ema200 * 0.998: return "DOWN"
        return "SIDEWAY"

# ==========================================
# --- 4. SIGNAL AGENT (Chấm điểm thông minh) ---
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
        swing_high, swing_low = df['high'].iloc[-50:].max(), df['low'].iloc[-50:].min()
        swing_range = swing_high - swing_low
        eq = swing_high - (swing_range * 0.5) if swing_range > 0 else 0
        
        max_bars = MAX_BARS_LIMITS.get(TIMEFRAME, 50)
        
        for i in range(len(df) - max_bars, len(df) - 2):
            setup_id = f"{symbol}_{df['ts'].iloc[i]}_{trend}"
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
                        if (df['low'].iloc[i+2:] <= entry).any(): continue # Loại bỏ Mitigated
                    else:
                        sl = df['high'].iloc[i] + (atr * SL_ATR_MULTIPLIER)
                        if (df['high'].iloc[i+2:] >= entry).any(): continue

                    risk = abs(entry - sl)
                    tp1 = entry + risk * 1.5 if direction == "BUY" else entry - risk * 1.5
                    tp2 = entry + risk * 2.5 if direction == "BUY" else entry - risk * 2.5
                        
                    state_manager.save(setup_id)
                    return {
                        "direction": direction, "entry": entry, "sl": sl, 
                        "tp1": tp1, "tp2": tp2, "score": score,
                        "type": f"{'Bullish' if direction=='BUY' else 'Bearish'} OB+FVG", 
                        "age": last_idx - i, "price": current_close
                    }
        return None

# ==========================================
# --- 5. EXECUTION AGENT (Telegram) ---
# ==========================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN: return

        icon = "🟢" if signal['direction'] == "BUY" else "🔴"
        dist = abs(signal['price'] - signal['entry']) / signal['entry'] * 100
        stars = "⭐" * signal['score']
        
        msg = f"""
{icon} <b>SMC SIGNAL {TIMEFRAME} | {symbol}</b>
───────────────
<b>Setup:</b> {signal['type']}
<b>Điểm chất lượng:</b> {signal['score']}/6 {stars}
<b>Action:</b> Limit {signal['direction']}
<b>Tuổi Setup:</b> {signal['age']} nến
<b>Trạng thái:</b> Cách Entry {dist:.2f}%

<b>Entry:</b> {signal['entry']:.4f}
<b>Stoploss:</b> {signal['sl']:.4f}
───────────────
<b>🎯 TP1 (RR 1:1.5):</b> {signal['tp1']:.4f}
<b>🎯 TP2 (RR 1:2.5):</b> {signal['tp2']:.4f}

<b>Giá hiện tại:</b> {signal['price']:.4f}
"""
        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e: print(f"Lỗi gửi Tele đến {chat_id}: {e}")

# ==========================================
# --- 6. MAIN WORKFLOW ---
# ==========================================
def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Khởi chạy Bot SMC Signal...")
    
    state_manager = GistStateManager()
    regime_agent = MarketRegimeAgent(exchange)
    signal_agent = SignalAgent()
    execution_agent = ExecutionAgent()
    
    signals_found = 0

    for symbol in PAIRS:
        df = regime_agent.get_data(symbol, TIMEFRAME)
        if df is None: continue
            
        trend = regime_agent.analyze_trend(df)
        print(f"[{symbol}] Đang quét... Trend 4H/1D: {trend}")
        
        signal = signal_agent.scan_for_setups(symbol, df, trend, state_manager)
        
        if signal:
            signals_found += 1
            print(f">>> Vừa nổ tín hiệu {signal['score']} SAO cho {symbol}!")
            execution_agent.send_telegram(symbol, signal)
            
    if signals_found == 0:
        print("Không có tín hiệu nào thỏa mãn lúc này.")

if __name__ == "__main__":
    main()
