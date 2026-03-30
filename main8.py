import pandas as pd
import numpy as np
import ccxt
import requests
import os
import time
from datetime import datetime, timedelta

# ======================================================================
# --- 1. CẤU HÌNH BOT ULTRA MTF (5m, 15m, 1h) ---
# ======================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAMES = {
    '5m': '1h',    # Entry 5m -> Trend 1h
    '15m': '1h',   # Entry 15m -> Trend 1h
    '1h': '4h'     # Entry 1h -> Trend 4h
}

MIN_SCORE_EXECUTE = 3.5 

# Tham số tối ưu từ Backtest 180 ngày MTF
PARAMS = {
    'sl_atr_mult': 0.5,  # Tăng lên 0.5 để râu nến MEXC khó quét tới (Phát hiện sáng nay 0.4 là quá chặt).
    'min_fvg_atr': 0.5,
    'min_disp_atr': 0.8,
    'ote_low': 0.62,
    'ote_high': 0.79,
    'tp2_rr': 2.0        # Tăng lên 2.0R để bù lại các lệnh bị thoát ở hòa vốn (BE).
}


exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

# ======================================================================
# --- 2. SMC PRICE ACTION AGENT (5 STRONG SETUPS) ---
# ======================================================================
class SMC_PA_Agent:
    def __init__(self):
        self.p = PARAMS

    def check_fvg(self, df, idx):
        if idx + 2 >= len(df): return False, None, None, None
        low_p, high_p = df['low'].iloc[idx], df['high'].iloc[idx]
        low_f, high_f = df['low'].iloc[idx+2], df['high'].iloc[idx+2]
        if low_f > high_p: return True, "bullish", high_p, low_f
        if high_f < low_p: return True, "bearish", high_f, low_p
        return False, None, None, None

    def check_strong_displacement(self, df, idx, direction, atr_val):
        if idx >= len(df): return False
        c = df.iloc[idx]
        body = abs(c['close'] - c['open'])
        total = c['high'] - c['low']
        if total == 0 or body < total * 0.65: return False
        if body < atr_val * self.p['min_disp_atr']: return False
        return (direction == "BUY" and c['close'] > c['open']) or \
               (direction == "SELL" and c['close'] < c['open'])

    def check_liquidity_sweep(self, df, idx, direction):
        lookback = df.iloc[max(0, idx-45):idx]
        if lookback.empty: return False
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
                if fvg_bottom - 0.3*(fvg_top-fvg_bottom) <= c['high'] <= fvg_top: return True
            elif direction == "SELL" and c['close'] > c['open']:
                if fvg_bottom <= c['low'] <= fvg_top + 0.3*(fvg_top-fvg_bottom): return True
        return False

    def calculate_ote_score(self, df, idx, direction, fvg_bottom, fvg_top):
        start = max(0, idx-12)
        imp_low = df['low'].iloc[start:idx+3].min()
        imp_high = df['high'].iloc[start:idx+3].max()
        rng = imp_high - imp_low
        if rng <= 0: return 0
        if direction == "BUY":
            ote_t, ote_b = imp_high - rng * self.p['ote_low'], imp_high - rng * self.p['ote_high']
            return 3 if fvg_top >= ote_b and fvg_bottom <= ote_t else 0
        else:
            ote_b, ote_t = imp_low + rng * self.p['ote_low'], imp_low + rng * self.p['ote_high']
            return 3 if fvg_bottom <= ote_t and fvg_top >= ote_b else 0

    def calculate_setup_score(self, df, idx, direction, fvg_top, fvg_bottom, atr_val):
        score = 0.0
        active = []
        if self.check_liquidity_sweep(df, idx, direction): score += 4.0; active.append("Shark Sweep 🦈")
        if self.check_unicorn_breaker(df, idx, direction, fvg_bottom, fvg_top): score += 3.5; active.append("Unicorn 🦄")
        ote_pts = self.calculate_ote_score(df, idx, direction, fvg_bottom, fvg_top)
        if ote_pts > 0: score += ote_pts; active.append("OTE Zone 🎯")
        
        vol_avg = df['vol'].iloc[max(0, idx-30):idx+1].mean()
        if df['vol'].iloc[idx] > vol_avg * 1.55: score += 1.5; active.append("Momentum 📊")
        
        if self.check_strong_displacement(df, idx+1, direction, atr_val): score += 1.5; active.append("MS Shift ⚡")
        return round(score, 1), active

