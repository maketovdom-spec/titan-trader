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
from datetime import datetime as dt
from typing import Dict, Optional, List

import aiohttp
import pytz

# Kivy imports
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.metrics import dp

# ============================================================================
# 1. ANDROID WAKE LOCK
# ============================================================================
HAS_ANDROID_WAKELOCK = False
wake_lock = None
try:
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Context = autoclass('android.content.Context')
    PowerManager = autoclass('android.os.PowerManager')
    
    activity = PythonActivity.mActivity
    power_manager = activity.getSystemService(Context.POWER_SERVICE)
    wake_lock = power_manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TITAN:TradingLock")
    HAS_ANDROID_WAKELOCK = True
    logging.info("✅ Android WakeLock инициализирован")
except ImportError:
    logging.info("⚠️ Запуск не на Android, WakeLock отключен")

# ============================================================================
# 2. DEVICE ID & ЛИЦЕНЗИОННАЯ ЗАЩИТА
# ============================================================================
DEVICE_ID = "UNKNOWN"
try:
    from jnius import autoclass
    Settings = autoclass('android.provider.Settings$Secure')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    activity = PythonActivity.mActivity
    content_resolver = activity.getContentResolver()
    android_id = Settings.Secure.getString(content_resolver, Settings.Secure.ANDROID_ID)
    DEVICE_ID = hashlib.sha256(android_id.encode()).hexdigest()[:16]
    logging.info(f"✅ Device ID: {DEVICE_ID}")
except Exception as e:
    logging.warning(f"⚠️ Не удалось получить Device ID: {e}")

LICENSE_SECRET = "TITAN_NEVINNOMYSSK_2026_SECRET_KEY"
LICENSE_FILE = os.path.join('.', "titan_license.key")
USER_DATA_FILE = os.path.join('.', "titan_user_data.enc")

def check_license_status() -> bool:
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, 'r', encoding='utf-8') as f:
                stored_key = f.read().strip()
            expected_key = hashlib.sha256(f"{DEVICE_ID}{LICENSE_SECRET}".encode()).hexdigest()[:32]
            if stored_key == expected_key:
                logging.info("✅ Лицензия активна и валидна")
                return True
        except Exception as e:
            logging.error(f"Ошибка чтения лицензии: {e}")
    return False

IS_LICENSED = check_license_status()

# ============================================================================
# 3. ШИФРОВАНИЕ ДАННЫХ ПОЛЬЗОВАТЕЛЯ
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

def load_user_credentials() -> dict:
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, 'rb') as f:
            return decrypt_user_data(f.read())
    return {"token": "", "fut": "7502Y5H", "stk": "D101327", "fx": "G68390"}

