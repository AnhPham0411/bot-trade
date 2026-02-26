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
# --- 1. CẤU HÌNH ---
# ==========================================
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
MTF_MAPPING = {'15m': '1h', '1h': '4h', '4h': '1d'}

SL_ATR_MULTIPLIER = 1.8
ENTRY_TOLERANCE = 0.6
WHALE_VOL_MULTIPLIER = 1.8
# MIN_SCORE = 5
MIN_SCORE = 4

MAX_BARS_LIMITS = {'15m': 35, '1h': 80, '4h': 55}

ENABLE_ORDER_ANTISPAM = True
ENABLE_HEARTBEAT = True
ENABLE_KILLZONES = False

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
user_chat_id = os.getenv('TELEGRAM_CHAT_ID')
group_chat_id = "-5213535598"
CHAT_IDS = [cid for cid in [user_chat_id, group_chat_id] if cid]

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# ==========================================
# --- 2. STATE MANAGER ---
# ==========================================
class GistStateManager:
def __init__(self, filename='bot_state.json'):
self.filename = filename
self.github_token = os.getenv('GH_GIST_TOKEN')
self.gist_id = os.getenv('GIST_ID')
self.headers = {"Authorization": f"token {self.github_token}", "Accept": "application/vnd.github.v3+json"} if self.github_token else {}
self.state = self.load()
