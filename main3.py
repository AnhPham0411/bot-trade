import pandas as pd
import numpy as np
import os
import requests
import time
import ccxt
from datetime import datetime

# ==========================================
# --- 1. CẤU HÌNH ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

SL_ATR_MULTIPLIER = 0.8
ENTRY_TOLERANCE = 0.5
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN_1')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
CHAT_IDS = [CHAT_ID] if CHAT_ID else []

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
LAST_ALERTED = {}

# ==========================================
# --- 2. HÀM CORE & CHỈ BÁO ---
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
    try:
        if direction == "UP": return df['low'].iloc[idx + 2] > df['high'].iloc[idx]
        return df['high'].iloc[idx + 2] < df['low'].iloc[idx]
    except: return False

def get_htf_trend(symbol, htf):
    try:
        bars = exchange.fetch_ohlcv(symbol, htf, limit=500)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        ema_200 = calculate_ema(df['close'], 200).iloc[-2]
        atr = calculate_atr(df).iloc[-2]
        close_price = df['close'].iloc[-2]
        if abs(close_price - ema_200) < atr: return "SIDEWAY"
        return "UP" if close_price > ema_200 else "DOWN"
    except: return "SIDEWAY"

def is_ob_fresh(df, ob_idx, sl, tp, trend):
    history = df.iloc[ob_idx + 1 : -1]
    if history.empty: return True
    if trend == "UP":
        if (history['low'] <= sl).any() or (history['high'] >= tp).any(): return False
    else:
        if (history['high'] >= sl).any() or (history['low'] <= tp).any(): return False
    return True

def find_quality_zone(df, trend, current_atr):
    fractal_lows = df[df['is_fractal_low']]
    fractal_highs = df[df['is_fractal_high']]
    
    def check_whale_vol(df, pos, ob_vol):
        avg_vol = df['vol'].iloc[max(0, pos-20):pos].mean()
        return ob_vol > (avg_vol * 1.5) if avg_vol > 0 else False

    if trend == "UP" and not fractal_lows.empty:
        idx = df.index.get_loc(fractal_lows.index[-1])
        sub = df.iloc[max(0, idx-15):idx+3]
        red = sub[sub['close'] < sub['open']]
        if not red.empty:
            ob = red.loc[red['low'].idxmin()]
            pos = df.index.get_loc(ob.name)
            has_whale = check_whale_vol(df, pos, ob['vol'])
            return (ob['high']+ob['low'])/2, ob['low']-(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "UP"), has_whale
        # Fallback fractal
        ob = df.iloc[idx]
        pos = idx
        has_whale = check_whale_vol(df, pos, ob['vol'])
        return (ob['high']+ob['low'])/2, ob['low']-(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "UP"), has_whale

    elif trend == "DOWN" and not fractal_highs.empty:
        idx = df.index.get_loc(fractal_highs.index[-1])
        sub = df.iloc[max(0, idx-15):idx+3]
        green = sub[sub['close'] > sub['open']]
        if not green.empty:
            ob = green.loc[green['high'].idxmax()]
            pos = df.index.get_loc(ob.name)
            has_whale = check_whale_vol(df, pos, ob['vol'])
            return (ob['high']+ob['low'])/2, ob['high']+(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "DOWN"), has_whale
        # Fallback fractal
        ob = df.iloc[idx]
        pos = idx
        has_whale = check_whale_vol(df, pos, ob['vol'])
        return (ob['high']+ob['low'])/2, ob['high']+(current_atr*SL_ATR_MULTIPLIER), pos, check_fvg(df, pos, "DOWN"), has_whale

    return 0, 0, 0, False, False

# ====================== CÁC BỘ LỌC SMC NÂNG CAO ======================
def get_swing_range(df):
    lookback = 120  # 2-4 giờ gần nhất tùy TF
    swing_high = df['high'].iloc[-lookback:].max()
    swing_low = df['low'].iloc[-lookback:].min()
    return swing_high, swing_low

def is_premium_discount(entry, trend, swing_high, swing_low):
    mid = (swing_high + swing_low) / 2
    if trend == "UP":   # BUY phải ở Discount (dưới 50%)
        return entry < mid
    else:               # SELL phải ở Premium (trên 50%)
        return entry > mid