# ============================================================================
# 4. ASYNC-SAFE SQLITE ОЧЕРЕДЬ
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
        self.conn.execute('INSERT OR REPLACE INTO pending_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (order_id, ticker, side, qty, price, mkt, time.monotonic(), 'PENDING'))
        self.conn.commit()

    def _sync_remove_order(self, order_id):
        self.conn.execute('DELETE FROM pending_orders WHERE order_id = ?', (order_id,))
        self.conn.commit()

    def _sync_get_pending(self):
        cursor = self.conn.execute('SELECT * FROM pending_orders ORDER BY created_at')
        return [{'order_id': r[0], 'ticker': r[1], 'side': r[2], 'qty': r[3], 
                 'price': r[4], 'mkt': r[5], 'created_at': r[6], 'status': r[7]} 
                for r in cursor.fetchall()]

    async def add_order(self, order_id: str, ticker: str, side: str, qty: int, price: float, mkt: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_add_order, order_id, ticker, side, qty, price, mkt)
    
    async def remove_order(self, order_id: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_remove_order, order_id)
    
    async def get_pending_orders(self) -> List[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_get_pending)
    
    def close(self):
        self.conn.close()

# ============================================================================
# 5. LOCK-FRIENDLY RING BUFFER
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
# 6. КОНФИГУРАЦИЯ
# ============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("TITAN")

USER_CREDS = load_user_credentials()

CONFIG = {
    "ALOR_TOKEN": USER_CREDS.get("token", ""),
    "SALT": os.getenv("TITAN_SALT", "NEVINNOMYSSK_TITAN_2026"),
    "MODE": "REAL",
}

DATA_DIR = '.'
STATE_FILE = os.path.join(DATA_DIR, "titan_monolith.json")
STATE_TMP_FILE = os.path.join(DATA_DIR, "titan_monolith.tmp.json")
ORDER_QUEUE_DB = os.path.join(DATA_DIR, "pending_orders.db")

PORTFOLIOS = {
    "FUT": USER_CREDS.get("fut", "7502Y5H"),
    "STK": USER_CREDS.get("stk", "D101327"),
    "FX": USER_CREDS.get("fx", "G68390")
}

class ClientProfile:
    def __init__(self, profile_id: str = "DEFAULT"):
        self.profile_id = profile_id
        self.seed = int(hashlib.md5(profile_id.encode()).hexdigest()[:8], 16)
        self.iq_mult = 0.85 + (self.seed % 30) / 100.0
        self.trail_mult = 0.80 + ((self.seed >> 4) % 40) / 100.0
        self.size_mult = 0.70 + ((self.seed >> 8) % 60) / 100.0
        all_assets = ["SBER", "GAZP", "Si", "CNY", "GOLD", "VTBR", "MGNT", "LKOH"]
        self.assets = random.Random(self.seed).sample(all_assets, 5)

PROFILE = ClientProfile("CLIENT_DEFAULT")
BASE_ASSETS = PROFILE.assets
moscow_tz = pytz.timezone('Europe/Moscow')
Window.clearcolor = (0.1, 0.1, 0.15, 1)

DAILY_LIMIT_PCT = 3.5
MARGIN_FACTOR = 0.15
MAX_SPREAD_LIMIT = 0.0006
IQ_STOCKS_THRESHOLD = 7.0 * PROFILE.iq_mult
IQ_FUTURES_THRESHOLD = 3.0
VOL_BREATH_THRESHOLD = 0.4
DIANA_TIGHT_TRAIL = 0.0015 * PROFILE.trail_mult
MAX_POSITION_LOTS = int(1000 * PROFILE.size_mult)
MAX_OPEN_POSITIONS = 5

# ============================================================================
# 7. TITAN MONOLITH CORE
# ============================================================================
class TitanAbsoluteMonolith:
    ASSET_PARAMS = {
        "SBER": {"type": "STK", "comm_buffer": 0.0003}, "GAZP": {"type": "STK", "comm_buffer": 0.0003},
        "GOLD": {"type": "FUT", "comm_fixed": 5.0, "min_step": 0.1, "step_val": 0.85},
        "Si": {"type": "FUT", "comm_fixed": 3.0, "min_step": 1.0, "step_val": 1.0},
        "CNY": {"type": "FX", "comm_buffer": 0.0006}, "DEFAULT": {"type": "STK", "comm_buffer": 0.0005}
    }

    def __init__(self):
        self.tz = moscow_tz
        self.data = {"pos": {}, "test_pos": {}, "limits": {"FUT": 50000.0, "STK": 10000.0, "FX": 5000.0}, 
                     "test_limits": {"FUT": 100000.0, "STK": 100000.0, "FX": 100000.0}, "trade_history": [], "balance": 0.0, "search_active": False}
        self._data_lock = asyncio.Lock()
        
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
        self.last_tick_price = {t: 0.0 for t in BASE_ASSETS}

        self.iq_slow = {t: 5.0 for t in BASE_ASSETS}
        self.IQ_SLOW_GAMMA = 0.05

        self.price_history = {t: [] for t in BASE_ASSETS}
        self.iq_history = {t: [] for t in BASE_ASSETS}
        self.range_history = {t: [] for t in BASE_ASSETS}

        self.jwt, self.jwt_expiry = "", 0
        self._jwt_lock = asyncio.Lock()
        self._http: Optional[aiohttp.ClientSession] = None
        self._loop = None
        
        self.is_processing = False
        self.ws_connected = False
        
        self.order_queue = OrderQueue(ORDER_QUEUE_DB)
        self.sent_order_ids: Dict[str, float] = {}

    def run_async_threadsafe(self, coro):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            logger.error("Async loop not running")

    async def get_safe_data(self):
        async with self._data_lock:
            market_data = {}
            for ticker in BASE_ASSETS:
                iq_hist = self.iq_history.get(ticker, [])
                price_hist = self.price_history.get(ticker, [])
                market_data[ticker] = {
                    "iq": iq_hist[-1] if iq_hist else 0,
                    "price": price_hist[-1] if price_hist else 0,
                    "iq_slow": self.iq_slow.get(ticker, 5.0),
                    "warmup": self.warmup_ticks.get(ticker, 0)
                }
            return {
                "total_pnl": self.data.get("total_pnl", 0.0),
                "daily_pnl": self.data.get("daily_pnl", 0.0),
                "test_pnl": self.data.get("test_pnl", 0.0),
                "search_active": self.data.get("search_active", False),
                "mode": CONFIG.get("MODE", "REAL"),
                "pos": dict(self.data.get("pos", {})),
                "test_pos": dict(self.data.get("test_pos", {})),
                "trade_history": list(self.data.get("trade_history", [])),
                "market": market_data,
                "is_processing": self.is_processing,
                "ws_connected": self.ws_connected,
                "balance": self.data.get("balance", 0.0)
            }

    def _start_service_safe(self, dt):
        try:
            from titan_service import start_service
            start_service()
        except ImportError:
            logger.warning("⚠️ titan_service.py не найден")

    async def start(self):
        self._http = aiohttp.ClientSession()
        if HAS_ANDROID_WAKELOCK and wake_lock and not wake_lock.isHeld():
            wake_lock.acquire(10 * 60 * 60 * 1000)
            logger.info("🔒 WakeLock активирован")
        Clock.schedule_once(self._start_service_safe, 0)
        await self.restore_pending_orders()
        logger.info(f"TITAN запущен! Режим: {CONFIG.get('MODE')} | Device: {DEVICE_ID}")

    async def stop(self):
        if HAS_ANDROID_WAKELOCK and wake_lock and wake_lock.isHeld():
            wake_lock.release()
        try:
            from titan_service import stop_service
            stop_service()
        except ImportError:
            pass
        self.order_queue.close()
        if self._http and not self._http.closed:
            await self._http.close()
        logger.info("TITAN остановлен")

    def get_market_session_status(self) -> str:
        now_msk = dt.now(self.tz)
        hour, minute = now_msk.hour, now_msk.minute
        if hour >= 19 or hour < 10: return "НОЧЬ"
        if hour == 10 and minute < 5: return "КЛИРИНГ"
        if hour == 14 and minute < 5: return "КЛИРИНГ"
        if hour == 18 and minute >= 45: return "КЛИРИНГ"
        return "ТОРГИ"

    async def http_polling_loop(self):
        logger.info("🔄 Запущен HTTP polling fallback")
        while True:
            try:
                if self.ws_connected:
                    await asyncio.sleep(5)
                    continue
                if not CONFIG.get("ALOR_TOKEN"):
                    await asyncio.sleep(5)
                    continue
                if not await self._ensure_jwt():
                    await asyncio.sleep(5); continue
                
                headers = {"Authorization": f"Bearer {self.jwt}"}
                for ticker in BASE_ASSETS:
                    try:
                        url = f"https://api.alor.ru/md/v2/Securities/MOEX/{ticker}/orderbook?depth=10"
                        async with self._http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as r:
                            if r.status == 200:
                                data = await r.json()
                                bids, asks = data.get('bids', []), data.get('asks', [])
                                if bids and asks:
                                    price = (bids[0]['price'] + asks[0]['price']) / 2
                                    await self.process_tick(ticker, price, {"bids": bids, "asks": asks})
                    except Exception as e: 
                        logger.debug(f"HTTP poll error {ticker}: {e}")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"HTTP polling critical: {e}")
                await asyncio.sleep(5)

    async def restore_pending_orders(self):
        pending = await self.order_queue.get_pending_orders()
        if not pending:
            logger.info("📭 Очередь ордеров пуста")
            return
        logger.info(f"🔄 Восстановление {len(pending)} ордеров из очереди")
        for order in pending:
            if time.monotonic() - order['created_at'] > 300:
                logger.info(f"🗑️ Удалён устаревший ордер: {order['order_id']}")
                await self.order_queue.remove_order(order['order_id'])
                continue
            logger.info(f"📤 Повторная отправка ордера: {order['order_id']}")
            result = await self.send_order(order['ticker'], order['side'], order['qty'], order['price'], order['mkt'], order['order_id'])
            if result.get('status') in ['FILLED', 'ACKNOWLEDGED']:
                await self.order_queue.remove_order(order['order_id'])

    async def _ensure_jwt(self) -> bool:
        async with self._jwt_lock:
            if time.monotonic() < self.jwt_expiry and self.jwt: return True
            try:
                url = f"https://oauth.alor.ru/refresh?token={CONFIG['ALOR_TOKEN']}"
                async with self._http.post(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        body = await r.json()
                        self.jwt = body.get('AccessToken', '')
                        self.jwt_expiry = time.monotonic() + 1100
                        return True
                    else: logger.error(f"JWT refresh HTTP {r.status}")
            except Exception as e: logger.error(f"JWT refresh error: {e}")
            return False

    async def send_order(self, ticker: str, side: str, qty: int, price: float, mkt: str, order_id: str = None) -> dict:
        if self.is_processing:
            return {"status": "REJECTED", "error": "UI_BLOCKED"}
        self.is_processing = True
        try:
            if order_id is None:
                order_id = str(uuid.uuid4())
            if order_id in self.sent_order_ids:
                logger.warning(f"⚠️ Дубликат ордера: {order_id}")
                return {"status": "REJECTED", "error": "DUPLICATE"}
            
            await self.order_queue.add_order(order_id, ticker, side, qty, price, mkt)
            self.sent_order_ids[order_id] = time.monotonic()

            if not await self._ensure_jwt(): 
                return {"status": "REJECTED", "error": "NO_JWT"}
                
            slip = 0.02 if mkt == "FUT" else 0.0
            fp = price + slip if side.upper() == "BUY" else price - slip
            payload = {"side": side.lower(), "quantity": int(qty), "price": float(round(fp, 4)),
                       "instrument": {"symbol": ticker, "exchange": "MOEX"}, "portfolio": PORTFOLIOS[mkt], "type": "limit"}
            headers = {"Authorization": f"Bearer {self.jwt}", "X-ALOR-REQID": order_id}
            
            try:
                url = "https://api.alor.ru/commandapi/warp/v1/orders/limit"
                async with self._http.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        body = await r.json()
                        logger.info(f"ОРДЕР ACK: {side} {ticker} {qty} @ {fp}")
                        await self.order_queue.remove_order(order_id)
                        return {"status": "ACKNOWLEDGED", "order_id": body.get('orderNumber'), "price": fp}
                    else: 
                        logger.error(f"Order HTTP {r.status}")
                        return {"status": "REJECTED", "error": f"HTTP {r.status}"}
            except Exception as e: 
                logger.error(f"Order error: {e}")
                return {"status": "PENDING_RETRY", "error": str(e)}
        finally:
            self.is_processing = False

    def analyze_book(self, ticker: str, bids: list, asks: list, now: float) -> Optional[dict]:
        if not bids or not asks: return None
        current_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        self.base_tick_rate[ticker] = 0.9 * self.base_tick_rate[ticker] + 0.1 * current_rate
        tau_base = 3.0
        rate_ratio = current_rate / max(self.base_tick_rate[ticker], 0.1)
        lam = (1.0 / tau_base) * (0.5 + 0.5 * min(rate_ratio, 3.0))
        lam = min(0.8, max(0.3, lam)) # Сглаживание
        
        eff_bid_vol, eff_ask_vol = 0.0, 0.0
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
        return {"static_iq": static_iq, "bid_power": eff_bid_vol, "ask_power": eff_ask_vol,
                "bid_wall": bids[0]['price'] if eff_bid_vol > 0 else 0,
                "ask_wall": asks[0]['price'] if eff_ask_vol > 0 else 0}

    def is_logical_trade(self, ticker: str, snap: dict) -> bool:
        if snap["bid_power"] < 10 or snap["ask_power"] < 10: return False
        if snap["bid_power"] > snap["ask_power"] * 50: return False
        return True

    def check_volatility(self, ticker: str, price: float) -> bool:
        hist = self.range_history[ticker]
        hist.append(price)
        if len(hist) > 600: hist.pop(0)
        if len(hist) < 300: return True
        sw = hist[-60:]
        sr = max(sw) - min(sw)
        lr = (max(hist) - min(hist)) / 10.0
        return sr >= (lr * VOL_BREATH_THRESHOLD)

    async def safe_save(self):
        loop = asyncio.get_running_loop()
        def _save():
            try:
                with open(STATE_TMP_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=4, ensure_ascii=False)
                os.replace(STATE_TMP_FILE, STATE_FILE)
            except Exception as e: logger.error(f"Save error: {e}")
        await loop.run_in_executor(None, _save)

    async def exit_trade(self, ticker: str, price: float, reason: str, prof: float):
        async with self._data_lock:
            plist = self.data["pos"]
            ll = self.data["limits"]
            p = plist.get(ticker)
            if not p: return
        
        await self.send_order(ticker, "SELL", p["lot"], price, p["mkt"])
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
            if len(self.data["trade_history"]) > 50: self.data["trade_history"] = self.data["trade_history"][-50:]
            del plist[ticker]
        await self.safe_save()
        logger.info(f"ВЫХОД {ticker} ({reason}) | PnL: {net}р")

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

    async def process_tick(self, ticker: str, price: float, book: dict):
        now = time.monotonic()
        self.tick_buffers[ticker].push(price, now)
        self.price_history[ticker].append(price)
        if len(self.price_history[ticker]) > 600:
            self.price_history[ticker].pop(0)
        
        session = self.get_market_session_status()
        if session in ["НОЧЬ", "КЛИРИНГ"]:
            return
        
        bids, asks = book.get('bids', []), book.get('asks', [])
        snap = self.analyze_book(ticker, bids, asks, now)
        
        if bids and asks and asks[0]['price'] and bids[0]['price']:
            current_spread = (asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 0.001)
            prev_p = self.last_tick_price[ticker]
            price_change = abs(price - prev_p) if prev_p > 0 else 0.0
            self.last_tick_price[ticker] = price
            self._update_ema_stats(self.spread_stats[ticker], current_spread, self.SPREAD_ALPHA)
            self._update_ema_stats(self.vol_stats[ticker], price_change, self.VOL_ALPHA)
            if self.warmup_ticks[ticker] < self.WARMUP_LIMIT:
                self.warmup_ticks[ticker] += 1
                return
            if current_spread > self.get_adaptive_spread_threshold(ticker): return
        else: return

        if not snap or not self.is_logical_trade(ticker, snap): return
        is_fut = ticker in ("GOLD", "Si")
        is_fx = ticker == "CNY"
        mkt = "FUT" if is_fut else ("FX" if is_fx else "STK")
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])

        tick_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        tick_factor = math.tanh(tick_rate / 15.0)
        static_iq = snap["static_iq"]
        
        target_time = now - 1.5
        prev_price = price
        buf = self.tick_buffers[ticker]
        for i in range(buf.head - 1, max(-1, buf.head - buf.size - 1), -1):
            idx = i & buf.mask
            if buf.times[idx] <= target_time:
                prev_price = buf.prices[idx]; break
        price_delta = price - prev_price
        book_delta = snap["bid_power"] - snap["ask_power"]
        delta_ratio = abs(price_delta) / current_spread if current_spread > 0 else 0
        
        if (price_delta > 0 and book_delta > 0) or (price_delta < 0 and book_delta < 0):
            c_truth = min(1.2, 1.0 + 0.2 * delta_ratio)
        elif price_delta != 0: c_truth = max(0.7, 1.0 - 0.3 * delta_ratio)
        else: c_truth = 1.0
        
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

        need_exit, exit_price, exit_reason, exit_prof = False, 0.0, "", 0.0
        
        async with self._data_lock:
            active_pos = self.data["pos"]
            active_limits = self.data["limits"]
            
            if ticker in active_pos:
                p = active_pos[ticker]
                prof = ((price - p["p"]) / p["p"]) if p["side"] == "BUY" else ((p["p"] - price) / p["p"])
                hold_time = (dt.now(self.tz) - dt.fromtimestamp(p.get("entry_time", time.time()), self.tz)).seconds
                exit_condition = False
                if p["side"] == "BUY" and snap["ask_wall"] > 0 and price < snap["ask_wall"] and final_iq < 0.8:
                    exit_reason, exit_prof, exit_condition = "WALL-REJECTION", prof, True
                cr = spec.get("comm_buffer", 0.0005)
                if not exit_condition and final_iq < 1.0 and prof < -(cr * 1.5):
                    exit_reason, exit_prof, exit_condition = "REVERSAL-EXIT", prof, True
                if not exit_condition:
                    hold_mult = 0.50 if hold_time < 15 else (0.80 if prof < 0.0015 else 0.60)
                    if final_iq <= p.get("entry_iq_real", 3.0) * hold_mult:
                        exit_reason, exit_prof, exit_condition = "IQ-DYNAMIC-EXIT", prof, True
                if exit_condition: need_exit, exit_price = True, price
                if not need_exit and prof > 0.003:
                    tr = p["p"] + (p["p"] * DIANA_TIGHT_TRAIL) if p["side"] == "BUY" else p["p"] - (p["p"] * DIANA_TIGHT_TRAIL)
                    if (price > tr and p["side"] == "BUY") or (price < tr and p["side"] == "SELL"): p["p"] = tr
            else:
                if not self.data.get("search_active", False): return
                if active_limits.get(mkt, 0) <= 0: return
                if not self.check_volatility(ticker, price): return
                if len(active_pos) >= MAX_OPEN_POSITIONS: return
                
                base_thr = IQ_FUTURES_THRESHOLD if ticker in ("GOLD", "Si") else IQ_STOCKS_THRESHOLD
                iq_thr = self.get_adaptive_iq_threshold(ticker, base_thr)
                if final_iq < iq_thr: return
                
                side = "BUY" if snap["bid_power"] > snap["ask_power"] else "SELL"
                risk = MARGIN_FACTOR * active_limits.get(mkt, 10000)
                lot = min(max(1, int(risk / (price * 0.01))), MAX_POSITION_LOTS)
                best_level_vol = bids[0]['volume'] if side == "BUY" else asks[0]['volume']
                slippage_ratio = lot / max(best_level_vol, 1.0)
                expected_slippage = (current_spread / 2.0) + 0.1 * (slippage_ratio ** 2)
                iq_excess = final_iq - iq_thr
                potential_profit_ratio = max(0.0, iq_excess * 0.0005)
                if expected_slippage > potential_profit_ratio * 0.5:
                    return
                
                frozen = (risk * 0.3) + (spec.get("comm_fixed", 2.0) * lot)
                active_limits[mkt] = round(active_limits.get(mkt, 0.0) - frozen, 2)
                pos = {"ticker": ticker, "side": side, "lot": lot, "p": price, "mkt": mkt,
                       "entry_time": time.time(), "frozen_margin": frozen,
                       "comm_paid": 0.0, "entry_iq_real": final_iq, "peak_iq": final_iq, "max_prof": 0.0}
                active_pos[ticker] = pos
                logger.info(f"ОТКРЫТА: {ticker} ({side}) {lot} лотов @ {price} | IQ: {final_iq:.2f}")
        
        if need_exit: await self.exit_trade(ticker, exit_price, exit_reason, exit_prof)
        await self.safe_save()

