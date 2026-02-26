import pandas as pd
import numpy as np
import time
import ccxt
from datetime import datetime
from functools import wraps

# ==========================================
# --- 1. CẤU HÌNH ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.8
MIN_SCORE = 5
MAX_BARS_LIMITS = {'1h': 80}

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ==========================================
# --- 2. UTILS ---
# ==========================================
def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for _ in range(retries):
                try: return func(*args, **kwargs)
                except: time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_api()
def fetch_ohlcv_safe(symbol, tf, limit=500):
    return exchange.fetch_ohlcv(symbol, tf, limit=limit)

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_atr(df, length=14):
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))

def identify_fractals(df):
    df = df.copy()
    df['is_fractal_high'] = (df['high'].shift(2) > df['high'].shift(4)) & (df['high'].shift(2) > df['high'].shift(3)) & \
                            (df['high'].shift(2) > df['high'].shift(1)) & (df['high'].shift(2) > df['high'])
    df['is_fractal_low'] = (df['low'].shift(2) < df['low'].shift(4)) & (df['low'].shift(2) < df['low'].shift(3)) & \
                           (df['low'].shift(2) < df['low'].shift(1)) & (df['low'].shift(2) < df['low'])
    return df

# ==========================================
# --- 3. SMC CORE ---
# ==========================================
def has_fvg(df, ob_idx):
    end_idx = min(ob_idx + 4, len(df) - 1)
    for i in range(ob_idx + 1, end_idx):
        if df['low'].iloc[i + 1] > df['high'].iloc[i - 1]: return True, "bullish"
        if df['high'].iloc[i + 1] < df['low'].iloc[i - 1]: return True, "bearish"
    return False, None

def is_mitigated(df, ob_idx, trend, entry):
    if ob_idx + 1 >= len(df) - 3: return False   
    recent = df.iloc[ob_idx + 1 : -3]
    if recent.empty: return False
    buffer = 0.1 * df['atr'].iloc[ob_idx] if 'atr' in df.columns else 0
    if trend == "UP":
        return (recent['low'] <= entry + buffer).any()
    return (recent['high'] >= entry - buffer).any()

def is_strong_displacement(df, idx, direction):
    if idx >= len(df): return False
    c = df.iloc[idx]
    body = abs(c['close'] - c['open'])
    if body < (c['high'] - c['low']) * 0.65: return False
    return (direction == "UP" and c['close'] > c['open']) or (direction == "DOWN" and c['close'] < c['open'])

def find_quality_zone(df, trend, atr):
    df = df.reset_index(drop=True)
    for i in range(len(df) - 10, 25, -1):
        if trend == "UP":
            if df['close'].iloc[i] < df['open'].iloc[i]:
                if is_strong_displacement(df, i + 1, "UP"):
                    fvg_ok, fvg_dir = has_fvg(df, i)
                    if fvg_ok and fvg_dir == "bullish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        swing_low = df['low'].iloc[max(0, i-40):i+1].min()
                        sl = min(df['low'].iloc[i], swing_low) - atr * SL_ATR_MULTIPLIER
                        if is_mitigated(df, i, trend, entry): continue
                        whale = df['vol'].iloc[i+1:i+5].max() > df['vol'].iloc[max(0,i-25):i].mean() * WHALE_VOL_MULTIPLIER
                        sweep = df['low'].iloc[i-6:i+1].min() < df['low'].iloc[i-15:i-6].min() if i > 15 else False
                        return entry, sl, i, True, whale, sweep
        else:
            if df['close'].iloc[i] > df['open'].iloc[i]:
                if is_strong_displacement(df, i + 1, "DOWN"):
                    fvg_ok, fvg_dir = has_fvg(df, i)
                    if fvg_ok and fvg_dir == "bearish":
                        entry = (df['high'].iloc[i] + df['low'].iloc[i]) / 2
                        swing_high = df['high'].iloc[max(0, i-40):i+1].max()
                        sl = max(df['high'].iloc[i], swing_high) + atr * SL_ATR_MULTIPLIER
                        if is_mitigated(df, i, trend, entry): continue
                        whale = df['vol'].iloc[i+1:i+5].max() > df['vol'].iloc[max(0,i-25):i].mean() * WHALE_VOL_MULTIPLIER
                        sweep = df['high'].iloc[i-6:i+1].max() > df['high'].iloc[i-15:i-6].max() if i > 15 else False
                        return entry, sl, i, True, whale, sweep
    return 0, 0, 0, False, False, False

def get_htf_trend_from_df(df):
    df = identify_fractals(df)
    ema200 = calculate_ema(df['close'], 200).iloc[-1]
    atr = calculate_atr(df).iloc[-1]
    close = df['close'].iloc[-1]
    if close > ema200 - atr * 1.2: return "UP"
    if close < ema200 + atr * 1.2: return "DOWN"
    return "SIDEWAY"

def get_swing_range(df):
    return df['high'].iloc[-250:].max(), df['low'].iloc[-250:].min()

def is_premium_discount(entry, trend, sh, sl):
    mid = (sh + sl) / 2
    return (trend == "UP" and entry < mid) or (trend == "DOWN" and entry > mid)

def is_trigger_candle(df, idx, sig):
    if idx < 1: return False, ""
    c, p = df.iloc[idx], df.iloc[idx-1]
    body = abs(c['close'] - c['open'])
    upper = c['high'] - max(c['close'], c['open'])
    lower = min(c['close'], c['open']) - c['low']
    if sig == "BUY":
        if lower > body * 2.2 and lower > upper: return True, "Bullish Pinbar"
        if p['close'] < p['open'] and c['close'] > p['open'] and c['close'] > c['open']: return True, "Bullish Engulfing"
    else:
        if upper > body * 2.2 and upper > lower: return True, "Bearish Pinbar"
        if p['close'] > p['open'] and c['close'] < p['open'] and c['close'] < c['open']: return True, "Bearish Engulfing"
    return False, ""

