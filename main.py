import sys
import os
import time
import json
import asyncio
import uuid
import logging
import ssl
import math
import threading
import hashlib
import random
import base64
import sqlite3
import traceback
from datetime import datetime as dt
from typing import Dict, Optional, List

import aiohttp
import pytz

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.checkbox import CheckBox
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.metrics import dp

# ============================================================================
# 0. ШТАТНОЕ ЛОГИРОВАНИЕ
# ============================================================================
LOG_DIR = None  # Будет установлен в App
LOG_FILE = None

def setup_logging():
    global LOG_DIR, LOG_FILE
    if LOG_DIR is None:
        try:
            LOG_DIR = App.get_running_app().user_data_dir if App.get_running_app() else os.getcwd()
        except:
            LOG_DIR = os.getcwd()
    LOG_FILE = os.path.join(LOG_DIR, "titan_crash.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

setup_logging()
logger = logging.getLogger("TITAN_PRO")

# ============================================================================
# 1. ANDROID WAKE LOCK & SERVICE
# ============================================================================
HAS_ANDROID_WAKELOCK = False
wake_lock = None
try:
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Context = autoclass('android.content.Context')
    PowerManager = autoclass('android.os.PowerManager')
    
    activity = PythonActivity.mActivity
    if activity is not None:
        power_manager = activity.getSystemService(Context.POWER_SERVICE)
        wake_lock = power_manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TITAN:TradingLock")
        HAS_ANDROID_WAKELOCK = True
        logger.info("✅ Android WakeLock инициализирован")
except Exception as e:
    logger.warning(f"⚠️ WakeLock не инициализирован: {e}")

def start_foreground_service():
    try:
        from titan_service import start_service
        start_service()
    except ImportError:
        logger.warning("⚠️ titan_service.py не найден")

# ============================================================================
# 2. DEVICE ID & ЗАЩИТА
# ============================================================================
DEVICE_ID = "UNKNOWN"
try:
    from jnius import autoclass
    Settings = autoclass('android.provider.Settings$Secure')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    activity = PythonActivity.mActivity
    if activity is None:
        raise Exception("PythonActivity.mActivity is None")
    
    content_resolver = activity.getContentResolver()
    android_id = Settings.Secure.getString(content_resolver, Settings.Secure.ANDROID_ID)
    DEVICE_ID = hashlib.sha256(android_id.encode()).hexdigest()[:16]
    logger.info(f"✅ Device ID получен: {DEVICE_ID}")
except Exception as e:
    logger.warning(f"⚠️ Не удалось получить Device ID: {e}")
    DEVICE_ID = "FALLBACK_" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]

LICENSE_SECRET = "TITAN_NEVINNOMYSSK_2026_SECRET_KEY"
LICENSE_FILE = None
USER_DATA_FILE = None

def get_file_paths():
    global LICENSE_FILE, USER_DATA_FILE
    if LICENSE_FILE is None:
        LICENSE_FILE = os.path.join(LOG_DIR, "titan_license.key")
        USER_DATA_FILE = os.path.join(LOG_DIR, "titan_user_data.enc")

get_file_paths()

def check_license_status() -> bool:
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, 'r', encoding='utf-8') as f:
                stored_key = f.read().strip()
            expected_key = hashlib.sha256(f"{DEVICE_ID}{LICENSE_SECRET}".encode()).hexdigest()[:32]
            if stored_key == expected_key:
                logger.info("✅ Лицензия активна")
                return True
        except Exception as e:
            logger.error(f"Ошибка чтения лицензии: {e}")
    return False

IS_LICENSED = check_license_status()

# ============================================================================
# 3. ШИФРОВАНИЕ И СОХРАНЕНИЕ ДАННЫХ КЛИЕНТА
# ============================================================================
def encrypt_user_data(data_dict: dict) -> bytes:
    json_str = json.dumps(data_dict)
    key = hashlib.sha256(DEVICE_ID.encode()).digest()
    encrypted = bytearray()
    for i, byte in enumerate(json_str.encode('utf-8')):
        encrypted.append(byte ^ key[i % len(key)])
    return base64.b64encode(encrypted)

def decrypt_user_data(encrypted_bytes: bytes) -> dict:
    try:
        decoded = base64.b64decode(encrypted_bytes)
        key = hashlib.sha256(DEVICE_ID.encode()).digest()
        decrypted = bytearray()
        for i, byte in enumerate(decoded):
            decrypted.append(byte ^ key[i % len(key)])
        return json.loads(decrypted.decode('utf-8'))
    except Exception:
        return {}

def save_user_credentials(token: str, fut: str, stk: str, fx: str):
    data = {"token": token, "fut": fut, "stk": stk, "fx": fx}
    with open(USER_DATA_FILE, 'wb') as f:
        f.write(encrypt_user_data(data))
    logger.info("✅ Данные клиента сохранены и зашифрованы")

def load_user_credentials() -> dict:
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, 'rb') as f:
            return decrypt_user_data(f.read())
    return {"token": "", "fut": "7502Y5H", "stk": "D101327", "fx": "G68390"}

# ============================================================================
# 4. ЮРИДИЧЕСКИЕ ФАЙЛЫ & НАСТРОЙКИ РИСКА
# ============================================================================
LEGAL_ACCEPTED_FILE = None
SETTINGS_FILE = None

