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
# --- 4. SIGNAL AGENT (Lõi SMC + Confirmation) ---
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

    def check_confirmation(self, df, direction, ob_high, ob_low, ob_idx):
        """Hàm kiểm tra xác nhận tại vùng POI"""
        latest_idx = len(df) - 1
        latest_candle = df.iloc[latest_idx]

        # Kiểm tra từ lúc sinh ra OB đến hiện tại
        action_df = df.iloc[ob_idx+2 : latest_idx+1]
        if len(action_df) == 0: return False, None

        # 1. Kiểm tra giá đã chạm vùng POI chưa và có bị phá vỡ hẳn không?
        if direction == "BUY":
            tapped = action_df['low'].min() <= ob_high
            invalidated = action_df['close'].min() < ob_low # Đóng nến thủng OB -> Bỏ
        else:
            tapped = action_df['high'].max() >= ob_low
            invalidated = action_df['close'].max() > ob_high # Đóng nến qua OB -> Bỏ

        if not tapped or invalidated:
            return False, None

        # 2. Điều kiện nến xác nhận (Nến vừa đóng)
        body = abs(latest_candle['close'] - latest_candle['open'])
        total = latest_candle['high'] - latest_candle['low']
        if total == 0: return False, None
        
        avg_vol = df['vol'].iloc[max(0, latest_idx-15):latest_idx].mean()
        has_vol = latest_candle['vol'] >= avg_vol * WHALE_VOL_MULTIPLIER

        is_reversal = False
        
        if direction == "BUY":
            lower_wick = min(latest_candle['open'], latest_candle['close']) - latest_candle['low']
            # Xác nhận: Rút chân dài ở vùng dưới HOẶC nến xanh có Vol lớn
            is_sweep = latest_candle['low'] < ob_low and latest_candle['close'] >= ob_low
            if (lower_wick > body * 1.2 and has_vol) or is_sweep or (latest_candle['close'] > latest_candle['open'] and has_vol):
                is_reversal = True
        else:
            upper_wick = latest_candle['high'] - max(latest_candle['open'], latest_candle['close'])
            is_sweep = latest_candle['high'] > ob_high and latest_candle['close'] <= ob_high
            if (upper_wick > body * 1.2 and has_vol) or is_sweep or (latest_candle['close'] < latest_candle['open'] and has_vol):
                is_reversal = True

        if is_reversal:
            return True, latest_candle['close'] # Trả về giá Market để vào lệnh
            
        return False, None

    def scan_for_setups(self, symbol, df, trend, state_manager):
        if trend == "UNKNOWN": return None

        atr = df['atr'].iloc[-1]
        max_bars_lookback = MAX_BARS_LIMITS.get(TIMEFRAME, 50)
        
        for i in range(len(df) - max_bars_lookback, len(df) - 2):
            direction = None
            if trend in ["UP", "SIDEWAY"] and df['close'].iloc[i] < df['open'].iloc[i]: direction = "BUY"
            elif trend in ["DOWN", "SIDEWAY"] and df['close'].iloc[i] > df['open'].iloc[i]: direction = "SELL"

            if direction and self.check_displacement(df, i+1, direction):
                has_fvg, fvg_dir = self.check_fvg(df, i)
                if (direction == "BUY" and fvg_dir == "bullish") or (direction == "SELL" and fvg_dir == "bearish"):
                    
                    ob_high, ob_low = df['high'].iloc[i], df['low'].iloc[i]
                    setup_id = f"{symbol}_{df['ts'].iloc[i]}_{direction}"
                    
                    # Trạng thái 1: Chưa báo WATCHING -> Báo để rình
                    watch_id = "WATCH_" + setup_id
                    exec_id = "EXEC_" + setup_id
                    
                    # Tính SL nới rộng hơn chút cho an toàn
                    sl = ob_low - (atr * 0.5) if direction == "BUY" else ob_high + (atr * 0.5)
                    
                    # Kiểm tra xem có nến xác nhận Market không
                    is_confirmed, market_price = self.check_confirmation(df, direction, ob_high, ob_low, i)

                    if is_confirmed:
                        if ENABLE_ORDER_ANTISPAM and exec_id in state_manager.state:
                            continue # Đã bắn lệnh rồi thì thôi
                            
                        # Tính TP theo R:R thực tế từ giá Market
                        risk = abs(market_price - sl)
                        tp1 = market_price + risk * 1.5 if direction == "BUY" else market_price - risk * 1.5
                        tp2 = market_price + risk * 2.5 if direction == "BUY" else market_price - risk * 2.5
                        
                        state_manager.save(exec_id)
                        return {
                            "type": "EXECUTION", "direction": direction, "market_price": market_price,
                            "ob_high": ob_high, "ob_low": ob_low, "sl": sl, "tp1": tp1, "tp2": tp2,
                            "signal_name": f"{'Bullish' if direction=='BUY' else 'Bearish'} Confirmed Entry"
                        }
                    
                    elif not is_confirmed and (watch_id not in state_manager.state):
                        # Gửi cảnh báo theo dõi nếu chưa gửi
                        state_manager.save(watch_id)
                        return {
                            "type": "WATCHING", "direction": direction, 
                            "ob_high": ob_high, "ob_low": ob_low,
                            "signal_name": f"Dò thấy {'Bullish' if direction=='BUY' else 'Bearish'} POI"
                        }
        return None