# ==========================================
# --- 4. BACKTEST LOGIC (NO LOOK-AHEAD) ---
# ==========================================
if __name__ == "__main__":
    print(f"SMC v6.5 BACKTEST Started {datetime.now().strftime('%H:%M:%S')} - NO LOOK-AHEAD BIAS")
    trades = []
    position_size_usd = 100.0
    leverage = 10

    for symbol in PAIRS:
        print(f"\n🔍 Backtesting {symbol} 1h (1500 nến \~62 ngày)...")
        bars1h = exchange.fetch_ohlcv(symbol, '1h', limit=1500)
        df1h = pd.DataFrame(bars1h, columns=['ts','open','high','low','close','vol'])
        bars4h = exchange.fetch_ohlcv(symbol, '4h', limit=400)
        df4h = pd.DataFrame(bars4h, columns=['ts','open','high','low','close','vol'])

        df1h = identify_fractals(df1h)
        df1h['atr'] = calculate_atr(df1h)
        df1h['rsi'] = calculate_rsi(df1h['close'])

        for i in range(300, len(df1h) - 10):
            current_df = df1h.iloc[:i+1].copy().reset_index(drop=True)
            current_ts = current_df['ts'].iloc[-1]

            htf_mask = df4h['ts'] <= current_ts
            if not htf_mask.any(): continue
            current_4h_df = df4h[htf_mask].copy().reset_index(drop=True)
            if len(current_4h_df) < 50: continue
            htf_trend = get_htf_trend_from_df(current_4h_df)
            if htf_trend == "SIDEWAY": continue

            atr = current_df['atr'].iloc[-1]
            entry, sl, ob_idx, fvg_found, has_whale, has_sweep = find_quality_zone(current_df, htf_trend, atr)
            if not fvg_found or entry == 0: continue

            risk = abs(entry - sl)
            score = 4
            if has_whale: score += 1
            if has_sweep: score += 1
            sh, sl_range = get_swing_range(current_df)
            if is_premium_discount(entry, htf_trend, sh, sl_range): score += 1
            rsi = current_df['rsi'].iloc[-1]
            if (htf_trend == "UP" and rsi < 48) or (htf_trend == "DOWN" and rsi > 52): score += 1
            if score < MIN_SCORE: continue

            search = current_df.iloc[ob_idx+5:i]
            if search.empty: continue
            tp = search['high'].max() if htf_trend == "UP" else search['low'].min()
            rr = abs(tp - entry) / risk if risk > 0 else 0
            if rr < 1.5: continue

            bars_since = i - ob_idx
            if bars_since > MAX_BARS_LIMITS['1h']: continue

            sig = "BUY" if htf_trend == "UP" else "SELL"
            has_trig, _ = is_trigger_candle(current_df, i, sig)
            tol = ENTRY_TOLERANCE * current_df['atr'].iloc[-1]
            tapped = (sig == "BUY" and current_df.iloc[-1]['low'] <= entry + tol) or \
                     (sig == "SELL" and current_df.iloc[-1]['high'] >= entry - tol)
            if not tapped: continue

            hit_sl = False
            hit_tp = False
            exit_price = None
            for j in range(i + 1, len(df1h)):
                candle = df1h.iloc[j]
                if sig == "BUY":
                    if candle['low'] <= sl:
                        hit_sl = True
                        exit_price = sl
                        break
                    if candle['high'] >= tp:
                        hit_tp = True
                        exit_price = tp
                        break
                else:
                    if candle['high'] >= sl:
                        hit_sl = True
                        exit_price = sl
                        break
                    if candle['low'] <= tp:
                        hit_tp = True
                        exit_price = tp
                        break

            if hit_sl or hit_tp:
                pnl = ((exit_price - entry) / entry * position_size_usd * leverage) if sig == "BUY" else \
                      ((entry - exit_price) / entry * position_size_usd * leverage)
                win = pnl > 0
                trades.append({
                    'time': datetime.fromtimestamp(current_df['ts'].iloc[-1]/1000),
                    'symbol': symbol,
                    'sig': sig,
                    'entry': round(entry, 4),
                    'sl': round(sl, 4),
                    'tp': round(tp, 4),
                    'rr': round(rr, 2),
                    'pnl': round(pnl, 2),
                    'win': win
                })

    if trades:
        df_trades = pd.DataFrame(trades)
        winrate = df_trades['win'].mean() * 100
        total_pnl = df_trades['pnl'].sum()
        avg_rr = df_trades['rr'].mean()
        num_trades = len(df_trades)

        print(f"\n📊 === BACKTEST KẾT QUẢ (1h) ===")
        print(f"Số lệnh: {num_trades}")
        print(f"Winrate: {winrate:.1f}%")
        print(f"Total PnL: ${total_pnl:.2f} ($100/lệnh, 10x leverage)")
        print(f"Avg RR: {avg_rr:.2f}")
        print(f"Max drawdown: ${df_trades['pnl'].cumsum().min():.2f}")
        print("\nChi tiết lệnh:")
        print(df_trades[['time', 'symbol', 'sig', 'rr', 'pnl', 'win']])
    else:
        print("Không có lệnh nào (strategy strict - tốt!).")

    print(f"\nBacktest xong lúc {datetime.now().strftime('%H:%M:%S')}")