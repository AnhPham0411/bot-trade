import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import json
from datetime import datetime

# ==========================================
# --- 1. CẤU HÌNH & BẢNG ĐIỀU KHIỂN BỘ LỌC ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAME = '1h'

# Các thông số rủi ro & tính toán
SL_ATR_MULTIPLIER = 1.5
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.5 # Đã hạ xuống mức thực tế hơn
MIN_SCORE = 4
MAX_BARS_LIMITS = {'15m': 35, '1h': 36, '4h': 30} # Đã chỉnh lại tuổi thọ hợp lý

# ------------------------------------------
# 🔴 CÔNG TẮC BỘ LỌC (BẬT/TẮT ĐỂ DEBUG) 🔴
# Đang để FALSE hết để đảm bảo bot "Nổ lệnh". Bạn bật TRUE dần từng cái để test.
# ------------------------------------------
ENABLE_FILTER_VOLUME   = False  # Yêu cầu nến đẩy phải có Volume đột biến
ENABLE_FILTER_SWEEP    = False  # Yêu cầu OB phải quét thanh khoản đỉnh/đáy 15 nến
ENABLE_FILTER_PD_ARRAY = False  # Yêu cầu giá phải ở Premium (Bán) / Discount (Mua)

# Công tắc hệ thống
ENABLE_ORDER_ANTISPAM = True

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ==========================================
# --- 2. STATE MANAGER (Bộ Nhớ GitHub Gist) ---
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
        if not self.github_token or not self.gist_id:
            # Fallback lưu local nếu test trên máy tính
            if os.path.exists(self.filename):
                with open(self.filename, 'r') as f:
                    return json.load(f)
            return []
            
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                gist_data = response.json()
                content = gist_data['files'].get(self.filename, {}).get('content', '[]')
                return json.loads(content)
        except Exception as e:
            print(f"Lỗi đọc Gist: {e}")
        return []

    def save(self, new_item):
        self.state.append(new_item)
        # Giữ bộ nhớ nhẹ nhàng (50 lệnh gần nhất)
        if len(self.state) > 50:
            self.state.pop(0)
            
        if not self.github_token or not self.gist_id:
            with open(self.filename, 'w') as f:
                json.dump(self.state, f)
            return
            
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {
                "files": {
                    self.filename: {"content": json.dumps(self.state)}
                }
            }
            requests.patch(url, headers=self.headers, json=payload)
        except Exception as e:
            print(f"Lỗi ghi Gist: {e}")

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
            df = df.iloc[:-1].copy() # Bỏ nến chưa đóng
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
        swing_high, swing_low = df['high'].iloc[-50:].max(), df['low'].iloc[-50:].min()
        eq = swing_high - ((swing_high - swing_low) * 0.5) if swing_high > swing_low else 0
        
        for i in range(len(df) - MAX_BARS_LIMITS[TIMEFRAME], len(df) - 2):
            setup_id = f"{symbol}_{df['ts'].iloc[i]}_{trend}"
            
            # Anti-spam: Đã báo rồi thì bỏ qua
            if ENABLE_ORDER_ANTISPAM and setup_id in state_manager.state:
                continue

            # ================= SETUP BUY =================
            if trend in ["UP", "SIDEWAY"] and df['close'].iloc[i] < df['open'].iloc[i]:
                if ENABLE_FILTER_SWEEP and df['low'].iloc[i] > df['low'].iloc[max(0, i-15):i].min():
                    continue

                if self.check_displacement(df, i+1, "UP"):
                    has_fvg, fvg_dir = self.check_fvg(df, i)
                    if has_fvg and fvg_dir == "bullish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        
                        if ENABLE_FILTER_PD_ARRAY:
                            if trend == "SIDEWAY" and entry > swing_low + ((swing_high - swing_low) * 0.3): continue
                            if trend == "UP" and entry > eq: continue
                        
                        sl = df['low'].iloc[i] - (atr * SL_ATR_MULTIPLIER)
                        risk = entry - sl
                        
                        # Loại bỏ nếu giá đã Mitigated (chạm lại entry)
                        if (df['low'].iloc[i+2:] <= entry).any(): continue 
                            
                        state_manager.save(setup_id)
                        return {
                            "direction": "BUY", "entry": entry, "sl": sl, 
                            "tp1": entry + risk * 1.5, "tp2": entry + risk * 2.5,
                            "type": "Bullish OB+FVG", "age": last_idx - i, "price": current_close
                        }

            # ================= SETUP SELL =================
            elif trend in ["DOWN", "SIDEWAY"] and df['close'].iloc[i] > df['open'].iloc[i]:
                if ENABLE_FILTER_SWEEP and df['high'].iloc[i] < df['high'].iloc[max(0, i-15):i].max():
                    continue

                if self.check_displacement(df, i+1, "DOWN"):
                    has_fvg, fvg_dir = self.check_fvg(df, i)
                    if has_fvg and fvg_dir == "bearish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        
                        if ENABLE_FILTER_PD_ARRAY:
                            if trend == "SIDEWAY" and entry < swing_high - ((swing_high - swing_low) * 0.3): continue
                            if trend == "DOWN" and entry < eq: continue
                        
                        sl = df['high'].iloc[i] + (atr * SL_ATR_MULTIPLIER)
                        risk = sl - entry
                        
                        # Loại bỏ nếu giá đã Mitigated
                        if (df['high'].iloc[i+2:] >= entry).any(): continue
                            
                        state_manager.save(setup_id)
                        return {
                            "direction": "SELL", "entry": entry, "sl": sl, 
                            "tp1": entry - risk * 1.5, "tp2": entry - risk * 2.5,
                            "type": "Bearish OB+FVG", "age": last_idx - i, "price": current_close
                        }
        return None

# ==========================================
# --- 5. EXECUTION AGENT (Báo Telegram) ---
# ==========================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN:
            print(f"[{symbol}] Có lệnh nhưng chưa cài TELEGRAM_TOKEN")
            return

        icon = "🟢" if signal['direction'] == "BUY" else "🔴"
        dist = abs(signal['price'] - signal['entry']) / signal['entry'] * 100
        
        msg = f"""
{icon} <b>SMC SIGNAL {TIMEFRAME} | {symbol}</b>
───────────────
<b>Action:</b> Limit {signal['direction']}
<b>Tuổi Setup:</b> {signal['age']} nến
<b>Trạng thái:</b> Cách Entry {dist:.2f}%

<b>Entry:</b> {signal['entry']:.4f}
<b>Stoploss:</b> {signal['sl']:.4f}
───────────────
<b>🎯 TP1 (RR 1:1.5):</b> {signal['tp1']:.