def get_settings_paths():
    global LEGAL_ACCEPTED_FILE, SETTINGS_FILE
    if LEGAL_ACCEPTED_FILE is None:
        LEGAL_ACCEPTED_FILE = os.path.join(LOG_DIR, "legal_accepted.json")
        SETTINGS_FILE = os.path.join(LOG_DIR, "titan_settings.json")

get_settings_paths()

LEGAL_TEXT = """
ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ TITAN PRO

1. ОБЩИЕ ПОЛОЖЕНИЯ
Программа является техническим инструментом для автоматизации торговли. 
НЕ является инвестиционной рекомендацией или доверительным управлением.

2. ОТКАЗ ОТ ОТВЕТСТВЕННОСТИ
Программа предоставляется "как есть". Разработчик не несёт ответственности 
за убытки, возникшие в результате использования.

3. РИСКИ
Торговля на финансовых рынках сопряжена с высокими рисками. 
Пользователь несёт полную ответственность за свои решения и настройки.

4. ЛИЦЕНЗИРОВАНИЕ
Лицензия привязана к Device ID. Запрещена передача третьим лицам.

5. КОНФИДЕНЦИАЛЬНОСТЬ
API-токены и данные кошельков шифруются и хранятся локально на устройстве. 
Разработчик не имеет к ним доступа.

НАЖИМАЯ "ПРИНЯТЬ", ВЫ ПОДТВЕРЖДАЕТЕ СОГЛАСИЕ С УСЛОВИЯМИ.
"""

def is_legal_accepted() -> bool:
    if os.path.exists(LEGAL_ACCEPTED_FILE):
        try:
            with open(LEGAL_ACCEPTED_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get('accepted', False)
        except:
            pass
    return False

def accept_legal():
    with open(LEGAL_ACCEPTED_FILE, 'w', encoding='utf-8') as f:
        json.dump({'accepted': True, 'timestamp': dt.now().isoformat(), 'version': '1.0'}, f)
    logger.info("✅ Пользователь принял условия соглашения")

PRESETS = {
    "КОНСЕРВАТИВНЫЙ": {'iq_threshold': 8.5, 'max_lots': 5, 'max_open_positions': 3, 'assets': {"SBER": True, "GOLD": True, "GAZP": False, "Si": False, "CNY": False}},
    "УМЕРЕННЫЙ": {'iq_threshold': 6.0, 'max_lots': 20, 'max_open_positions': 5, 'assets': {"SBER": True, "GAZP": True, "GOLD": True, "Si": True, "CNY": True}},
    "АГРЕССИВНЫЙ": {'iq_threshold': 3.0, 'max_lots': 100, 'max_open_positions': 10, 'assets': {"SBER": True, "GAZP": True, "GOLD": True, "Si": True, "CNY": True}}
}

def load_risk_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: pass
    return PRESETS["УМЕРЕННЫЙ"]

def save_risk_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f)