def has_liquidity_sweep(df, ob_idx, trend):
    """Kiểm tra râu quét fractal cũ trước OB (Quét thanh khoản)"""
    ob_candle = df.iloc[ob_idx]
    if trend == "UP":
        prev_fractals = df[df['is_fractal_low'] & (df.index < ob_idx)]
        if prev_fractals.empty: return False
        prev_low = prev_fractals['low'].iloc[-1]
        check_range = df.iloc[max(0, ob_idx-4):ob_idx]
        return (check_range['low'] < prev_low).any()
    else:
        prev_fractals = df[df['is_fractal_high'] & (df.index < ob_idx)]
        if prev_fractals.empty: return False
        prev_high = prev_fractals['high'].iloc[-1]
        check_range = df.iloc[max(0, ob_idx-4):ob_idx]
        return (check_range['high'] > prev_high).any()

def is_trigger_candle(df, idx, signal_type):
    candle = df.iloc[idx]
    prev = df.iloc[idx-1]
    body = abs(candle['close'] - candle['open'])
    upper = candle['high'] - max(candle['close'], candle['open'])
    lower = min(candle['close'], candle['open']) - candle['low']
    
    if signal_type == "BUY":
        if lower > body * 2 and lower > upper: return True, "Bullish Pinbar"
        if prev['close'] < prev['open'] and candle['close'] > candle['open'] and \
           candle['close'] > prev['open'] and candle['open'] < prev['close']: 
            return True, "Bullish Engulfing"
    else:
        if upper > body * 2 and upper > lower: return True, "Bearish Pinbar"
        if prev['close'] > prev['open'] and candle['close'] < candle['open'] and \
           candle['close'] < prev['open'] and candle['open'] > prev['close']: 
            return True, "Bearish Engulfing"
    return False, ""