# ============================================================================
# 8. WEBSOCKET
# ============================================================================
ALOR_WS_URL = "wss://api.alor.ru/ws"

async def _get_access_token(http_session: aiohttp.ClientSession) -> Optional[str]:
    try:
        url = f"https://oauth.alor.ru/refresh?token={CONFIG['ALOR_TOKEN']}"
        async with http_session.post(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                body = await r.json()
                token = body.get("AccessToken", "")
                if token: return token
    except Exception as e: logger.error(f"OAuth error: {e}")
    return None

async def ws_market_data_feed(bot: TitanAbsoluteMonolith):
    retry_count = 0
    max_retries = 5
    while True:
        access_token = None
        try:
            async with aiohttp.ClientSession() as session:
                access_token = await _get_access_token(session)
            if not access_token:
                await asyncio.sleep(10); continue
            
            ssl_context = ssl.create_default_context()
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientWSTimeout(ws_close=10.0, ws_receive=30.0)
                async with session.ws_connect(ALOR_WS_URL, heartbeat=30, ssl=ssl_context, timeout=timeout) as ws:
                    logger.info("✅ WS-соединение установлено")
                    bot.ws_connected = True
                    retry_count = 0
                    for ticker in BASE_ASSETS:
                        sub_msg = {"opcode": "OrderBookGetAndSubscribe", "code": ticker, "depth": 10,
                                   "exchange": "MOEX", "format": "Simple", "frequency": 0,
                                   "guid": str(uuid.uuid4()), "token": access_token}
                        await ws.send_json(sub_msg)
                    async for raw_msg in ws:
                        if raw_msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(raw_msg.data)
                                if data.get("opcode") == "OrderBook":
                                    ticker = data.get("code")
                                    if ticker:
                                        bids, asks = data.get("bids", []), data.get("asks", [])
                                        if bids and asks:
                                            price = (bids[0]['price'] + asks[0]['price']) / 2
                                            await bot.process_tick(ticker, price, {"bids": bids, "asks": asks})
                            except Exception as e: logger.debug(f"WS parse error: {e}")
                        elif raw_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR): break
        except Exception as e: 
            logger.error(f"WS critical error: {e}")
            bot.ws_connected = False
            delay = min(2 ** retry_count, 60) + random.uniform(0, 1)
            await asyncio.sleep(delay)
            retry_count += 1

