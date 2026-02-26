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
TIMEFRAMES = ['15m', '1h', '4h']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.8
MIN_SCORE = 4
MAX_BARS_LIMITS = {'15m': 35, '1h': 80, '4h': 55, '1d': 40}

# ------------------------------------------
# 🔴 CÔNG TẮC BỘ LỌC ÉP BUỘC (HARD FILTERS)
# ------------------------------------------
ENABLE_FILTER_VOLUME   = False  
ENABLE_FILTER_SWEEP    = False  
ENABLE_FILTER_PD_ARRAY = False  

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
        
        loaded_state = self.load()
        self.state = loaded_state if isinstance(loaded_state, list) else []
        
    def load(self):
        if not self.github_token or not self.gist_id:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except:
                    return []
            return []
            
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
        if not isinstance(self.state, list):
            self.state = []
            
        self.state.append(new_item)
        
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
        
        if close > ema200 * 1.002: return "UP"
        if close < ema200 * 0.998: return "DOWN"
        return "SIDEWAY"

# ==========================================
# --- 4. SIGNAL AGENT (Lõi SMC + MTF) ---
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
        """Hàm kiểm tra xác nhận Sweep/Volume tại vùng POI"""
        latest_idx = len(df) - 1
        latest_candle = df.iloc[latest_idx]

        action_df = df.iloc[ob_idx+2 : latest_idx+1]
        if len(action_df) == 0: return False, None

        if direction == "BUY":
            tapped = action_df['low'].min() <= ob_high
            invalidated = action_df['close'].min() < ob_low 
        else:
            tapped = action_df['high'].max() >= ob_low
            invalidated = action_df['close'].max() > ob_high 

        if not tapped or invalidated:
            return False, None

        body = abs(latest_candle['close'] - latest_candle['open'])
        total = latest_candle['high'] - latest_candle['low']
        if total == 0: return False, None
        
        avg_vol = df['vol'].iloc[max(0, latest_idx-15):latest_idx].mean()
        has_vol = latest_candle['vol'] >= avg_vol * WHALE_VOL_MULTIPLIER

        is_reversal = False
        
        if direction == "BUY":
            lower_wick = min(latest_candle['open'], latest_candle['close']) - latest_candle['low']
            is_sweep = latest_candle['low'] < ob_low and latest_candle['close'] >= ob_low
            if (lower_wick > body * 1.2 and has_vol) or is_sweep or (latest_candle['close'] > latest_candle['open'] and has_vol):
                is_reversal = True
        else:
            upper_wick = latest_candle['high'] - max(latest_candle['open'], latest_candle['close'])
            is_sweep = latest_candle['high'] > ob_high and latest_candle['close'] <= ob_high
            if (upper_wick > body * 1.2 and has_vol) or is_sweep or (latest_candle['close'] < latest_candle['open'] and has_vol):
                is_reversal = True

        if is_reversal:
            return True, latest_candle['close'] 
            
        return False, None

    def scan_mtf_setups(self, symbol, df_ltf, df_htf, ltf_str, htf_str, trend_htf, state_manager):
        if trend_htf == "UNKNOWN": return None

        # --- BƯỚC 1: TÌM POI Ở KHUNG LỚN (HTF) ---
        htf_pois = []
        max_bars_htf = MAX_BARS_LIMITS.get(htf_str, 50)
        
        for i in range(len(df_htf) - max_bars_htf, len(df_htf) - 2):
            direction = None
            if trend_htf in ["UP", "SIDEWAY"] and df_htf['close'].iloc[i] < df_htf['open'].iloc[i]: direction = "BUY"
            elif trend_htf in ["DOWN", "SIDEWAY"] and df_htf['close'].iloc[i] > df_htf['open'].iloc[i]: direction = "SELL"
            
            if direction and self.check_displacement(df_htf, i+1, direction):
                has_fvg, _ = self.check_fvg(df_htf, i)
                if has_fvg:
                    htf_pois.append({
                        "dir": direction, 
                        "high": df_htf['high'].iloc[i], 
                        "low": df_htf['low'].iloc[i]
                    })
                    
        if not htf_pois: return None # Không có cản cứng thì bỏ qua

        # --- BƯỚC 2: TÌM POI KHUNG NHỎ (LTF) VÀ CHECK LỒNG NHAU ---
        atr_ltf = df_ltf['atr'].iloc[-1]
        max_bars_ltf = MAX_BARS_LIMITS.get(ltf_str, 35)
        
        for i in range(len(df_ltf) - max_bars_ltf, len(df_ltf) - 2):
            direction = None
            if trend_htf in ["UP", "SIDEWAY"] and df_ltf['close'].iloc[i] < df_ltf['open'].iloc[i]: direction = "BUY"
            elif trend_htf in ["DOWN", "SIDEWAY"] and df_ltf['close'].iloc[i] > df_ltf['open'].iloc[i]: direction = "SELL"

            if direction and self.check_displacement(df_ltf, i+1, direction):
                has_fvg, fvg_dir = self.check_fvg(df_ltf, i)
                if (direction == "BUY" and fvg_dir == "bullish") or (direction == "SELL" and fvg_dir == "bearish"):
                    
                    ob_high_ltf, ob_low_ltf = df_ltf['high'].iloc[i], df_ltf['low'].iloc[i]
                    
                    # Kiểm tra lồng nhau (Confluence)
                    is_nested = False
                    for poi in htf_pois:
                        if poi['dir'] == direction:
                            # POI LTF phải nằm đè lên hoặc lọt thỏm trong POI HTF
                            if (ob_high_ltf <= poi['high'] and ob_high_ltf >= poi['low']) or \
                               (ob_low_ltf >= poi['low'] and ob_low_ltf <= poi['high']):
                                is_nested = True
                                break
                                
                    if not is_nested:
                        continue # Đẹp mà lơ lửng thì cũng bỏ

                    setup_id = f"{symbol}_{df_ltf['ts'].iloc[i]}_{direction}_{ltf_str}"
                    watch_id = "WATCH_" + setup_id
                    exec_id = "EXEC_" + setup_id
                    
                    # Tính SL theo khung nhỏ (Tối ưu R:R)
                    sl = ob_low_ltf - (atr_ltf * 0.5) if direction == "BUY" else ob_high_ltf + (atr_ltf * 0.5)
                    
                    is_confirmed, market_price = self.check_confirmation(df_ltf, direction, ob_high_ltf, ob_low_ltf, i)

                    if is_confirmed:
                        if ENABLE_ORDER_ANTISPAM and exec_id in state_manager.state:
                            continue 
                            
                        risk = abs(market_price - sl)
                        tp1 = market_price + risk * 1.5 if direction == "BUY" else market_price - risk * 1.5
                        tp2 = market_price + risk * 3.0 if direction == "BUY" else market_price - risk * 3.0
                        
                        state_manager.save(exec_id)
                        return {
                            "type": "EXECUTION", "direction": direction, "market_price": market_price,
                            "ob_high": ob_high_ltf, "ob_low": ob_low_ltf, "sl": sl, "tp1": tp1, "tp2": tp2,
                            "signal_name": f"{'Bullish' if direction=='BUY' else 'Bearish'} Confirmed Entry",
                            "ltf": ltf_str, "htf": htf_str
                        }
                    
                    elif not is_confirmed and (watch_id not in state_manager.state):
                        state_manager.save(watch_id)
                        return {
                            "type": "WATCHING", "direction": direction, 
                            "ob_high": ob_high_ltf, "ob_low": ob_low_ltf,
                            "signal_name": f"Dò thấy {'Bullish' if direction=='BUY' else 'Bearish'} POI (Nested)",
                            "ltf": ltf_str, "htf": htf_str
                        }
        return None

