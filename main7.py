import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
import json
from datetime import datetime

# ======================================================================
# --- 1. USER CONFIGURATIONS (ONE-SHOT MODE) ---
# ======================================================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAMES = ['15m', '1h', '4h']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}
MAX_BARS_LIMITS = {'15m': 40, '1h': 80, '4h': 60, '1d': 45}

MIN_SCORE_ALERT = 4.0
MIN_SCORE_EXECUTE = 5.5
ENABLE_ORDER_ANTISPAM = True
ENABLE_HEARTBEAT = True
AGGRESSIVE_MODE = False          # True = entry nhanh hơn

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ======================================================================
# --- 2. STATE MANAGER (An toàn 100%) ---
# ======================================================================
class GistStateManager:
    def __init__(self, filename='bot_state.json'):
        self.filename = filename
        self.state = self.load()

    def load(self):
        try:
            with open(self.filename, 'r') as f:
                data = json.load(f)
                return data[-300:] if isinstance(data, list) else []
        except:
            return []

    def save(self, item):
        self.state.append(item)
        if len(self.state) > 300:
            self.state = self.state[-300:]
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"Lỗi save state: {e}")

# ======================================================================
# --- 3. MARKET REGIME AGENT ---
# ======================================================================
class MarketRegimeAgent:
    def __init__(self, exchange_api):
        self.exchange = exchange_api

    def get_data(self, symbol, timeframe, limit=300):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df = df.iloc[:-1].copy()
            df['atr'] = self._calculate_atr(df)
            df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
            return df
        except Exception as e:
            print(f"Lỗi lấy data {symbol} {timeframe}: {e}")
            return None

    def _calculate_atr(self, df, length=14):
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(length).mean()

    def analyze_trend(self, df):
        if df is None or len(df) < 200: return "UNKNOWN"
        close, ema = df['close'].iloc[-1], df['ema200'].iloc[-1]
        if close > ema * 1.002: return "UP"
        if close < ema * 0.998: return "DOWN"
        return "SIDEWAY"