# ======================================================================
# --- 3. MAIN SCREENER ENGINE ---
# ======================================================================
def fetch_data(symbol, tf, limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        hl, hc, lc = df['high'] - df['low'], np.abs(df['high'] - df['close'].shift()), np.abs(df['low'] - df['close'].shift())
        df['atr'] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        return df
    except: return None

def main():
    print(f"[{datetime.now()}] 🚀 Khởi chạy ULTRA MTF (Multi-Timeframe)...")
    agent = SMC_PA_Agent()

    for symbol in PAIRS:
        # Cache HTF data
        dfs = {tf: fetch_data(symbol, tf) for tf in ['5m', '15m', '1h', '4h']}
        
        for ltf, htf in TIMEFRAMES.items():
            df_ltf = dfs[ltf]
            df_htf = dfs[htf]
            if df_ltf is None or df_htf is None: continue

            last_htf = df_htf.iloc[-1]
            trend = "UP" if last_htf['close'] > last_htf['ema200'] else "DOWN"

            for i in range(len(df_ltf) - 6, len(df_ltf) - 2):
                direction = "BUY" if trend == "UP" else "SELL"
                atr = df_ltf['atr'].iloc[i]
                
                has_fvg, fvg_dir, fvg_bottom, fvg_top = agent.check_fvg(df_ltf, i)
                if not has_fvg or ((direction=="BUY" and fvg_dir!="bullish") or (direction=="SELL" and fvg_dir!="bearish")): continue
                if not agent.check_strong_displacement(df_ltf, i+1, direction, atr): continue
                
                score, active = agent.calculate_setup_score(df_ltf, i, direction, fvg_top, fvg_bottom, atr)
                
                if score >= MIN_SCORE_EXECUTE:
                    action_df = df_ltf.iloc[i+3:]
                    touched = (action_df['low'].min() <= fvg_top if direction=="BUY" else action_df['high'].max() >= fvg_bottom)
                    if touched:
                        # Tính toán mốc thời gian (UTC+7 - Giờ Việt Nam)
                        now_vn = datetime.utcnow() + timedelta(hours=7)
                        expiry_minutes = 60 if ltf == '5m' else (180 if ltf == '15m' else 720)
                        expiry_time = (now_vn + timedelta(minutes=expiry_minutes)).strftime("%H:%M %d/%m")
                        
                        entry = fvg_top if direction=="BUY" else fvg_bottom
                        sl = (min(df_ltf['low'].iloc[i], fvg_bottom) - atr * PARAMS['sl_atr_mult']) if direction=="BUY" else \
                             (max(df_ltf['high'].iloc[i], fvg_top) + atr * PARAMS['sl_atr_mult'])
                        risk = abs(entry - sl)
                        tp1, tp2 = (entry + risk, entry + risk * PARAMS['tp2_rr']) if direction=="BUY" else (entry - risk, entry - risk * PARAMS['tp2_rr'])
                        
                        msg = (
                            f"🌀 <b>SMC ULTRA MTF ({ltf})</b> 🌀\n"
                            f"🪙 <b>Cặp:</b> {symbol}\n"
                            f"📈 <b>Hướng:</b> {direction} MARKET\n"
                            f"-----------------\n"
                            f"🎯 <b>Entry:</b> <code>{entry:.5f}</code>\n"
                            f"🛑 <b>Stoploss:</b> <code>{sl:.5f}</code>\n"
                            f"💰 <b>TP1 (1R - 50% & BE):</b> <code>{tp1:.5f}</code>\n"
                            f"💰 <b>TP2 ({PARAMS['tp2_rr']}R):</b> <code>{tp2:.5f}</code>\n"
                            f"-----------------\n"
                            f"🕛 <b>Bỏ lệnh sau:</b> <code>{expiry_time}</code> (Giờ VN)\n"
                            f"⚠️ <i>Hủy nếu quét SL hoặc đạt TP trước khi khớp.</i>\n\n"
                            f"🔍 <b>Setups:</b>\n" + "\n".join([f"• {s}" for s in active])
                        )
                        send_telegram(msg)
                        print(f"!!! PHÁT HIỆN MTF {ltf} -> {symbol} {direction} !!!")
                        break

if __name__ == "__main__":
    try: main()
    except Exception as e: print(f"Lỗi: {e}")