# ==========================================
# --- 5. EXECUTION AGENT (Telegram) ---
# ==========================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN: return

        ltf, htf = signal.get('ltf'), signal.get('htf')

        if signal['type'] == "WATCHING":
            icon = "👀"
            msg = f"""
{icon} <b>SMC WATCHING | {symbol}</b>
───────────────
<b>Khung đánh:</b> {ltf} (Bảo kê bởi {htf})
<b>Trạng thái:</b> Đang rình vùng POI
<b>Action:</b> Canh {signal['direction']}
<b>Vùng quét (OB):</b> {signal['ob_low']:.4f} - {signal['ob_high']:.4f}

<i>*Bot sẽ tự động bắn lệnh Market nếu xuất hiện nến xác nhận tại vùng này. Giữ im lặng...</i>
"""
        else:
            icon = "🚀" if signal['direction'] == "BUY" else "💥"
            msg = f"""
{icon} <b>SMC MARKET EXECUTION | {symbol}</b>
───────────────
<b>Khung đánh:</b> {ltf} (Bảo kê bởi {htf})
<b>Xác nhận:</b> Volume/Sweep đảo chiều thành công!
<b>Action:</b> VÀO LỆNH MARKET {signal['direction']} NGAY

<b>Giá Market (Entry):</b> {signal['market_price']:.4f}
<b>Stoploss:</b> {signal['sl']:.4f}
───────────────
<b>🎯 TP1 (RR 1:1.5):</b> {signal['tp1']:.4f}
<b>🎯 TP2 (RR 1:3.0):</b> {signal['tp2']:.4f}
"""
        
        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
            except Exception:
                pass

    def send_text(self, text):
        if not TELEGRAM_TOKEN or "ĐIỀN" in TELEGRAM_TOKEN:
            return

        for chat_id in CHAT_IDS:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            except Exception:
                pass

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
        for ltf in TIMEFRAMES:
            htf = MTF_MAPPING.get(ltf)
            if not htf: continue
            
            df_ltf = regime_agent.get_data(symbol, ltf)
            df_htf = regime_agent.get_data(symbol, htf)
            
            if df_ltf is None or df_htf is None:
                continue
                
            trend_htf = regime_agent.analyze_trend(df_htf)
            print(f"[{symbol} | {ltf}/{htf}] Đang quét... Trend {htf}: {trend_htf}")
            
            signal = signal_agent.scan_mtf_setups(symbol, df_ltf, df_htf, ltf, htf, trend_htf, state_manager)
            
            if signal:
                signals_found += 1
                if signal['type'] == 'WATCHING':
                    print(f">>> [MỚI] Dò thấy vùng POI cho {symbol} ({ltf}). Đưa vào danh sách theo dõi.")
                else:
                    print(f">>> [XÁC NHẬN] Có tín hiệu đảo chiều {symbol} ({ltf}). Bắn lệnh Market ngay!")
                    
                execution_agent.send_telegram(symbol, signal)
                
    if signals_found == 0:
        print("Không có tín hiệu nào thỏa mãn lúc này.")
        
    if ENABLE_HEARTBEAT:
        heartbeat_msg = f"⏱ <b>[{run_time}]</b> Hệ thống SMC MTF Bot vẫn đang hoạt động ổn định."
        print(heartbeat_msg)
        execution_agent.send_text(heartbeat_msg)

if __name__ == "__main__":
    main()