# ============================================================================
# 9. UI: ЭКРАН АКТИВАЦИИ
# ============================================================================
class ActivationScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=20, spacing=15)
        layout.add_widget(Label(text="TITAN Pro — Активация", font_size=dp(24), bold=True, size_hint_y=None, height=dp(50)))
        layout.add_widget(Label(text="Отправьте этот Device ID разработчику для получения ключа:", font_size=dp(14), size_hint_y=None, height=dp(40), color=(0.8, 0.8, 0.8, 1)))
        
        device_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50))
        device_box.add_widget(Label(text="Device ID:", font_size=dp(16)))
        device_box.add_widget(Label(text=DEVICE_ID, font_size=dp(16), color=(0.2, 0.8, 1, 1), bold=True))
        layout.add_widget(device_box)
        
        self.key_btn = Button(text="Нажмите, чтобы ввести лицензионный ключ", font_size=dp(14), background_color=(0.2, 0.2, 0.3, 1), size_hint_y=None, height=dp(50))
        self.key_btn.bind(on_press=self._open_key_input)
        layout.add_widget(self.key_btn)
        
        self.activate_btn = Button(text="АКТИВИРОВАТЬ", font_size=dp(18), bold=True, background_color=(0.2, 0.8, 0.2, 1), size_hint_y=None, height=dp(60))
        self.activate_btn.bind(on_press=self.activate)
        layout.add_widget(self.activate_btn)
        
        self.status_lbl = Label(text="", font_size=dp(14), size_hint_y=None, height=dp(30))
        layout.add_widget(self.status_lbl)
        self.add_widget(layout)
        self.input_key = ""

    def _open_key_input(self, instance):
        content = BoxLayout(orientation='vertical', spacing=10)
        self.text_input = TextInput(hint_text='Введите ключ', font_size=dp(16), multiline=False)
        content.add_widget(self.text_input)
        btn = Button(text='OK', size_hint_y=None, height=dp(50))
        content.add_widget(btn)
        popup = Popup(title='Лицензионный ключ', content=content, size_hint=(0.9, 0.3))
        btn.bind(on_press=lambda x: (setattr(self, 'input_key', self.text_input.text), setattr(self.key_btn, 'text', self.text_input.text), popup.dismiss()))
        popup.open()

    def activate(self, instance):
        key_text = self.input_key.strip()
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
            self.status_lbl.text = "✅ Успешно! Переход в приложение..."
            self.status_lbl.color = (0.2, 0.9, 0.2, 1)
            Clock.schedule_once(lambda dt: self._go_to_main(), 1.0)
        else:
            self.status_lbl.text = "❌ Неверный ключ для этого устройства!"
            self.status_lbl.color = (0.9, 0.2, 0.2, 1)

    def _go_to_main(self):
        self.manager.current = 'dashboard'
        self.manager.app.start_bot(0)