# ==========================================
# --- 5. EXECUTION AGENT (Telegram) ---
# ==========================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN: return

        if signal['type'] == "WATCHING":
            icon = "👀"
            msg = f"""
{icon} <b>SMC WATCHING | {symbol} {TIMEFRAME}</b>
───────────────
<b>Trạng thái:</b> Đang rình vùng POI
<b>Action:</b> Canh {signal['direction']}
<b>Vùng quét (OB):</b> {signal['ob_low']:.4f} - {signal['ob_high']:.4f}

<i>*Bot sẽ tự động bắn lệnh Market nếu xuất hiện nến xác nhận có Volume / Sweep tại vùng này. Giữ im lặng...</i>
"""
        else:
            icon = "🚀" if signal['direction'] == "BUY" else "💥"
            msg = f"""
{icon} <b>SMC MARKET EXECUTION | {symbol} {TIMEFRAME}</b>
───────────────
<b>Xác nhận:</b> Volume/Sweep đảo chiều thành công!
<b>Action:</b> VÀO LỆNH MARKET {signal['direction']} NGAY

<b>Giá Market (Entry):</b> {signal['market_price']:.4f}
<b>Stoploss:</b> {signal['sl']:.4f}
───────────────
<b>🎯 TP1 (RR 1:1.5):</b> {signal['tp1']:.4f}
<b>🎯 TP2 (RR 1:2.5):</b> {signal['tp2']:.4f}
"""
        
        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e:
                pass

    def send_text(self, text):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN:
            return

        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
                if response.status_code != 200:
                    print(f"Lỗi khi gửi text cho ID {chat_id}: {response.text}")
            except Exception as e:
                print(f"Lỗi gửi Tele text đến {chat_id}: {e}")

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
            if signal['type'] == 'WATCHING':
                print(f">>> [MỚI] Dò thấy vùng POI cho {symbol}. Đưa vào danh sách theo dõi.")
            else:
                print(f">>> [XÁC NHẬN] Có tín hiệu đảo chiều {symbol}. Bắn lệnh Market ngay!")
                
            execution_agent.send_telegram(symbol, signal)
            
    if signals_found == 0:
        print("Không có tín hiệu nào thỏa mãn lúc này.")
        
    if ENABLE_HEARTBEAT:
        heartbeat_msg = f"⏱ <b>[{run_time}]</b> Hệ thống SMC Bot vẫn đang hoạt động ổn định."
        print(heartbeat_msg)
        execution_agent.send_text(heartbeat_msg)

if __name__ == "__main__":
    main()