# ==========================================
# --- 3. ENGINE SMC PRO v5 (Setup Score + Execution) ---
# ==========================================
def analyze_pair(symbol, tf):
    htf = MTF_MAPPING.get(tf)
    htf_trend = get_htf_trend(symbol, htf)
    if htf_trend == "SIDEWAY": return

    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=300)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = identify_fractals(df)
        df['atr'] = calculate_atr(df, length=14)
        df['rsi'] = calculate_rsi(df['close'])

        atr = df['atr'].iloc[-2]
        entry, sl, ob_idx, has_fvg, has_whale_vol = find_quality_zone(df, htf_trend, atr)
        if entry == 0: return

        risk = abs(entry - sl)
        tp_mitigation = (entry + risk * 1.618) if htf_trend == "UP" else (entry - risk * 1.618)
        if not is_ob_fresh(df, ob_idx, sl, tp_mitigation, htf_trend): return

        # === BỘ LỌC FRESHNESS ===
        current_price = df['close'].iloc[-2]
        distance_atr = abs(current_price - entry) / atr
        bars_since_ob = len(df) - 2 - ob_idx
        if distance_atr > 3.0 or bars_since_ob > 15: return

        # === BỘ LỌC ANTI-SPAM ===
        key = f"{symbol}_{tf}_{ob_idx}"
        if key in LAST_ALERTED and time.time() - LAST_ALERTED[key] < 3600: return
        LAST_ALERTED[key] = time.time()

        signal_type = "BUY" if htf_trend == "UP" else "SELL"

        # === SETUP SCORE (MAX 5) ===
        setup_score = 1  # Trend nền tảng
        factors = [f"HTF Trend {htf_trend}"]
        
        if has_fvg:
            setup_score += 1
            factors.append("SMC FVG")
        if has_whale_vol:
            setup_score += 1
            factors.append("Whale Volume 🐳")
        
        # RSI Confluence
        rsi = df['rsi'].iloc[-2]
        if (signal_type == "BUY" and rsi < 45) or (signal_type == "SELL" and rsi > 55):
            setup_score += 1
            factors.append(f"RSI {'Oversold' if signal_type=='BUY' else 'Overbought'} ({rsi:.1f})")
        
        # Premium/Discount Zone
        swing_high, swing_low = get_swing_range(df)
        if is_premium_discount(entry, htf_trend, swing_high, swing_low):
            setup_score += 1
            factors.append("Premium/Discount Zone ✅")
        
        # Điểm nhấn VIP: Liquidity Sweep
        liquidity_sweep = has_liquidity_sweep(df, ob_idx, htf_trend)

        # === EXECUTION STATUS (TRẠNG THÁI VÀO LỆNH) ===
        last_idx = len(df) - 2
        has_trigger, trigger_name = is_trigger_candle(df, last_idx, signal_type)
        
        tapped = False
        if signal_type == "BUY" and df.iloc[-2]['low'] <= (entry + ENTRY_TOLERANCE * atr):
            tapped = True
        elif signal_type == "SELL" and df.iloc[-2]['high'] >= (entry - ENTRY_TOLERANCE * atr):
            tapped = True

        if tapped and has_trigger:
            execution = "CE Triggered 🔥"
        elif tapped:
            execution = "Tapped Zone ✅"
        else:
            execution = "Waiting Limit ⏳"

        # === TP LOGIC (KẾT HỢP FIB 1.618 & THANH KHOẢN ĐỈNH/ĐÁY) ===
        tp_fib = entry + (risk * 1.618) if signal_type == "BUY" else entry - (risk * 1.618)
        tp = tp_fib
        tp_type = "Fib 1.618"
        
        if signal_type == "BUY":
            fractal_highs = df[df['is_fractal_high']]
            if not fractal_highs.empty:
                temp_struct = fractal_highs['high'].iloc[-1]
                if (temp_struct - entry) / risk >= 1.5:
                    tp, tp_type = temp_struct, "Fractal High (Thanh khoản đỉnh)"
        else:
            fractal_lows = df[df['is_fractal_low']]
            if not fractal_lows.empty:
                temp_struct = fractal_lows['low'].iloc[-1]
                if (entry - temp_struct) / risk >= 1.5:
                    tp, tp_type = temp_struct, "Fractal Low (Thanh khoản đáy)"
                    
        rr = abs(tp - entry) / risk if risk > 0 else 0

        # === XUẤT TIN NHẮN ĐẸP LÊN TELEGRAM ===
        strength = "💎 PERFECT" if setup_score == 5 else "🔥 STRONG" if setup_score >= 3 else "⚠️ MODERATE"
        
        msg = (f"🚀 <b>SMC PRO v5 - {signal_type} {strength}</b>\n"
               f"Symbol: {symbol} ({tf}) | Age: {bars_since_ob} bars\n"
               f"-----------------\n"
               f"Setup Score: <b>{setup_score}/5</b>\n"
               f"Execution: <b>{execution}</b>\n"
               f"Entry Zone: <code>{entry:.4f}</code>\n"
               f"SL: <code>{sl:.4f}</code>\n"
               f"TP ({tp_type}): <code>{tp:.4f}</code>\n"
               f"R:R: <b>1:{rr:.2f}</b>\n"
               f"-----------------\n"
               f"🔍 Setup Confluences:\n + " + "\n + ".join(factors))
        
        if liquidity_sweep:
            msg += "\n + Liquidity Sweep (Inducement) 🦈"
        
        # Khuyến nghị hành động
        if execution == 'CE Triggered 🔥':
            action_text = "⚡ VÀO LỆNH MARKET NGAY (Đã có nến đảo chiều xác nhận)!"
        elif execution == 'Tapped Zone ✅':
            action_text = "👀 Giá đang chạm vùng Entry, bật chart theo dõi hoặc rải Limit!"
        else:
            action_text = "⏳ Cài sẵn LIMIT ngay tại vùng Entry và chờ đợi!"
            
        msg += f"\n\n💡 {action_text}"

        send_telegram(msg)
        print(f">>> {symbol} {tf}: {execution} (Setup {setup_score}/5)")

    except Exception as e:
        print(f"Error {symbol} {tf}: {e}")

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_IDS: return
    for cid in CHAT_IDS:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# ==========================================
# --- 4. CHẠY MAIN ---
# ==========================================
if __name__ == "__main__":
    print(f"🚀 SMC PRO v5 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for symbol in PAIRS:
        for tf in MTF_MAPPING.keys():
            analyze_pair(symbol, tf)
            time.sleep(1.2)
    print(f"✅ Finished at {datetime.now().strftime('%H:%M:%S')}")