# ============================================================================
# 10. UI: НАСТРОЙКИ
# ============================================================================
class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=15, spacing=10)
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

        save_btn = Button(text="СОХРАНИТЬ И ПРИМЕНИТЬ", font_size=dp(16), bold=True, background_color=(0.2, 0.8, 0.2, 1), size_hint_y=None, height=dp(50))
        save_btn.bind(on_press=self.save_credentials)
        layout.add_widget(save_btn)
        self.status_lbl = Label(text="", font_size=dp(14), size_hint_y=None, height=dp(30))
        layout.add_widget(self.status_lbl)
        
        nav = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=5)
        for name, scr in [("Главная", "dashboard"), ("Настройки", "settings")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=scr: setattr(self.manager, 'current', s))
            nav.add_widget(btn)
        layout.add_widget(nav)
        self.add_widget(layout)

    def save_credentials(self, instance):
        token, fut, stk, fx = self.in_token.text.strip(), self.in_fut.text.strip(), self.in_stk.text.strip(), self.in_fx.text.strip()
        if not token:
            self.status_lbl.text, self.status_lbl.color = "❌ Token обязателен!", (0.9, 0.2, 0.2, 1)
            return
        save_user_credentials(token, fut, stk, fx)
        CONFIG["ALOR_TOKEN"] = token
        PORTFOLIOS["FUT"], PORTFOLIOS["STK"], PORTFOLIOS["FX"] = fut, stk, fx
        self.status_lbl.text, self.status_lbl.color = "✅ Данные сохранены и зашифрованы! (Перезапустите приложение)", (0.2, 0.9, 0.2, 1)