# ======================================================================
# --- 4. SIGNAL AGENT (Giữ nguyên logic mạnh) ---
# ======================================================================
class SignalAgent:
    def __init__(self):
        self.displacement_ratio = 0.65
        self.min_displacement_atr = 1.15
        self.ote_low = 0.705
        self.ote_high = 0.79
        self.min_fvg_atr = 1.25

    def _calculate_atr(self, df, length=14):
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(length).mean()

    def check_fvg(self, df, idx):
        if idx + 2 >= len(df): return False, None, None, None
        low_p, high_p = df['low'].iloc[idx], df['high'].iloc[idx]
        low_f, high_f = df['low'].iloc[idx+2], df['high'].iloc[idx+2]
        if low_f > high_p: return True, "bullish", high_p, low_f
        if high_f < low_p: return True, "bearish", high_f, low_p
        return False, None, None, None

    def check_strong_displacement(self, df, idx, direction, atr_series):
        if idx >= len(df): return False
        candle = df.iloc[idx]
        body = abs(candle['close'] - candle['open'])
        total = candle['high'] - candle['low']
        if total == 0 or body < total * self.displacement_ratio: return False
        atr = atr_series.iloc[idx] if idx < len(atr_series) else 1
        if body < atr * self.min_displacement_atr: return False
        return (direction == "BUY" and candle['close'] > candle['open']) or \
               (direction == "SELL" and candle['close'] < candle['open'])

    def check_liquidity_sweep(self, df, idx, direction):
        lookback = df.iloc[max(0, idx-45):idx]
        liq = lookback['low'].min() if direction == "BUY" else lookback['high'].max()
        prev = df.iloc[idx-1]
        if direction == "BUY":
            return prev['low'] <= liq * 0.999 and prev['close'] > prev['open']
        else:
            return prev['high'] >= liq * 1.001 and prev['close'] < prev['open']

    def check_unicorn_breaker(self, df, idx, direction, fvg_bottom, fvg_top):
        lookback = df.iloc[max(0, idx-50):idx]
        for i in range(len(lookback)-1, max(0, len(lookback)-25), -1):
            c = lookback.iloc[i]
            if direction == "BUY" and c['close'] < c['open']:
                if fvg_bottom - 0.3*(fvg_top-fvg_bottom) <= c['high'] <= fvg_top:
                    return True
            elif direction == "SELL" and c['close'] > c['open']:
                if fvg_bottom <= c['low'] <= fvg_top + 0.3*(fvg_top-fvg_bottom):
                    return True
        return False

    def calculate_ote_score(self, df, idx, direction, fvg_bottom, fvg_top):
        start = max(0, idx-12)
        imp_low = df['low'].iloc[start:idx+3].min()
        imp_high = df['high'].iloc[start:idx+3].max()
        rng = imp_high - imp_low
        if rng <= 0: return 0
        if direction == "BUY":
            ote_t = imp_high - rng * self.ote_low
            ote_b = imp_high - rng * self.ote_high
            return 3 if fvg_top >= ote_b and fvg_bottom <= ote_t else 0
        else:
            ote_b = imp_low + rng * self.ote_low
            ote_t = imp_low + rng * self.ote_high
            return 3 if fvg_bottom <= ote_t and fvg_top >= ote_b else 0

    def calculate_setup_score(self, df, idx, direction, fvg_top, fvg_bottom):
        score = 0.0
        details = []
        active_setups = []
        atr_series = self._calculate_atr(df)

        if self.check_liquidity_sweep(df, idx, direction):
            score += 4.0; details.append("🧹 Strong Liquidity Sweep (+4)"); active_setups.append("LiquiditySweep")
        elif abs(df['low'].iloc[idx-1] - (df.iloc[max(0,idx-45):idx]['low'].min() if direction=="BUY" else df.iloc[max(0,idx-45):idx]['high'].max())) < atr_series.iloc[idx]*0.4:
            score += 2.0; details.append("Liquidity Grab (+2)"); active_setups.append("WeakSweep")

        if self.check_unicorn_breaker(df, idx, direction, fvg_bottom, fvg_top):
            score += 3.5; details.append("🦄 Unicorn Breaker+FVG (+3.5)"); active_setups.append("Unicorn")

        ote_pts = self.calculate_ote_score(df, idx, direction, fvg_bottom, fvg_top)
        if ote_pts > 0:
            score += ote_pts; details.append(f"🎯 OTE Golden Zone (+{ote_pts})"); active_setups.append("OTE")

        vol_avg = df['vol'].iloc[max(0, idx-30):idx+1].mean()
        cur_vol = df['vol'].iloc[idx]
        if cur_vol > vol_avg * 2.3:
            score += 2.5; details.append("📈 Climax Volume (+2.5)"); active_setups.append("Momentum")
        elif cur_vol > vol_avg * 1.55:
            score += 1.5; details.append("📊 Strong Volume (+1.5)"); active_setups.append("Momentum")

        if self.check_strong_displacement(df, idx+1, direction, atr_series):
            score += 1.5; details.append("🚀 Strong Displacement (+1.5)")

        return round(score, 1), details, active_setups

    def get_risk_parameters(self, active_setups):
        params = {'sl_atr': 0.85, 'rr1': 2.2, 'rr2': 3.5, 'partial_pct': 50}
        if "Unicorn" in active_setups and "OTE" in active_setups:
            params.update({'sl_atr': 0.45, 'rr1': 3.0, 'rr2': 4.5, 'partial_pct': 70})
        elif "LiquiditySweep" in active_setups:
            params.update({'sl_atr': 1.25, 'rr1': 2.0, 'rr2': 3.2, 'partial_pct': 40})
        elif "OTE" in active_setups:
            params.update({'sl_atr': 0.65, 'rr1': 2.8, 'rr2': 4.0, 'partial_pct': 60})
        elif "Momentum" in active_setups:
            params.update({'sl_atr': 0.85, 'rr1': 2.5, 'rr2': 3.8, 'partial_pct': 50})
        if len([x for x in active_setups if x != "WeakSweep"]) >= 3:
            params.update({'sl_atr': 0.40, 'rr1': 3.5, 'rr2': 5.5, 'partial_pct': 70})
        return params

    def scan_mtf_setups(self, symbol, df_ltf, df_htf, ltf_str, htf_str, trend_htf, state_manager):
        if trend_htf == "UNKNOWN": return None
        atr_ltf = self._calculate_atr(df_ltf)
        max_bars = MAX_BARS_LIMITS.get(ltf_str, 40)

        for i in range(len(df_ltf) - max_bars, len(df_ltf) - 4):
            direction = None
            if trend_htf in ["UP", "SIDEWAY"] and df_ltf['close'].iloc[i] < df_ltf['open'].iloc[i]:
                direction = "BUY"
            elif trend_htf in ["DOWN", "SIDEWAY"] and df_ltf['close'].iloc[i] > df_ltf['open'].iloc[i]:
                direction = "SELL"
            if not direction: continue

            if not self.check_strong_displacement(df_ltf, i+1, direction, atr_ltf): continue

            has_fvg, fvg_dir, fvg_bottom, fvg_top = self.check_fvg(df_ltf, i)
            if not has_fvg or ((direction=="BUY" and fvg_dir!="bullish") or (direction=="SELL" and fvg_dir!="bearish")):
                continue

            fvg_size = abs(fvg_top - fvg_bottom)
            if fvg_size < atr_ltf.iloc[i] * self.min_fvg_atr: continue
            if (direction == "BUY" and df_ltf['low'].iloc[i+3:].min() < fvg_bottom * 0.999) or \
               (direction == "SELL" and df_ltf['high'].iloc[i+3:].max() > fvg_top * 1.001):
                continue

            score, details, active = self.calculate_setup_score(df_ltf, i, direction, fvg_top, fvg_bottom)
            if score < MIN_SCORE_ALERT: continue

            latest_idx = len(df_ltf) - 1
            action_df = df_ltf.iloc[i+3:latest_idx+1]
            if len(action_df) < 2: continue

            touched = (action_df['low'].min() <= fvg_top if direction=="BUY" else action_df['high'].max() >= fvg_bottom)
            ob_low, ob_high = df_ltf['low'].iloc[i], df_ltf['high'].iloc[i]
            invalidated = (action_df['low'].min() < ob_low*0.998 if direction=="BUY" else action_df['high'].max() > ob_high*1.002)
            if not touched or invalidated: continue

            latest = df_ltf.iloc[latest_idx]
            reversal = True if AGGRESSIVE_MODE else \
                       ((direction=="BUY" and latest['close'] > latest['open']*1.001) or \
                        (direction=="SELL" and latest['close'] < latest['open']*0.999))

            if reversal and score >= MIN_SCORE_EXECUTE:
                exec_id = f"EXEC_{symbol}_{int(df_ltf['ts'].iloc[i])}"
                if ENABLE_ORDER_ANTISPAM and exec_id in state_manager.state: continue
                state_manager.save(exec_id)

                market_price = latest['close']
                risk_params = self.get_risk_parameters(active)
                sl = (min(ob_low, fvg_bottom) - atr_ltf.iloc[-1] * risk_params['sl_atr'] if direction=="BUY" else
                      max(ob_high, fvg_top) + atr_ltf.iloc[-1] * risk_params['sl_atr'])
                risk = abs(market_price - sl)

                return {
                    "type": "EXECUTION", "direction": direction,
                    "market_price": round(market_price, 6), "sl": round(sl, 6),
                    "tp1": round(market_price + risk * risk_params['rr1'] if direction=="BUY" else market_price - risk * risk_params['rr1'], 6),
                    "tp2": round(market_price + risk * risk_params['rr2'] if direction=="BUY" else market_price - risk * risk_params['rr2'], 6),
                    "rr1": risk_params['rr1'], "rr2": risk_params['rr2'],
                    "score": score, "details": details, "active_setups": active,
                    "ltf": ltf_str, "htf": htf_str, "partial_pct": risk_params['partial_pct']
                }
        return None