# ============================================================================
# 5. ASYNC-SAFE SQLITE ОЧЕРЕДЬ С RETRY
# ============================================================================
class OrderQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute('''CREATE TABLE IF NOT EXISTS pending_orders (
            order_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, side TEXT NOT NULL,
            qty INTEGER NOT NULL, price REAL NOT NULL, mkt TEXT NOT NULL,
            created_at REAL NOT NULL, status TEXT DEFAULT 'PENDING'
        )''')
        self.conn.commit()
    
    def _sync_add_order(self, order_id, ticker, side, qty, price, mkt):
        try:
            self.conn.execute('INSERT OR REPLACE INTO pending_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (order_id, ticker, side, qty, price, mkt, time.monotonic(), 'PENDING'))
            self.conn.commit()
        except Exception as e:
            logger.error(f"SQLite error: {e}")

    def _sync_remove_order(self, order_id):
        try:
            self.conn.execute('DELETE FROM pending_orders WHERE order_id = ?', (order_id,))
            self.conn.commit()
        except Exception as e:
            logger.error(f"SQLite error: {e}")

    async def add_order(self, order_id: str, ticker: str, side: str, qty: int, price: float, mkt: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_add_order, order_id, ticker, side, qty, price, mkt)
    
    async def remove_order(self, order_id: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_remove_order, order_id)
    
    def close(self):
        try:
            self.conn.close()
        except:
            pass

# ============================================================================
# 6. RING BUFFER
# ============================================================================
class TickRingBuffer:
    def __init__(self, size=1024):
        self.size = size
        self.mask = size - 1
        self.prices = [0.0] * size
        self.times = [0.0] * size
        self.head = 0

    def push(self, price: float, t: float):
        idx = self.head & self.mask
        self.prices[idx] = price
        self.times[idx] = t
        self.head += 1

    def count_recent(self, window_sec: float, current_time: float) -> int:
        count = 0
        for i in range(self.head - 1, max(-1, self.head - self.size - 1), -1):
            idx = i & self.mask
            if current_time - self.times[idx] < window_sec:
                count += 1
            else:
                break
        return count

# ============================================================================
# 7. TITAN MONOLITH CORE (ПОЛНАЯ МАТЕМАТИКА + RETRY)
# ============================================================================
BASE_ASSETS = ["SBER", "GAZP", "GOLD", "Si", "CNY"]
moscow_tz = pytz.timezone('Europe/Moscow')
Window.clearcolor = (0.1, 0.1, 0.15, 1)

# Константы для продвинутой математики
DAILY_LIMIT_PCT = 3.5
MARGIN_FACTOR = 0.15
MAX_SPREAD_LIMIT = 0.0006
IQ_STOCKS_THRESHOLD = 7.0
IQ_FUTURES_THRESHOLD = 3.0
VOL_BREATH_THRESHOLD = 0.4
DIANA_TIGHT_TRAIL = 0.0015

class TitanAbsoluteMonolith:
    ASSET_PARAMS = {
        "SBER": {"type": "STK", "comm_buffer": 0.0003}, 
        "GAZP": {"type": "STK", "comm_buffer": 0.0003},
        "GOLD": {"type": "FUT", "comm_fixed": 5.0, "min_step": 0.1, "step_val": 0.85},
        "Si": {"type": "FUT", "comm_fixed": 3.0, "min_step": 1.0, "step_val": 1.0},
        "CNY": {"type": "FX", "comm_buffer": 0.0006}, 
        "DEFAULT": {"type": "STK", "comm_buffer": 0.0005}
    }

    def __init__(self):
        self.tz = moscow_tz
        self.data = {"pos": {}, "limits": {"FUT": 50000.0, "STK": 10000.0, "FX": 5000.0}, 
                     "trade_history": [], "search_active": False}
        self._data_lock = asyncio.Lock()
        
        # === ПРОДВИНУТАЯ МАТЕМАТИКА СТАКАНА ===
        self.tick_buffers = {t: TickRingBuffer(1024) for t in BASE_ASSETS}
        self.level_eff_volumes = {t: {'bid': [0.0]*5, 'ask': [0.0]*5} for t in BASE_ASSETS}
        self.level_last_update = {t: {'bid': [0.0]*5, 'ask': [0.0]*5} for t in BASE_ASSETS}
        self.base_tick_rate = {t: 1.0 for t in BASE_ASSETS}
        
        self.warmup_ticks = {t: 0 for t in BASE_ASSETS}
        self.WARMUP_LIMIT = 100
        
        self.spread_stats = {t: {'mean': 0.0, 'var': 0.0} for t in BASE_ASSETS}
        self.SPREAD_ALPHA, self.SPREAD_K, self.SPREAD_MIN = 0.05, 1.5, 0.0002
        
        self.vol_stats = {t: {'mean': 0.0, 'var': 0.0} for t in BASE_ASSETS}
        self.VOL_ALPHA, self.VOL_SENSITIVITY = 0.05, 0.5
        
        self.iq_slow = {t: 5.0 for t in BASE_ASSETS}
        self.IQ_SLOW_GAMMA = 0.05
        
        self.price_history = {t: [] for t in BASE_ASSETS}
        self.iq_history = {t: [] for t in BASE_ASSETS}
        self.range_history = {t: [] for t in BASE_ASSETS}
        self.last_tick_price = {t: 0.0 for t in BASE_ASSETS}
        # ========================================
        
        self.jwt, self.jwt_expiry = "", 0
        self._jwt_lock = asyncio.Lock()
        self._http: Optional[aiohttp.ClientSession] = None
        self._loop = None
        self.ws_connected = False
        self.order_queue = OrderQueue(os.path.join(LOG_DIR, "pending_orders.db"))
        
        self.settings_lock = threading.Lock()
        self.runtime_settings = load_risk_settings()

    def run_async_threadsafe(self, coro):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def update_runtime_settings(self, new_settings):
        with self.settings_lock:
            self.runtime_settings = new_settings
        logger.info(f"⚙️ Настройки риск-профиля обновлены: {new_settings.get('profile')}")

    # === RETRY-ЛОГИКА ДЛЯ API ===
    async def safe_get(self, url: str, retries: int = 3) -> Optional[dict]:
        for attempt in range(retries):
            try:
                async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status == 200:
                        return await r.json()
                    elif r.status in (429, 503):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        logger.error(f"API error {r.status}")
                        break
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on {url}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Request error: {e}")
                await asyncio.sleep(1)
        return None

    async def _ensure_jwt(self) -> bool:
        async with self._jwt_lock:
            if time.monotonic() < self.jwt_expiry and self.jwt: return True
            try:
                creds = load_user_credentials()
                token = creds.get("token", "")
                if not token:
                    logger.warning("️ Токен не найден. Введите его в настройках.")
                    return False
                
                url = f"https://oauth.alor.ru/refresh?token={token}"
                async with self._http.post(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        body = await r.json()
                        self.jwt = body.get('AccessToken', '')
                        self.jwt_expiry = time.monotonic() + 1100
                        return True
                    else:
                        logger.error(f"JWT refresh HTTP {r.status}")
            except Exception as e: 
                logger.error(f"JWT error: {e}")
            return False

    async def start(self):
        self._http = aiohttp.ClientSession()
        start_foreground_service()
        logger.info("✅ TITAN запущен с полной математикой!")

    async def stop(self):
        self.order_queue.close()
        if self._http and not self._http.closed:
            await self._http.close()

    # === ВОССТАНОВЛЕННЫЕ МАТЕМАТИЧЕСКИЕ МЕТОДЫ ===
    def _update_ema_stats(self, stats_dict, value, alpha):
        diff = value - stats_dict['mean']
        stats_dict['mean'] += alpha * diff
        stats_dict['var'] = (1.0 - alpha) * (stats_dict['var'] + alpha * diff * diff)

    def get_adaptive_spread_threshold(self, ticker: str) -> float:
        stats = self.spread_stats[ticker]
        sigma = math.sqrt(stats['var']) if stats['var'] > 0 else 0.0
        return max(self.SPREAD_MIN, stats['mean'] + self.SPREAD_K * sigma)

    def get_adaptive_iq_threshold(self, ticker: str, base_threshold: float) -> float:
        stats = self.vol_stats[ticker]
        vol_deviation = (math.sqrt(stats['var']) - stats['mean']) / stats['mean'] if stats['mean'] > 0 else 0.0
        return base_threshold * (1.0 + self.VOL_SENSITIVITY * max(0, vol_deviation))

    def check_volatility(self, ticker: str, price: float) -> bool:
        hist = self.range_history[ticker]
        hist.append(price)
        if len(hist) > 600: hist.pop(0)
        if len(hist) < 300: return True
        sw = hist[-60:]
        sr = max(sw) - min(sw)
        lr = (max(hist) - min(hist)) / 10.0
        return sr >= (lr * VOL_BREATH_THRESHOLD)

    def is_logical_trade(self, ticker: str, snap: dict) -> bool:
        if snap["bid_power"] < 10 or snap["ask_power"] < 10: return False
        if snap["bid_power"] > snap["ask_power"] * 50: return False
        return True

    def analyze_book(self, ticker: str, bids: list, asks: list, now: float) -> Optional[dict]:
        if not bids or not asks: return None
        
        # 1. Расчет текущей частоты тиков и адаптивного затухания (lam)
        current_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        self.base_tick_rate[ticker] = 0.9 * self.base_tick_rate[ticker] + 0.1 * current_rate
        tau_base = 3.0
        rate_ratio = current_rate / max(self.base_tick_rate[ticker], 0.1)
        lam = (1.0 / tau_base) * (0.5 + 0.5 * min(rate_ratio, 3.0))
        lam = min(0.8, max(0.3, lam))
        
        eff_bid_vol, eff_ask_vol = 0.0, 0.0
        
        # 2. Экспоненциальное затухание объемов по 5 уровням стакана
        for i in range(5):
            if i < len(bids):
                dt_time = now - self.level_last_update[ticker]['bid'][i]
                decayed = self.level_eff_volumes[ticker]['bid'][i] * math.exp(-lam * dt_time)
                self.level_eff_volumes[ticker]['bid'][i] = decayed + bids[i]['volume']
                self.level_last_update[ticker]['bid'][i] = now
                eff_bid_vol += self.level_eff_volumes[ticker]['bid'][i]
            else:
                dt_time = now - self.level_last_update[ticker]['bid'][i]
                self.level_eff_volumes[ticker]['bid'][i] *= math.exp(-lam * dt_time)
                eff_bid_vol += self.level_eff_volumes[ticker]['bid'][i]
                
            if i < len(asks):
                dt_time = now - self.level_last_update[ticker]['ask'][i]
                decayed = self.level_eff_volumes[ticker]['ask'][i] * math.exp(-lam * dt_time)
                self.level_eff_volumes[ticker]['ask'][i] = decayed + asks[i]['volume']
                self.level_last_update[ticker]['ask'][i] = now
                eff_ask_vol += self.level_eff_volumes[ticker]['ask'][i]
            else:
                dt_time = now - self.level_last_update[ticker]['ask'][i]
                self.level_eff_volumes[ticker]['ask'][i] *= math.exp(-lam * dt_time)
                eff_ask_vol += self.level_eff_volumes[ticker]['ask'][i]
                
        total_eff = eff_bid_vol + eff_ask_vol
        static_iq = ((eff_bid_vol - eff_ask_vol) / total_eff + 1) * 5 if total_eff > 0 else 5.0
        
        return {
            "static_iq": static_iq, 
            "bid_power": eff_bid_vol, 
            "ask_power": eff_ask_vol,
            "bid_wall": bids[0]['price'] if eff_bid_vol > 0 else 0,
            "ask_wall": asks[0]['price'] if eff_ask_vol > 0 else 0
        }

    async def exit_trade(self, ticker: str, price: float, reason: str, prof: float):
        async with self._data_lock:
            plist = self.data["pos"]
            ll = self.data["limits"]
            p = plist.get(ticker)
            if not p: return
        
        logger.info(f"🚪 ЗАКРЫТИЕ: {ticker} ({reason}) @ {price} | PnL: {prof:.2f}%")
        
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])
        if spec["type"] == "FUT":
            pts = (price - p["p"]) if p["side"] == "BUY" else (p["p"] - price)
            net = round((pts / spec.get("min_step", 1.0)) * spec.get("step_val", 1.0) * p["lot"] - spec.get("comm_fixed", 2.0) * p["lot"], 2)
        else:
            pos_val = p["lot"] * p["p"]
            net = round(pos_val * prof - pos_val * spec.get("comm_buffer", 0.0005), 2)
            
        async with self._data_lock:
            ll[p["mkt"]] = round(ll.get(p["mkt"], 0.0) + p.get("frozen_margin", 0.0) + net, 2)
            trade_record = {
                "ticker": ticker, "side": p["side"], "entry_price": p["p"],
                "exit_price": price, "lot": p["lot"], "pnl": net,
                "reason": reason, "time": dt.now(self.tz).strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "REAL"
            }
            self.data["trade_history"].append(trade_record)
            if len(self.data["trade_history"]) > 50: 
                self.data["trade_history"] = self.data["trade_history"][-50:]
            del plist[ticker]
        logger.info(f"💰 ВЫХОД {ticker} ({reason}) | PnL: {net}р")

    async def process_tick(self, ticker: str, price: float, book: dict):
        now = time.monotonic()
        
        # Безопасное чтение настроек
        with self.settings_lock:
            current_iq_threshold = self.runtime_settings['iq_threshold']
            current_max_lots = self.runtime_settings['max_lots']
            current_max_pos = self.runtime_settings['max_open_positions']
            active_assets = self.runtime_settings['assets']
            
        if not active_assets.get(ticker, False): return

        self.tick_buffers[ticker].push(price, now)
        self.price_history[ticker].append(price)
        if len(self.price_history[ticker]) > 600:
            self.price_history[ticker].pop(0)
        
        bids, asks = book.get('bids', []), book.get('asks', [])
        if not bids or not asks: return
        
        # Расчет спреда и обновление статистики
        current_spread = (asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 0.001)
        prev_p = self.last_tick_price[ticker]
        price_change = abs(price - prev_p) if prev_p > 0 else 0.0
        self.last_tick_price[ticker] = price
        
        self._update_ema_stats(self.spread_stats[ticker], current_spread, self.SPREAD_ALPHA)
        self._update_ema_stats(self.vol_stats[ticker], price_change, self.VOL_ALPHA)
        
        if self.warmup_ticks[ticker] < self.WARMUP_LIMIT:
            self.warmup_ticks[ticker] += 1
            return
            
        if current_spread > self.get_adaptive_spread_threshold(ticker): 
            return

        snap = self.analyze_book(ticker, bids, asks, now)
        if not snap or not self.is_logical_trade(ticker, snap): return
        
        is_fut = ticker in ("GOLD", "Si")
        is_fx = ticker == "CNY"
        mkt = "FUT" if is_fut else ("FX" if is_fx else "STK")
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])

        # === ГЕНИАЛЬНЫЙ РАСЧЁТ IQ ===
        tick_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        tick_factor = math.tanh(tick_rate / 15.0)
        static_iq = snap["static_iq"]
        
        target_time = now - 1.5
        prev_price = price
        buf = self.tick_buffers[ticker]
        for i in range(buf.head - 1, max(-1, buf.head - buf.size - 1), -1):
            idx = i & buf.mask
            if buf.times[idx] <= target_time:
                prev_price = buf.prices[idx]
                break
                
        price_delta = price - prev_price
        book_delta = snap["bid_power"] - snap["ask_power"]
        delta_ratio = abs(price_delta) / current_spread if current_spread > 0 else 0
        
        # Коэффициент истинности движения (c_truth)
        if (price_delta > 0 and book_delta > 0) or (price_delta < 0 and book_delta < 0):
            c_truth = min(1.2, 1.0 + 0.2 * delta_ratio)
        elif price_delta != 0: 
            c_truth = max(0.7, 1.0 - 0.3 * delta_ratio)
        else: 
            c_truth = 1.0
            
        raw_iq = static_iq * (0.3 + 0.7 * tick_factor) * c_truth
        fast_iq_prev = self.iq_history[ticker][-1] if self.iq_history[ticker] else raw_iq
        gamma_fast = 0.2 + 0.6 * tick_factor
        cur_iq = gamma_fast * raw_iq + (1 - gamma_fast) * fast_iq_prev
        
        iq_slow_prev = self.iq_slow[ticker]
        self.iq_slow[ticker] = (1.0 - self.IQ_SLOW_GAMMA) * iq_slow_prev + self.IQ_SLOW_GAMMA * cur_iq
        iq_diff = cur_iq - self.iq_slow[ticker]
        c_trend = 1.0 + 0.3 * math.tanh(iq_diff / 2.0)
        final_iq = cur_iq * c_trend
        
        iqh = self.iq_history[ticker]
        iqh.append(final_iq)
        if len(iqh) > 15: iqh.pop(0)
        # ===========================

        need_exit, exit_price, exit_reason, exit_prof = False, 0.0, "", 0.0
        
        async with self._data_lock:
            active_pos = self.data["pos"]
            active_limits = self.data["limits"]
            
            if ticker in active_pos:
                p = active_pos[ticker]
                prof = ((price - p["p"]) / p["p"]) if p["side"] == "BUY" else ((p["p"] - price) / p["p"])
                hold_time = (dt.now(self.tz) - dt.fromtimestamp(p.get("entry_time", time.time()), self.tz)).seconds
                
                exit_condition = False
                # 1. Отскок от стены
                if p["side"] == "BUY" and snap["ask_wall"] > 0 and price < snap["ask_wall"] and final_iq < 0.8:
                    exit_reason, exit_prof, exit_condition = "WALL-REJECTION", prof, True
                # 2. Разворот против позиции
                cr = spec.get("comm_buffer", 0.0005)
                if not exit_condition and final_iq < 1.0 and prof < -(cr * 1.5):
                    exit_reason, exit_prof, exit_condition = "REVERSAL-EXIT", prof, True
                # 3. Динамический выход по падению IQ
                if not exit_condition:
                    hold_mult = 0.50 if hold_time < 15 else (0.80 if prof < 0.0015 else 0.60)
                    if final_iq <= p.get("entry_iq_real", 3.0) * hold_mult:
                        exit_reason, exit_prof, exit_condition = "IQ-DYNAMIC-EXIT", prof, True
                        
                if exit_condition: 
                    need_exit, exit_price = True, price
                    
                # 4. Трейлинг-стоп (Diana Tight Trail)
                if not need_exit and prof > 0.003:
                    tr = p["p"] + (p["p"] * DIANA_TIGHT_TRAIL) if p["side"] == "BUY" else p["p"] - (p["p"] * DIANA_TIGHT_TRAIL)
                    if (price > tr and p["side"] == "BUY") or (price < tr and p["side"] == "SELL"): 
                        p["p"] = tr
            else:
                # ЛОГИКА ОТКРЫТИЯ ПОЗИЦИИ
                if not self.data.get("search_active", False): return
                if active_limits.get(mkt, 0) <= 0: return
                if not self.check_volatility(ticker, price): return
                if len(active_pos) >= current_max_pos: return
                
                base_thr = IQ_FUTURES_THRESHOLD if ticker in ("GOLD", "Si") else IQ_STOCKS_THRESHOLD
                iq_thr = self.get_adaptive_iq_threshold(ticker, base_thr)
                
                effective_iq_thr = max(iq_thr, current_iq_threshold)
                if final_iq < effective_iq_thr: return
                
                side = "BUY" if snap["bid_power"] > snap["ask_power"] else "SELL"
                risk = MARGIN_FACTOR * active_limits.get(mkt, 10000)
                
                lot = min(max(1, int(risk / (price * 0.01))), current_max_lots)
                
                best_level_vol = bids[0]['volume'] if side == "BUY" else asks[0]['volume']
                slippage_ratio = lot / max(best_level_vol, 1.0)
                
                expected_slippage = (current_spread / 2.0) + 0.1 * (slippage_ratio ** 2)
                iq_excess = final_iq - effective_iq_thr
                potential_profit_ratio = max(0.0, iq_excess * 0.0005)
                
                if expected_slippage > potential_profit_ratio * 0.5:
                    return
                
                frozen = (risk * 0.3) + (spec.get("comm_fixed", 2.0) * lot)
                active_limits[mkt] = round(active_limits.get(mkt, 0.0) - frozen, 2)
                
                pos = {
                    "ticker": ticker, "side": side, "lot": lot, "p": price, "mkt": mkt,
                    "entry_time": time.time(), "frozen_margin": frozen,
                    "comm_paid": 0.0, "entry_iq_real": final_iq, "peak_iq": final_iq, "max_prof": 0.0
                }
                active_pos[ticker] = pos
                logger.info(f" ОТКРЫТА: {ticker} ({side}) {lot} лотов @ {price} | IQ: {final_iq:.2f} | Slippage: {expected_slippage:.4f}")
        
        if need_exit: 
            await self.exit_trade(ticker, exit_price, exit_reason, exit_prof)

    async def http_polling_loop(self):
        logger.info("🔄 HTTP polling fallback запущен с retry-логикой")
        while True:
            try:
                if self.ws_connected:
                    await asyncio.sleep(5)
                    continue
                if not await self._ensure_jwt():
                    await asyncio.sleep(5); continue
                
                for ticker in BASE_ASSETS:
                    url = f"https://api.alor.ru/md/v2/Securities/MOEX/{ticker}/orderbook?depth=10"
                    data = await self.safe_get(url, retries=3)
                    if data:
                        bids, asks = data.get('bids', []), data.get('asks', [])
                        if bids and asks:
                            price = (bids[0]['price'] + asks[0]['price']) / 2
                            await self.process_tick(ticker, price, {"bids": bids, "asks": asks})
                
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"HTTP polling error: {e}")
                await asyncio.sleep(5)