# ============================================================================
# 11. UI: DASHBOARD & HISTORY (Упрощенные для клиента)
# ============================================================================
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
        btn_layout.add_widget(self.start_btn); btn_layout.add_widget(self.stop_btn)
        layout.add_widget(btn_layout)

        self.pulse_layout = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(120), spacing=2)
        layout.add_widget(self.pulse_layout)

        nav_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=5)
        for name, screen in [("Главная", "dashboard"), ("Настройки", "settings")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=screen: setattr(self.manager, 'current', s))
            nav_layout.add_widget(btn)
        layout.add_widget(nav_layout)
        self.add_widget(layout)

    def start_trading(self, instance):
        if self.bot and not self.bot.is_processing:
            async def do_start():
                self.bot.data["search_active"] = True
                self.status_label.text = "Статус: АКТИВЕН"
                self.status_label.color = (0.2, 0.9, 0.2, 1)
            self.bot.run_async_threadsafe(do_start())

    def stop_trading(self, instance):
        if self.bot and not self.bot.is_processing:
            async def do_stop():
                self.bot.data["search_active"] = False
                self.status_label.text = "Статус: ПАУЗА"
                self.status_label.color = (1, 1, 1, 1)
            self.bot.run_async_threadsafe(do_stop())

    def update_data(self, data_snapshot):
        self.status_label.color = (0.2, 0.9, 0.2, 1) if data_snapshot.get("search_active") else (1, 1, 1, 1)
        self.pulse_layout.clear_widgets()
        market = data_snapshot.get("market", {})
        for ticker in BASE_ASSETS:
            info = market.get(ticker, {"iq": 0, "price": 0})
            iq, price = info["iq"], info["price"]
            cl = (0.2, 0.9, 0.2, 1) if iq >= 7.0 else (0.9, 0.9, 0.2, 1) if iq >= 3.0 else (0.5, 0.5, 0.5, 1)
            self.pulse_layout.add_widget(Label(text=f"{ticker}: IQ:{iq:.1f} | {price:.1f}р", font_size=dp(13), size_hint_y=None, height=dp(22), color=cl))

class HistoryScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=10, spacing=8)
        layout.add_widget(Label(text="История сделок", font_size=dp(24), bold=True, size_hint_y=None, height=dp(50)))
        self.stats_label = Label(text="Загрузка...", font_size=dp(15), size_hint_y=None, height=dp(60))
        layout.add_widget(self.stats_label)
        scroll = ScrollView(size_hint=(1, 1))
        self.history_layout = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(400), spacing=5)
        scroll.add_widget(self.history_layout)
        layout.add_widget(scroll)
        nav = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=5)
        for name, scr in [("Главная", "dashboard"), ("Настройки", "settings")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=scr: setattr(self.manager, 'current', s))
            nav.add_widget(btn)
        layout.add_widget(nav)
        self.add_widget(layout)

    def update_data(self, data_snapshot):
        trades = data_snapshot.get("trade_history", [])
        if not trades:
            self.stats_label.text = "Нет сделок"
            self.history_layout.clear_widgets()
            return
        total = len(trades)
        wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
        winrate = (wins / total * 100) if total > 0 else 0
        self.stats_label.text = f"Сделок: {total} | Винрейт: {winrate:.1f}%"
        self.history_layout.clear_widgets()
        for trade in reversed(trades[-20:]):
            color = (0.2, 0.9, 0.2, 1) if trade.get('pnl', 0) > 0 else (0.9, 0.2, 0.2, 1)
            self.history_layout.add_widget(Label(
                text=f"[{trade['mode']}] {trade['ticker']} ({trade['side']}) | PnL: {trade['pnl']:.2f}р | {trade['time']}",
                size_hint_y=None, height=dp(40), color=color
            ))