# ======================================================================
# --- 5. EXECUTION AGENT ---
# ======================================================================
class ExecutionAgent:
    def send_telegram(self, symbol, signal):
        if signal['type'] != "EXECUTION" or not TELEGRAM_TOKEN: return
        icon = "🚀" if signal['direction'] == "BUY" else "💥"
        setups = " + ".join(signal['active_setups'])
        msg = f"""
{icon} <b>SMC PRO v2.3 (One-Shot) | {symbol}</b>
───────────────
<b>Khung:</b> {signal['ltf']} (HTF: {signal['htf']})
<b>Score:</b> {signal['score']}/12.5 ⭐
<b>Setups:</b> {setups}
<b>Lý do:</b> {", ".join(signal['details'])}
───────────────
<b>Action:</b> MARKET {signal['direction']}
<b>Entry:</b> {signal['market_price']:.6f}
<b>SL:</b> {signal['sl']:.6f}
<b>TP1 (RR {signal['rr1']}):</b> {signal['tp1']:.6f}
<b>TP2 (RR {signal['rr2']}):</b> {signal['tp2']:.6f}
<b>Partial:</b> {signal['partial_pct']}% @ RR 1.6
"""
        for cid in CHAT_IDS:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              json={"chat_id": cid, "text": msg, "parse_mode": "HTML"})
            except: pass

    def send_text(self, msg):
        if not TELEGRAM_TOKEN: return
        for cid in CHAT_IDS:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              json={"chat_id": cid, "text": msg, "parse_mode": "HTML"})
            except: pass