async def ws_market_data_feed(bot: TitanAbsoluteMonolith):
    retry_count = 0
    while True:
        try:
            await asyncio.sleep(10)
        except Exception as e:
            bot.ws_connected = False
            await asyncio.sleep(2 ** min(retry_count, 5))
            retry_count += 1

# ============================================================================
# 8. UI ЭКРАНЫ
# ============================================================================
class LegalScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=15, spacing=10)
        layout.add_widget(Label(text="TITAN Pro — Правовая информация", font_size=dp(20), bold=True, size_hint_y=None, height=dp(50)))
        
        scroll = ScrollView(size_hint=(1, 1))
        legal_label = Label(text=LEGAL_TEXT, font_size=dp(12), halign='left', valign='top', padding=[10, 10, 10, 10], size_hint_y=None)
        legal_label.bind(texture_size=legal_label.setter('size'))
        scroll.add_widget(legal_label)
        layout.add_widget(scroll)
        
        checkbox_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=10)
        self.checkbox = CheckBox(active=False, size_hint_x=None, width=dp(40))
        checkbox_layout.add_widget(self.checkbox)
        checkbox_layout.add_widget(Label(text="Я принимаю условия соглашения", font_size=dp(12)))
        layout.add_widget(checkbox_layout)
        
        self.continue_btn = Button(text="ПРОДОЛЖИТЬ", font_size=dp(16), bold=True, background_color=(0.5, 0.5, 0.5, 1), size_hint_y=None, height=dp(50), disabled=True)
        self.continue_btn.bind(on_press=self.on_accept)
        layout.add_widget(self.continue_btn)
        
        self.checkbox.bind(active=self.on_checkbox_change)
        self.add_widget(layout)
    
    def on_checkbox_change(self, instance, value):
        if value:
            self.continue_btn.disabled = False
            self.continue_btn.background_color = (0.2, 0.8, 0.2, 1)
        else:
            self.continue_btn.disabled = True
            self.continue_btn.background_color = (0.5, 0.5, 0.5, 1)
    
    def on_accept(self, instance):
        if self.checkbox.active:
            accept_legal()
            self.manager.current = 'activation'

class ActivationScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=20, spacing=15)
        layout.add_widget(Label(text="TITAN Pro — Активация", font_size=dp(24), bold=True, size_hint_y=None, height=dp(50)))
        layout.add_widget(Label(text=f"Device ID: {DEVICE_ID}", font_size=dp(16), color=(0.2, 0.8, 1, 1), bold=True))
        layout.add_widget(Label(text="Отправьте этот ID для получения ключа", font_size=dp(14)))
        
        self.key_input = TextInput(hint_text='Введите лицензионный ключ', font_size=dp(16), multiline=False, size_hint_y=None, height=dp(50))
        layout.add_widget(self.key_input)
        
        activate_btn = Button(text="АКТИВИРОВАТЬ", font_size=dp(18), bold=True, background_color=(0.2, 0.8, 0.2, 1), size_hint_y=None, height=dp(60))
        activate_btn.bind(on_press=self.activate)
        layout.add_widget(activate_btn)
        
        self.status_lbl = Label(text="", font_size=dp(14), size_hint_y=None, height=dp(30))
        layout.add_widget(self.status_lbl)
        self.add_widget(layout)

    def activate(self, instance):
        key_text = self.key_input.text.strip()
        if not key_text:
            self.status_lbl.text = "Введите ключ!"
            self.status_lbl.color = (0.9, 0.2, 0.2, 1)
            return
        
        expected_key = hashlib.sha256(f"{DEVICE_ID}{LICENSE_SECRET}".encode()).hexdigest()[:32]
        if key_text == expected_key:
            with open(LICENSE_FILE, 'w', encoding='utf-8') as f:
                f.write(key_text)
            global IS_LICENSED
            IS_LICENSED = True
            self.status_lbl.text = "✅ Успешно!"
            self.status_lbl.color = (0.2, 0.9, 0.2, 1)
            Clock.schedule_once(lambda dt: setattr(self.manager, 'current', 'credentials'), 1.0)
        else:
            self.status_lbl.text = "❌ Неверный ключ!"
            self.status_lbl.color = (0.9, 0.2, 0.2, 1)

class CredentialsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=20, spacing=15)
        layout.add_widget(Label(text="Настройки подключения", font_size=dp(22), bold=True, size_hint_y=None, height=dp(50)))
        
        self.creds = load_user_credentials()
        def create_input(label_text, hint_text, current_val):
            box = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(70))
            box.add_widget(Label(text=label_text, font_size=dp(14), color=(0.8, 0.8, 0.8, 1), size_hint_y=None, height=dp(20)))
            inp = TextInput(text=current_val, hint_text=hint_text, font_size=dp(14), multiline=False, background_color=(0.15, 0.15, 0.2, 1))
            box.add_widget(inp)
            return box, inp

        layout.add_widget(Label(text="Введите ваши данные Alor API:", font_size=dp(14), color=(0.6, 0.8, 1, 1), size_hint_y=None, height=dp(20)))
        _, self.in_token = create_input("API Token:", "Ваш токен Alor", self.creds.get("token", ""))
        layout.add_widget(_[0])
        _, self.in_fut = create_input("Кошелек FUT (Срочный):", "Например: 7502Y5H", self.creds.get("fut", "7502Y5H"))
        layout.add_widget(_[0])
        _, self.in_stk = create_input("Кошелек STK (Акции):", "Например: D101327", self.creds.get("stk", "D101327"))
        layout.add_widget(_[0])
        _, self.in_fx = create_input("Кошелек FX (Валюта):", "Например: G68390", self.creds.get("fx", "G68390"))
        layout.add_widget(_[0])

        save_btn = Button(text="СОХРАНИТЬ И ПРОДОЛЖИТЬ", font_size=dp(16), bold=True, background_color=(0.2, 0.8, 0.2, 1), size_hint_y=None, height=dp(50))
        save_btn.bind(on_press=self.save_and_continue)
        layout.add_widget(save_btn)
        
        self.status_lbl = Label(text="", font_size=dp(14), size_hint_y=None, height=dp(30))
        layout.add_widget(self.status_lbl)
        self.add_widget(layout)

    def save_and_continue(self, instance):
        token, fut, stk, fx = self.in_token.text.strip(), self.in_fut.text.strip(), self.in_stk.text.strip(), self.in_fx.text.strip()
        if not token:
            self.status_lbl.text, self.status_lbl.color = "❌ Token обязателен!", (0.9, 0.2, 0.2, 1)
            return
        save_user_credentials(token, fut, stk, fx)
        self.status_lbl.text, self.status_lbl.color = "✅ Данные сохранены!", (0.2, 0.9, 0.2, 1)
        Clock.schedule_once(lambda dt: setattr(self.manager, 'current', 'risk_profile'), 1.0)

class RiskProfileScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=20, spacing=15)
        layout.add_widget(Label(text="Выберите Риск-Профиль", font_size=dp(24), bold=True, size_hint_y=None, height=dp(50)))
        
        profiles = [
            {"name": "КОНСЕРВАТИВНЫЙ", "desc": "IQ > 8.5 | Макс 5 лотов", "color": (0.2, 0.8, 0.2, 1)},
            {"name": "УМЕРЕННЫЙ", "desc": "IQ > 6.0 | Макс 20 лотов", "color": (0.2, 0.6, 0.9, 1)},
            {"name": "АГРЕССИВНЫЙ", "desc": "IQ > 3.0 | Макс 100 лотов", "color": (0.9, 0.2, 0.2, 1)}
        ]
        
        for p in profiles:
            btn = Button(text=f"{p['name']}\n{p['desc']}", font_size=dp(14), background_color=p['color'], size_hint_y=None, height=dp(80))
            btn.bind(on_press=lambda x, profile=p['name']: self.apply_profile(profile))
            layout.add_widget(btn)
        self.add_widget(layout)

    def apply_profile(self, profile_name):
        if profile_name not in PRESETS: return
        settings = PRESETS[profile_name].copy()
        settings['profile'] = profile_name
        save_risk_settings(settings)
        app = App.get_running_app()
        if app and app.bot:
            app.bot.update_runtime_settings(settings)
        self.manager.current = 'dashboard'

class DashboardScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=10, spacing=5)
        layout.add_widget(Label(text="TITAN Pro Client", font_size=dp(28), bold=True, size_hint_y=None, height=dp(50)))
        self.status_label = Label(text="Статус: ОЖИДАНИЕ", font_size=dp(18), size_hint_y=None, height=dp(40))
        layout.add_widget(self.status_label)
        
        btn_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=10)
        self.start_btn = Button(text="СТАРТ", font_size=dp(16), bold=True, background_color=(0.2, 0.8, 0.2, 1))
        self.start_btn.bind(on_press=self.start_trading)
        self.stop_btn = Button(text="СТОП", font_size=dp(16), bold=True, background_color=(0.9, 0.2, 0.2, 1))
        self.stop_btn.bind(on_press=self.stop_trading)
        btn_layout.add_widget(self.start_btn)
        btn_layout.add_widget(self.stop_btn)
        layout.add_widget(btn_layout)
        
        self.info_label = Label(text="Бот готов к работе. Нажмите СТАРТ.", font_size=dp(14), size_hint_y=None, height=dp(100))
        layout.add_widget(self.info_label)
        self.add_widget(layout)

    def start_trading(self, instance):
        if self.bot:
            self.bot.data["search_active"] = True
            self.status_label.text = "Статус: АКТИВЕН ✅"
            self.status_label.color = (0.2, 0.9, 0.2, 1)
            self.info_label.text = "Торговля запущена. Мониторинг рынка..."

    def stop_trading(self, instance):
        if self.bot:
            self.bot.data["search_active"] = False
            self.status_label.text = "Статус: ПАУЗА ⏸"
            self.status_label.color = (1, 1, 1, 1)
            self.info_label.text = "Торговля приостановлена."

# ============================================================================
# 9. APP & ЗАПУСК
# ============================================================================
class TITANProApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = "TITAN Pro"
        self.bot = TitanAbsoluteMonolith()
        self._loop_thread = None

    def build(self):
        global LOG_DIR
        LOG_DIR = self.user_data_dir
        get_file_paths()
        get_settings_paths()
        setup_logging()
        
        sm = ScreenManager()
        if not is_legal_accepted():
            sm.add_widget(LegalScreen(name='legal'))
            sm.current = 'legal'
        elif not IS_LICENSED:
            sm.add_widget(ActivationScreen(name='activation'))
            sm.current = 'activation'
        elif not os.path.exists(USER_DATA_FILE) or not load_user_credentials().get("token"):
            sm.add_widget(CredentialsScreen(name='credentials'))
            sm.current = 'credentials'
        elif not os.path.exists(SETTINGS_FILE):
            sm.add_widget(RiskProfileScreen(name='risk_profile'))
            sm.current = 'risk_profile'
        else:
            dash = DashboardScreen(name='dashboard')
            dash.bot = self.bot
            sm.add_widget(dash)
            sm.current = 'dashboard'
            Clock.schedule_once(self.start_bot, 0.5)
        return sm

    def start_bot(self, dt):
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.async_main())
        self._loop_thread = threading.Thread(target=run_loop, daemon=True)
        self._loop_thread.start()
        logger.info("✅ Асинхронный поток запущен")

    async def async_main(self):
        self.bot._loop = asyncio.get_running_loop()
        await self.bot.start()
        await asyncio.gather(
            ws_market_data_feed(self.bot),
            self.bot.http_polling_loop(),
            return_exceptions=True
        )

    def on_stop(self):
        if self.bot:
            logger.info("🛑 Остановка бота...")

def show_error_popup(error_msg):
    try:
        content = BoxLayout(orientation='vertical', padding=10)
        scroll = ScrollView()
        label = Label(text=error_msg, halign='left', valign='top', padding=10, size_hint_y=None)
        label.bind(texture_size=label.setter('size'))
        scroll.add_widget(label)
        content.add_widget(scroll)
        btn = Button(text='Закрыть приложение', size_hint_y=None, height=50, background_color=(0.8, 0.2, 0.2, 1))
        content.add_widget(btn)
        popup = Popup(title=' ОШИБКА TITAN PRO', content=content, size_hint=(0.95, 0.8), auto_dismiss=False)
        btn.bind(on_press=lambda x: (popup.dismiss(), sys.exit(1)))
        popup.open()
    except: pass

if __name__ == '__main__':
    try:
        TITANProApp().run()
    except Exception as e:
        error_txt = f"{traceback.format_exc()}"
        logger.critical(error_txt)
        show_error_popup(error_txt)
        sys.exit(1)