# ============================================================================
# 12. APP
# ============================================================================
class TITANProApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = "TITAN Pro"
        self.bot = TitanAbsoluteMonolith()
        self._loop_thread = None

    def build(self):
        sm = ScreenManager()
        if not IS_LICENSED:
            act = ActivationScreen(name='activation')
            act.manager = sm
            sm.add_widget(act)
            sm.current = 'activation'
            return sm
        
        dash = DashboardScreen(name='dashboard')
        dash.bot = self.bot
        sm.add_widget(dash)
        sm.add_widget(HistoryScreen(name='history'))
        sm.add_widget(SettingsScreen(name='settings'))
        sm.current = 'dashboard'
        Clock.schedule_once(self.start_bot, 0.5)
        Clock.schedule_interval(self.update_ui, 1.0)
        return sm

    async def async_main(self):
        self.bot._loop = asyncio.get_running_loop()
        await self.bot.start()
        await asyncio.gather(
            ws_market_data_feed(self.bot),
            self.bot.http_polling_loop(),
            return_exceptions=True
        )

    def start_bot(self, dt):
        def run_loop():
            asyncio.run(self.async_main())
        self._loop_thread = threading.Thread(target=run_loop, daemon=True)
        self._loop_thread.start()

    def update_ui(self, dt):
        if self.bot._loop and self.bot._loop.is_running():
            asyncio.create_task(self._async_update_ui())

    async def _async_update_ui(self):
        data_snapshot = await self.bot.get_safe_data()
        Clock.schedule_once(lambda dt: self._apply_ui_update(data_snapshot), 0)

    def _apply_ui_update(self, data_snapshot):
        sm = self.root
        if sm and sm.current != 'activation':
            sm.get_screen('dashboard').update_data(data_snapshot)
            sm.get_screen('history').update_data(data_snapshot)

    def on_stop(self):
        if self.bot:
            self.bot.run_async_threadsafe(self.bot.stop())

if __name__ == '__main__':
    TITANProApp().run()