# ======================================================================
# --- 6. MAIN – ONE-SHOT (Chạy 1 lần rồi kết thúc) ---
# ======================================================================
def main():
    print("🚀 Bot SMC PRO v2.3 (One-Shot Mode) bắt đầu quét...")
    run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    state_manager = GistStateManager()
    regime_agent = MarketRegimeAgent(exchange)
    signal_agent = SignalAgent()
    execution_agent = ExecutionAgent()

    signals_found = 0
    total_scans = 0

    for symbol in PAIRS:
        for ltf in TIMEFRAMES:
            htf = MTF_MAPPING.get(ltf)
            if not htf: continue

            df_ltf = regime_agent.get_data(symbol, ltf)
            time.sleep(0.7)   # Ngăn MEXC block IP
            df_htf = regime_agent.get_data(symbol, htf)
            time.sleep(0.7)

            if df_ltf is None or df_htf is None or len(df_ltf) < 50:
                continue

            total_scans += 1
            trend_htf = regime_agent.analyze_trend(df_htf)
            print(f"[{run_time}] [{symbol} | {ltf}/{htf}] Trend {htf}: {trend_htf} | Quét...")

            signal = signal_agent.scan_mtf_setups(symbol, df_ltf, df_htf, ltf, htf, trend_htf, state_manager)

            if signal and signal['type'] == 'EXECUTION':
                signals_found += 1
                print(f">>> [XÁC NHẬN] 🚀 {symbol} ({ltf}) - Score: {signal['score']} | {signal['active_setups']}")
                execution_agent.send_telegram(symbol, signal)

    # Heartbeat + Báo cáo tóm tắt (chỉ 1 lần)
    if ENABLE_HEARTBEAT:
        heartbeat = f"✅ [{run_time}] Bot SMC PRO v2.3 (One-Shot) đã quét xong\n• Quét {total_scans} khung thời gian\n• Tìm thấy {signals_found} tín hiệu chất lượng cao"
        print(heartbeat)
        execution_agent.send_text(heartbeat)

    if signals_found == 0:
        print(f"[{run_time}] Không có tín hiệu nào thỏa mãn lúc này.")

    print("🏁 Bot đã hoàn thành quét và tự động kết thúc (One-Shot Mode).")

if __name__ == "__main__":
    main()
