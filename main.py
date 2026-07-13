ker} — статimport sys
import os
import time
import json
import asyncio
import uuid
import logging
import ssl
import math
import threading
from datetime import datetime as dt
from typing import Dict, Optional

import aiohttp
import pytz

# Kivy imports
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.metrics import dp

# ============================================================================
# LOCK-FRIENDLY RING BUFFER (GIL-friendly, без мьютексов)
# ============================================================================
class TickRingBuffer:
    """Кольцевой буфер для хранения тиков. O(1) запись, быстрое чтение."""
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
# КОНФИГУРАЦИЯ И СИСТЕМА ПРОФИЛЕЙ
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TITAN")

CONFIG = {
    "ALOR_TOKEN": os.getenv("ALOR_TOKEN", "c4a6afed-0dae-4057-8289-d516c7b09feb"),
    "SALT": os.getenv("TITAN_SALT", "NEVINNOMYSSK_TITAN_2026"),
    "MODE": os.getenv("TITAN_MODE", "TEST"),
}

if 'ANDROID_PRIVATE' in os.environ:
    DATA_DIR = os.environ['ANDROID_PRIVATE']
else:
    DATA_DIR = '.'
STATE_FILE = os.path.join(DATA_DIR, "titan_monolith.json")
STATE_TMP_FILE = os.path.join(DATA_DIR, "titan_monolith.tmp.json")

import hashlib
import random

class ClientProfile:
    def __init__(self, profile_id: str = "DEFAULT"):
        self.profile_id = profile_id
        self.seed = int(hashlib.md5(profile_id.encode()).hexdigest()[:8], 16)
        self.iq_mult = 0.85 + (self.seed % 30) / 100.0
        self.trail_mult = 0.80 + ((self.seed >> 4) % 40) / 100.0
        self.size_mult = 0.70 + ((self.seed >> 8) % 60) / 100.0
        all_assets = ["SBER", "GAZP", "Si", "CNY", "GOLD", "VTBR", "MGNT", "LKOH"]
        self.assets = self._select_assets(all_assets)
        logger.info(f"Профиль загружен: {profile_id} | IQx{self.iq_mult:.2f} | Активы: {self.assets}")

    def _select_assets(self, assets):
        rng = random.Random(self.seed)
        return rng.sample(assets, 5)

PROFILE = ClientProfile("CLIENT_DEFAULT")

DAILY_LIMIT_PCT = 3.5
MARGIN_FACTOR = 0.15
WALL_MULTIPLIER = 5.5
MAX_SPREAD_LIMIT = 0.0006  # Резервная константа, теперь используется адаптивный порог
IQ_STOCKS_THRESHOLD = 7.0 * PROFILE.iq_mult
IQ_FUTURES_THRESHOLD = 3.0
VOL_BREATH_THRESHOLD = 0.4
DIANA_TIGHT_TRAIL = 0.0015 * PROFILE.trail_mult
MAX_POSITION_LOTS = int(1000 * PROFILE.size_mult)

PORTFOLIOS = {"FUT": "7502Y5H", "STK": "D101327", "FX": "G68390"}
BASE_ASSETS = PROFILE.assets
moscow_tz = pytz.timezone('Europe/Moscow')

Window.clearcolor = (0.1, 0.1, 0.15, 1)

def verify_auth(config: dict) -> bool:
    return config.get('SALT', '') == "NEVINNOMYSSK_TITAN_2026"

if not verify_auth(CONFIG):
    logger.critical("ОШИБКА АВТОРИЗАЦИИ.")
    sys.exit(1)

# ============================================================================
# STATE MANAGEMENT
# ============================================================================
def _default_state() -> dict:
    def_army = {t: {"pnl_today": 0.0, "state": "SHADOW", "nominal_iq": 4.5} for t in BASE_ASSETS}
    return {
        "total_pnl": 0.0, "test_pnl": 0.0, "daily_pnl": 0.0, "search_active": True,
        "limits": {"FUT": 50000.0, "STK": 10000.0, "FX": 5000.0},
        "test_limits": {"FUT": 100000.0, "STK": 100000.0, "FX": 100000.0},
        "army": def_army,
        "day_open_prices": {t: 0.0 for t in BASE_ASSETS},
        "pos": {}, "test_pos": {},
        "last_reset_date": dt.now(moscow_tz).strftime("%Y-%m-%d"),
        "auto_iq_mode": True,
        "trade_history": []
    }

def load_state() -> dict:
    base = _default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            for k, v in base.items():
                if k not in d:
                    d[k] = v
            return d
        except Exception as e:
            logger.error(f"State load error: {e}")
    return base

def save_state_atomic(data: dict):
    try:
        with open(STATE_TMP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(STATE_TMP_FILE, STATE_FILE)
    except Exception as e:
        logger.error(f"Save error: {e}")

# ============================================================================
# TITAN MONOLITH CORE — ВСЯ МАТЕМАТИКА ЗДЕСЬ
# ============================================================================
class TitanAbsoluteMonolith:
    ASSET_PARAMS = {
        "SBER": {"type": "STK", "comm_buffer": 0.0003, "risk_mult": 1.0},
        "GAZP": {"type": "STK", "comm_buffer": 0.0003, "risk_mult": 1.0},
        "GOLD": {"type": "FUT", "comm_fixed": 5.0, "min_step": 0.1, "step_val": 0.85, "risk_mult": 1.0},
        "Si": {"type": "FUT", "comm_fixed": 3.0, "min_step": 1.0, "step_val": 1.0, "risk_mult": 1.0},
        "CNY": {"type": "FX", "comm_buffer": 0.0006, "risk_mult": 1.0},
        "DEFAULT": {"type": "STK", "comm_buffer": 0.0005, "risk_mult": 1.0}
    }

    def __init__(self):
        self.mode = CONFIG.get("MODE", "TEST")
        self.tz = moscow_tz
        self.data = load_state()
        self._data_lock = threading.RLock()

        # === ЭТАП 1: Ring Buffer + Time-Decayed Order Book ===
        self.tick_buffers: Dict[str, TickRingBuffer] = {t: TickRingBuffer(1024) for t in BASE_ASSETS}
        self.level_eff_volumes = {t: {'bid': [0.0]*5, 'ask': [0.0]*5} for t in BASE_ASSETS}
        self.level_last_update = {t: {'bid': [0.0]*5, 'ask': [0.0]*5} for t in BASE_ASSETS}
        self.base_tick_rate = {t: 1.0 for t in BASE_ASSETS}

        # === СПРИНТ 1: Адаптивные пороги + Warmup ===
        self.warmup_ticks = {t: 0 for t in BASE_ASSETS}
        self.WARMUP_LIMIT = 100
        self.spread_stats = {t: {'mean': 0.0, 'var': 0.0} for t in BASE_ASSETS}
        self.SPREAD_ALPHA = 0.05
        self.SPREAD_K = 1.5
        self.SPREAD_MIN = 0.0002
        self.vol_stats = {t: {'mean': 0.0, 'var': 0.0} for t in BASE_ASSETS}
        self.VOL_ALPHA = 0.05
        self.VOL_SENSITIVITY = 0.5
        self.last_tick_price = {t: 0.0 for t in BASE_ASSETS}

        # === СПРИНТ 3: Двойное сглаживание IQ (Trend Filter) ===
        self.iq_slow: Dict[str, float] = {t: 5.0 for t in BASE_ASSETS}
        self.IQ_SLOW_GAMMA = 0.05

        # Остальные структуры
        self.price_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.iq_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.range_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.walls: Dict[str, dict] = {
            t: {"bid_wall": 0, "ask_wall": 0, "bid_power": 0, "ask_power": 0}
            for t in BASE_ASSETS
        }

        self.jwt: str = ""
        self.jwt_expiry: float = 0
        self._jwt_lock = asyncio.Lock()
        self._http: Optional[aiohttp.ClientSession] = None
        self._loop = None

    def run_async_threadsafe(self, coro):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            logger.error("Async loop not running")

    def get_safe_data(self):
        with self._data_lock:
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
                "search_active": self.data.get("search_active", True),
                "mode": self.mode,
                "pos": dict(self.data.get("pos", {})),
                "test_pos": dict(self.data.get("test_pos", {})),
                "trade_history": list(self.data.get("trade_history", [])),
                "market": market_data
            }

    async def start(self):
        self._http = aiohttp.ClientSession()
        logger.info(f"TITAN запущен! Режим: {self.mode}")

    async def stop(self):
        if self._http and not self._http.closed:
            await self._http.close()
        logger.info("TITAN остановлен")

    async def _ensure_jwt(self) -> bool:
        async with self._jwt_lock:
            if time.time() < self.jwt_expiry and self.jwt:
                return True
            try:
                url = f"https://oauth.alor.ru/refresh?token={CONFIG['ALOR_TOKEN']}"
                async with self._http.post(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        body = await r.json()
                        self.jwt = body.get('AccessToken', '')
                        self.jwt_expiry = time.time() + 1100
                        return True
                    else:
                        logger.error(f"JWT refresh HTTP {r.status}")
            except Exception as e:
                logger.error(f"JWT refresh error: {e}")
            return False

    async def send_order(self, ticker: str, side: str, qty: int, price: float, mkt: str) -> Optional[str]:
        if self.mode == "TEST":
            logger.info(f"[TEST] {side} {ticker} {qty} @ {price}")
            return str(uuid.uuid4())

        if not await self._ensure_jwt():
            logger.error("Cannot send order: JWT unavailable")
            return None

        slip = 0.02 if mkt == "FUT" else 0.0
        fp = price + slip if side.upper() == "BUY" else price - slip
        payload = {
            "side": side.lower(),
            "quantity": int(qty),
            "price": float(round(fp, 4)),
            "instrument": {"symbol": ticker, "exchange": "MOEX"},
            "portfolio": PORTFOLIOS[mkt],
            "type": "limit"
        }
        headers = {
            "Authorization": f"Bearer {self.jwt}",
            "X-ALOR-REQID": str(uuid.uuid4())
        }
        try:
            url = "https://api.alor.ru/commandapi/warp/v1/orders/limit"
            async with self._http.post(url, json=payload, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    body = await r.json()
                    logger.info(f"ОРДЕР: {side} {ticker} {qty} @ {fp}")
                    return body.get('orderNumber')
                else:
                    text = await r.text()
                    logger.error(f"Order HTTP {r.status}: {text}")
        except Exception as e:
            logger.error(f"Order error: {e}")
        return None

    @staticmethod
    def _sort_book(bids: list, asks: list):
        bids_sorted = sorted(bids, key=lambda x: x['price'], reverse=True)
        asks_sorted = sorted(asks, key=lambda x: x['price'])
        return bids_sorted, asks_sorted

    # === ЭТАП 1: Time-Decayed Order Book ===
    def analyze_book(self, ticker: str, bids: list, asks: list, now: float) -> Optional[dict]:
        if not bids or not asks:
            return None

        # Адаптивный коэффициент затухания
        current_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        self.base_tick_rate[ticker] = 0.9 * self.base_tick_rate[ticker] + 0.1 * current_rate

        tau_base = 3.0
        rate_ratio = current_rate / max(self.base_tick_rate[ticker], 0.1)
        lam = (1.0 / tau_base) * (0.5 + 0.5 * min(rate_ratio, 3.0))

        eff_bid_vol = 0.0
        eff_ask_vol = 0.0

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
        if total_eff > 0:
            static_iq = ((eff_bid_vol - eff_ask_vol) / total_eff + 1) * 5
        else:
            static_iq = 5.0

        return {
            "static_iq": static_iq,
            "bid_power": eff_bid_vol,
            "ask_power": eff_ask_vol,
            "bid_wall": bids[0]['price'] if eff_bid_vol > 0 else 0,
            "ask_wall": asks[0]['price'] if eff_ask_vol > 0 else 0
        }

    def is_logical_trade(self, ticker: str, snap: dict) -> bool:
        if snap["bid_power"] < 10 or snap["ask_power"] < 10:
            return False
        if snap["bid_power"] > snap["ask_power"] * 50:
            return False
        if self.data["army"][ticker]["pnl_today"] < -DAILY_LIMIT_PCT * 100:
            return False
        return True

    def check_volatility(self, ticker: str, price: float) -> bool:
        hist = self.range_history[ticker]
        hist.append(price)
        if len(hist) > 600:
            hist.pop(0)
        if len(hist) < 300:
            return True
        sw = hist[-60:]
        sr = max(sw) - min(sw)
        lr = (max(hist) - min(hist)) / 10.0
        return sr >= (lr * VOL_BREATH_THRESHOLD)

    async def safe_save(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, save_state_atomic, self.data)

    async def exit_trade(self, ticker: str, price: float, reason: str, prof: float):
        with self._data_lock:
            plist = self.data["pos"] if self.mode == "REAL" else self.data["test_pos"]
            ll = self.data["limits"] if self.mode == "REAL" else self.data["test_limits"]
            p = plist.get(ticker)
            if not p:
                return

        await self.send_order(ticker, "SELL", p["lot"], price, p["mkt"])
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])

        if spec["type"] == "FUT":
            pts = (price - p["p"]) if p["side"] == "BUY" else (p["p"] - price)
            net = round(
                (pts / spec.get("min_step", 1.0)) * spec.get("step_val", 1.0) * p["lot"]
                - spec.get("comm_fixed", 2.0) * p["lot"], 2
            )
        else:
            pos_val = p["lot"] * p["p"]
            net = round(pos_val * prof - pos_val * spec.get("comm_buffer", 0.0005), 2)

        with self._data_lock:
            ll[p["mkt"]] = round(ll.get(p["mkt"], 0.0) + p.get("frozen_margin", 0.0) + net, 2)
            self.data["army"][ticker]["pnl_today"] += net

            if self.mode == "REAL":
                self.data["total_pnl"] += net
                self.data["daily_pnl"] += net
            else:
                self.data.setdefault("test_pnl", 0.0)
                self.data["test_pnl"] += net

            trade_record = {
                "ticker": ticker, "side": p["side"], "entry_price": p["p"],
                "exit_price": price, "lot": p["lot"], "pnl": net,
                "reason": reason, "time": dt.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")
            }
            self.data["trade_history"].append(trade_record)
            if len(self.data["trade_history"]) > 50:
                self.data["trade_history"] = self.data["trade_history"][-50:]

            del plist[ticker]

        await self.safe_save()
        logger.info(f"ВЫХОД {ticker} ({reason}) | PnL: {net}р")

    # === ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ СПРИНТА 1 ===
    def _update_ema_stats(self, stats_dict, value, alpha):
        diff = value - stats_dict['mean']
        stats_dict['mean'] += alpha * diff
        stats_dict['var'] = (1.0 - alpha) * (stats_dict['var'] + alpha * diff * diff)

    def get_adaptive_spread_threshold(self, ticker: str) -> float:
        stats = self.spread_stats[ticker]
        sigma = math.sqrt(stats['var']) if stats['var'] > 0 else 0.0
        dynamic_threshold = stats['mean'] + self.SPREAD_K * sigma
        return max(self.SPREAD_MIN, dynamic_threshold)

    def get_adaptive_iq_threshold(self, ticker: str, base_threshold: float) -> float:
        stats = self.vol_stats[ticker]
        if stats['mean'] > 0:
            vol_deviation = (math.sqrt(stats['var']) - stats['mean']) / stats['mean']
        else:
            vol_deviation = 0.0
        return base_threshold * (1.0 + self.VOL_SENSITIVITY * max(0, vol_deviation))

    # === ГЛАВНЫЙ МЕТОД — ВСЯ МАТЕМАТИКА ЗДЕСЬ ===
    async def process_tick(self, ticker: str, price: float, book: dict):
        if self.mode == "TEST" and not book.get('bids'):
            book = {
                'bids': [{'price': price * 0.9999, 'volume': 1000}],
                'asks': [{'price': price * 1.0001, 'volume': 1000}]
            }

        now = time.time()
        self.tick_buffers[ticker].push(price, now)

        bids = book.get('bids', [])
        asks = book.get('asks', [])
        snap = self.analyze_book(ticker, bids, asks, now)

        # === СПРИНТ 1: Статистика, Warmup, Адаптивные пороги ===
        if bids and asks and asks[0]['price'] and bids[0]['price']:
            current_spread = (asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 0.001)

            # Безопасный расчёт дельты цены
            prev_p = self.last_tick_price[ticker]
            price_change = abs(price - prev_p) if prev_p > 0 else 0.0
            self.last_tick_price[ticker] = price

            self._update_ema_stats(self.spread_stats[ticker], current_spread, self.SPREAD_ALPHA)
            self._update_ema_stats(self.vol_stats[ticker], price_change, self.VOL_ALPHA)

            if self.warmup_ticks[ticker] < self.WARMUP_LIMIT:
                self.warmup_ticks[ticker] += 1
                if self.warmup_ticks[ticker] == self.WARMUP_LIMIT:
                    logger.info(f"WARMUP PASSED для {ticker} — статистика собрана")
                return

            if current_spread > self.get_adaptive_spread_threshold(ticker):
                return
        else:
            return

        if not snap or not self.is_logical_trade(ticker, snap):
            return

        is_fut = ticker in ("GOLD", "Si")
        is_fx = ticker == "CNY"
        mkt = "FUT" if is_fut else ("FX" if is_fx else "STK")
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])

        # === ДИНАМИЧЕСКИЙ IQ + СПРИНТ 3: TREND FILTER ===
        tick_rate = self.tick_buffers[ticker].count_recent(5.0, now) / 5.0
        tick_factor = math.tanh(tick_rate / 15.0)

        static_iq = snap["static_iq"]

        # Truth Factor
        current_spread = (asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 0.001)
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

        if (price_delta > 0 and book_delta > 0) or (price_delta < 0 and book_delta < 0):
            c_truth = min(1.2, 1.0 + 0.2 * delta_ratio)
        elif price_delta != 0:
            c_truth = max(0.7, 1.0 - 0.3 * delta_ratio)
        else:
            c_truth = 1.0

        # Сырой IQ
        raw_iq = static_iq * (0.3 + 0.7 * tick_factor) * c_truth

        # === СПРИНТ 3: C_trend (Trend Filter) ===
        fast_iq_prev = self.iq_history[ticker][-1] if self.iq_history[ticker] else raw_iq
        # Быстрая EMA (адаптивная)
        gamma_fast = 0.2 + 0.6 * tick_factor
        cur_iq = gamma_fast * raw_iq + (1 - gamma_fast) * fast_iq_prev

        # Медленная EMA (тренд)
        iq_slow_prev = self.iq_slow[ticker]
        self.iq_slow[ticker] = (1.0 - self.IQ_SLOW_GAMMA) * iq_slow_prev + self.IQ_SLOW_GAMMA * cur_iq

        # C_trend через tanh
        iq_diff = cur_iq - self.iq_slow[ticker]
        c_trend = 1.0 + 0.3 * math.tanh(iq_diff / 2.0)

        # Финальный IQ с тренд-контекстом
        final_iq = cur_iq * c_trend

        iqh = self.iq_history[ticker]
        iqh.append(final_iq)
        if len(iqh) > 15:
            iqh.pop(0)

        # === ЛОГИКА ПОЗИЦИЙ ===
        need_exit = False
        exit_price = 0.0
        exit_reason = ""
        exit_prof = 0.0

        with self._data_lock:
            if self.data["day_open_prices"].get(ticker, 0) == 0:
                self.data["day_open_prices"][ticker] = price

            active_pos = self.data["pos"] if self.mode == "REAL" else self.data["test_pos"]
            active_limits = self.data["limits"] if self.mode == "REAL" else self.data["test_limits"]

            if ticker in active_pos:
                p = active_pos[ticker]

                if p.get("status") == "SHADOW":
                    if final_iq >= 3.0:
                        p["status"] = "FIRM"
                        p["entry_iq_real"] = final_iq
                        logger.info(f"ПОДТВЕРЖДЕНО: {ticker}")
                    return

                prof = ((price - p["p"]) / p["p"]) if p["side"] == "BUY" else ((p["p"] - price) / p["p"])
                hold_time = (dt.now(self.tz) - dt.fromtimestamp(p.get("entry_time", time.time()), self.tz)).seconds

                exit_condition = False
                if p["side"] == "BUY" and snap["ask_wall"] > 0 and price < snap["ask_wall"] and final_iq < 0.8:
                    exit_reason = "WALL-REJECTION"
                    exit_prof = prof
                    exit_condition = True

                cr = spec.get("comm_buffer", 0.0005)
                if not exit_condition and final_iq < 1.0 and prof < -(cr * 1.5):
                    exit_reason = "REVERSAL-EXIT"
                    exit_prof = prof
                    exit_condition = True

                if not exit_condition:
                    hold_mult = 0.50 if hold_time < 15 else (0.80 if prof < 0.0015 else 0.60)
                    if final_iq <= p.get("entry_iq_real", 3.0) * hold_mult:
                        exit_reason = "IQ-DYNAMIC-EXIT"
                        exit_prof = prof
                        exit_condition = True

                if exit_condition:
                    need_exit = True
                    exit_price = price

                if not need_exit and prof > 0.003:
                    tr = (
                        p["p"] + (p["p"] * DIANA_TIGHT_TRAIL)
                        if p["side"] == "BUY"
                        else p["p"] - (p["p"] * DIANA_TIGHT_TRAIL)
                    )
                    if (price > tr and p["side"] == "BUY") or (price < tr and p["side"] == "SELL"):
                        p["p"] = tr
            else:
                if not self.data.get("search_active", True):
                    return

                dpnl = self.data["daily_pnl"] if self.mode == "REAL" else self.data.get("test_pnl", 0.0)
                if dpnl < -DAILY_LIMIT_PCT * 100:
                    return
                if active_limits.get(mkt, 0) <= 0:
                    return
                if not self.check_volatility(ticker, price):
                    return

                base_thr = IQ_FUTURES_THRESHOLD if ticker in ("GOLD", "Si") else IQ_STOCKS_THRESHOLD
                iq_thr = self.get_adaptive_iq_threshold(ticker, base_thr)
                if final_iq < iq_thr:
                    return

                # === СПРИНТ 2: Effective Spread Model ===
                side = "BUY" if snap["bid_power"] > snap["ask_power"] else "SELL"
                risk = MARGIN_FACTOR * active_limits.get(mkt, 10000)
                lot = min(max(1, int(risk / (price * 0.01))), MAX_POSITION_LOTS)

                best_level_vol = bids[0]['volume'] if side == "BUY" else asks[0]['volume']
                slippage_ratio = lot / max(best_level_vol, 1.0)
                expected_slippage = (current_spread / 2.0) + 0.1 * (slippage_ratio ** 2)

                iq_excess = final_iq - iq_thr
                potential_profit_ratio = max(0.0, iq_excess * 0.0005)

                if expected_slippage > potential_profit_ratio * 0.5:
                    logger.debug(f"Отсечено (Slippage): {ticker} | Ожид. проскальз: {expected_slippage:.5f} | Потенц. профит: {potential_profit_ratio:.5f}")
                    return

                frozen = (risk * 0.3) + (spec.get("comm_fixed", 2.0) * lot)
                active_limits[mkt] = round(active_limits.get(mkt, 0.0) - frozen, 2)

                pos = {
                    "ticker": ticker, "side": side, "lot": lot, "p": price, "mkt": mkt,
                    "entry_time": time.time(), "status": "SHADOW", "frozen_margin": frozen,
                    "comm_paid": 0.0, "entry_iq_real": final_iq, "peak_iq": final_iq, "max_prof": 0.0
                }
                active_pos[ticker] = pos
                logger.info(f"ОТКРЫТА: {ticker} ({side}) {lot} лотов @ {price} | IQ: {final_iq:.2f} | C_trend: {c_trend:.2f}")

        if need_exit:
            await self.exit_trade(ticker, exit_price, exit_reason, exit_prof)

        await self.safe_save()

# ============================================================================
# WEBSOCKET MARKET DATA
# ============================================================================
ALOR_WS_URL = "wss://api.alor.ru/ws"

async def _get_access_token(http_session: aiohttp.ClientSession) -> Optional[str]:
    try:
        url = f"https://oauth.alor.ru/refresh?token={CONFIG['ALOR_TOKEN']}"
        async with http_session.post(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                body = await r.json()
                token = body.get("AccessToken", "")
                if token:
                    logger.info("AccessToken получен для WS")
                    return token
                logger.error(f"OAuth ответ без AccessToken: {body}")
            else:
                text = await r.text()
                logger.error(f"OAuth HTTP {r.status}: {text[:300]}")
    except Exception as e:
        logger.error(f"OAuth error: {e}")
    return None

async def ws_market_data_feed(bot: TitanAbsoluteMonolith):
    while True:
        access_token = None
        try:
            async with aiohttp.ClientSession() as session:
                access_token = await _get_access_token(session)

            if not access_token:
                logger.warning("Не удалось получить AccessToken. Повтор через 10с...")
                await asyncio.sleep(10)
                continue

            logger.info(f"Подключение к Alor WebSocket: {ALOR_WS_URL}")

            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ALOR_WS_URL, heartbeat=30, ssl=ssl_context) as ws:
                    logger.info("WS-соединение установлено")

                    for ticker in BASE_ASSETS:
                        sub_msg = {
                            "opcode": "OrderBookGetAndSubscribe",
                            "code": ticker,
                            "depth": 10,
                            "exchange": "MOEX",
                            "format": "Simple",
                            "frequency": 0,
                            "guid": str(uuid.uuid4()),
                            "token": access_token
                        }
                        await ws.send_json(sub_msg)
                        logger.debug(f"Подписка: {ticker}")

                    logger.info(f"Подписано {len(BASE_ASSETS)} активов")

                    async for raw_msg in ws:
                        if raw_msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(raw_msg.data)
                                if data.get("opcode") == "OrderBook":
                                    ticker = data.get("code")
                                    if ticker:
                                        bids = data.get("bids", [])
                                        asks = data.get("asks", [])
                                        if bids and asks:
                                            price = (bids[0]['price'] + asks[0]['price']) / 2
                                            await bot.process_tick(ticker, price, {"bids": bids, "asks": asks})
                            except Exception as e:
                                logger.debug(f"WS parse error: {e}")
                        elif raw_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

        except Exception as e:
            logger.error(f"WS critical error: {e}")

        logger.warning("WS переподключение через 5с...")
        await asyncio.sleep(5)

# ============================================================================
# UI: DASHBOARD
# ============================================================================
class DashboardScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bot = None

        layout = BoxLayout(orientation='vertical', padding=10, spacing=10)

        title = Label(
            text="TITAN Pro",
            font_size=dp(28),
            bold=True,
            size_hint_y=None,
            height=dp(60),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(title)

        self.pnl_label = Label(
            text="Общий PnL: 0р",
            font_size=dp(24),
            bold=True,
            size_hint_y=None,
            height=dp(80),
            color=(0.2, 0.9, 0.2, 1)
        )
        layout.add_widget(self.pnl_label)

        self.daily_pnl_label = Label(
            text="Сегодня: 0р",
            font_size=dp(20),
            size_hint_y=None,
            height=dp(60),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(self.daily_pnl_label)

        self.status_label = Label(
            text="Статус: ПАУЗА",
            font_size=dp(18),
            size_hint_y=None,
            height=dp(50),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(self.status_label)

        self.mode_label = Label(
            text="Режим: TEST",
            font_size=dp(16),
            size_hint_y=None,
            height=dp(40),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(self.mode_label)

        btn_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(60), spacing=10)

        self.start_btn = Button(
            text="СТАРТ",
            font_size=dp(16),
            bold=True,
            background_color=(0.2, 0.8, 0.2, 1)
        )
        self.start_btn.bind(on_press=self.start_trading)
        btn_layout.add_widget(self.start_btn)

        self.stop_btn = Button(
            text="СТОП",
            font_size=dp(16),
            bold=True,
            background_color=(0.9, 0.2, 0.2, 1)
        )
        self.stop_btn.bind(on_press=self.stop_trading)
        btn_layout.add_widget(self.stop_btn)

        self.mode_btn = Button(
            text="РЕЖИМ",
            font_size=dp(16),
            bold=True,
            background_color=(0.2, 0.4, 0.8, 1)
        )
        self.mode_btn.bind(on_press=self.switch_mode)
        btn_layout.add_widget(self.mode_btn)

        layout.add_widget(btn_layout)

        pulse_label = Label(
            text="--- Пульс рынка ---",
            font_size=dp(16),
            bold=True,
            size_hint_y=None,
            height=dp(30),
            color=(0.9, 0.9, 0.2, 1)
        )
        layout.add_widget(pulse_label)

        self.pulse_layout = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            height=dp(150),
            spacing=3
        )
        layout.add_widget(self.pulse_layout)

        positions_label = Label(
            text="Активные позиции:",
            font_size=dp(18),
            bold=True,
            size_hint_y=None,
            height=dp(40),
            halign='left',
            color=(1, 1, 1, 1)
        )
        layout.add_widget(positions_label)

        scroll = ScrollView(size_hint=(1, 1))
        self.positions_layout = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            height=dp(300),
            spacing=5
        )
        scroll.add_widget(self.positions_layout)
        layout.add_widget(scroll)

        nav_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(60), spacing=5)

        dashboard_btn = Button(text="Главная", font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
        dashboard_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'dashboard'))
        nav_layout.add_widget(dashboard_btn)

        history_btn = Button(text="История", font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
        history_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'history'))
        nav_layout.add_widget(history_btn)

        params_btn = Button(text="Параметры", font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
        params_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'settings'))
        nav_layout.add_widget(params_btn)

        market_btn = Button(text="Рынок", font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
        market_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'market'))
        nav_layout.add_widget(market_btn)

        layout.add_widget(nav_layout)
        self.add_widget(layout)

    def start_trading(self, instance):
        async def do_start():
            self.bot.data["search_active"] = True
            self.status_label.text = "Статус: АКТИВЕН"
            self.status_label.color = (0.2, 0.9, 0.2, 1)
            await self.bot.safe_save()
            logger.info("Бот ЗАПУЩЕН")
        self.bot.run_async_threadsafe(do_start())

    def stop_trading(self, instance):
        async def do_stop():
            self.bot.data["search_active"] = False
            self.status_label.text = "Статус: ПАУЗА"
            self.status_label.color = (1, 1, 1, 1)
            await self.bot.safe_save()
            logger.info("Поиск новых сделок ОСТАНОВЛЕН")
        self.bot.run_async_threadsafe(do_stop())

    def switch_mode(self, instance):
        async def do_switch():
            self.bot.mode = "REAL" if self.bot.mode == "TEST" else "TEST"
            CONFIG["MODE"] = self.bot.mode
            self.mode_label.text = f"Режим: {self.bot.mode}"
            await self.bot.safe_save()
        self.bot.run_async_threadsafe(do_switch())

    def update_data(self, data_snapshot):
        total = data_snapshot.get("total_pnl", 0)
        daily = data_snapshot.get("daily_pnl", 0)

        color = (0.2, 0.9, 0.2, 1) if total >= 0 else (0.9, 0.2, 0.2, 1)
        self.pnl_label.text = f"Общий PnL: {total:.2f}р"
        self.pnl_label.color = color

        color = (0.2, 0.9, 0.2, 1) if daily >= 0 else (0.9, 0.2, 0.2, 1)
        self.daily_pnl_label.text = f"Сегодня: {daily:.2f}р"
        self.daily_pnl_label.color = color

        # Проверка warmup
        market = data_snapshot.get("market", {})
        any_warmup = any(market.get(t, {}).get("warmup", 100) < 100 for t in BASE_ASSETS)

        if any_warmup:
            min_warmup = min(market.get(t, {}).get("warmup", 100) for t in BASE_ASSETS)
            self.status_label.text = f"Статус: ПРОГРЕВ ({min_warmup}/100)"
            self.status_label.color = (0.9, 0.9, 0.2, 1)
        else:
            status = "АКТИВЕН" if data_snapshot.get("search_active") else "ПАУЗА"
            status_color = (0.2, 0.9, 0.2, 1) if data_snapshot.get("search_active") else (1, 1, 1, 1)
            self.status_label.text = f"Статус: {status}"
            self.status_label.color = status_color

        self.mode_label.text = f"Режим: {data_snapshot.get('mode', 'TEST')}"

        self.pulse_layout.clear_widgets()
        for ticker in BASE_ASSETS:
            info = market.get(ticker, {"iq": 0, "price": 0, "iq_slow": 5.0})
            iq = info["iq"]
            iq_slow = info.get("iq_slow", 5.0)
            price = info["price"]

            if iq >= 7.0:
                status_text = "ВХОД"
                color = (0.2, 0.9, 0.2, 1)
            elif iq >= 3.0:
                status_text = "ТЕНЬ"
                color = (0.9, 0.9, 0.2, 1)
            elif iq >= 1.0:
                status_text = "ВЫХОД"
                color = (0.9, 0.5, 0.2, 1)
            else:
                status_text = "ТИШИНА"
                color = (0.5, 0.5, 0.5, 1)

            trend_arrow = "+" if iq > iq_slow else "-" if iq < iq_slow else "="
            asset_label = Label(
                text=f"{ticker}: {status_text} | IQ: {iq:.2f} ({trend_arrow}) | {price:.2f}р",
                font_size=dp(14),
                size_hint_y=None,
                height=dp(30),
                color=color
            )
            self.pulse_layout.add_widget(asset_label)

        self.positions_layout.clear_widgets()
        active_pos = data_snapshot.get("pos", {}) if data_snapshot.get("mode") == "REAL" else data_snapshot.get("test_pos", {})

        if not active_pos:
            label = Label(
                text="Нет активных позиций",
                size_hint_y=None,
                height=dp(40),
                color=(0.7, 0.7, 0.7, 1)
            )
            self.positions_layout.add_widget(label)
        else:
            for ticker, pos in active_pos.items():
                side_icon = "[BUY]" if pos['side'] == 'BUY' else "[SELL]"
                pos_label = Label(
                    text=f"{side_icon} {ticker} | {pos['side']} {pos['lot']} лотов\n"
                         f"Вход: {pos['p']:.2f}р",
                    size_hint_y=None,
                    height=dp(60),
                    color=(1, 1, 1, 1)
                )
                self.positions_layout.add_widget(pos_label)

# ============================================================================
# UI: HISTORY
# ============================================================================
class HistoryScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        layout = BoxLayout(orientation='vertical', padding=10, spacing=10)

        title = Label(
            text="История сделок",
            font_size=dp(24),
            bold=True,
            size_hint_y=None,
            height=dp(60),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(title)

        self.stats_label = Label(
            text="Всего сделок: 0\nВинрейт: 0%\nСредний профит: 0р",
            font_size=dp(16),
            size_hint_y=None,
            height=dp(100),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(self.stats_label)

        scroll = ScrollView(size_hint=(1, 1))
        self.history_layout = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            height=dp(400),
            spacing=5
        )
        scroll.add_widget(self.history_layout)
        layout.add_widget(scroll)

        nav_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(60), spacing=5)
        for name, screen in [("Главная", "dashboard"), ("История", "history"),
                             ("Параметры", "settings"), ("Рынок", "market")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=screen: setattr(self.manager, 'current', s))
            nav_layout.add_widget(btn)
        layout.add_widget(nav_layout)
        self.add_widget(layout)

    def update_data(self, data_snapshot):
        trades = data_snapshot.get("trade_history", [])

        if not trades:
            self.stats_label.text = "Всего сделок: 0\nВинрейт: 0%\nСредний профит: 0р"
            self.history_layout.clear_widgets()
            label = Label(text="История пуста", size_hint_y=None, height=dp(40), color=(0.7, 0.7, 0.7, 1))
            self.history_layout.add_widget(label)
            return

        total = len(trades)
        wins = sum(1 for t in trades if t['pnl'] > 0)
        winrate = (wins / total * 100) if total > 0 else 0
        avg_profit = sum(t['pnl'] for t in trades) / total if total > 0 else 0

        self.stats_label.text = (
            f"Всего сделок: {total}\n"
            f"Винрейт: {winrate:.1f}%\n"
            f"Средний профит: {avg_profit:.2f}р"
        )

        self.history_layout.clear_widgets()
        for trade in reversed(trades[-20:]):
            icon = "[OK]" if trade['pnl'] > 0 else "[X]"
            color = (0.2, 0.9, 0.2, 1) if trade['pnl'] > 0 else (0.9, 0.2, 0.2, 1)

            trade_label = Label(
                text=f"{icon} {trade['ticker']} ({trade['side']})\n"
                     f"Вход: {trade['entry_price']:.2f} -> Выход: {trade['exit_price']:.2f}\n"
                     f"PnL: {trade['pnl']:.2f}р | {trade['reason']}\n"
                     f"Время: {trade['time']}",
                size_hint_y=None,
                height=dp(100),
                color=color
            )
            self.history_layout.add_widget(trade_label)

# ============================================================================
# UI: SETTINGS (Параметры)
# ============================================================================
class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        layout = BoxLayout(orientation='vertical', padding=10, spacing=10)

        title = Label(
            text="Параметры стратегии",
            font_size=dp(24),
            bold=True,
            size_hint_y=None,
            height=dp(60),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(title)

        settings = [
            ("Фактор маржи:", f"{MARGIN_FACTOR}"),
            ("Дневной лимит:", f"{DAILY_LIMIT_PCT}%"),
            ("IQ порог (акции):", f"{IQ_STOCKS_THRESHOLD:.2f}"),
            ("IQ порог (фьючерсы):", f"{IQ_FUTURES_THRESHOLD:.2f}"),
            ("Макс. спред (мин):", f"{MAX_SPREAD_LIMIT}"),
            ("Trailing stop:", f"{DIANA_TIGHT_TRAIL}"),
            ("Warmup тиков:", f"100"),
            ("Spread K (Z-score):", f"1.5"),
            ("Vol Sensitivity:", f"0.5"),
        ]

        for label_text, value_text in settings:
            box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50))
            label = Label(text=label_text, font_size=dp(16), color=(1, 1, 1, 1))
            value = Label(text=value_text, font_size=dp(16), color=(0.2, 0.8, 1, 1))
            box.add_widget(label)
            box.add_widget(value)
            layout.add_widget(box)

        nav_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(60), spacing=5)
        for name, screen in [("Главная", "dashboard"), ("История", "history"),
                             ("Параметры", "settings"), ("Рынок", "market")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=screen: setattr(self.manager, 'current', s))
            nav_layout.add_widget(btn)
        layout.add_widget(nav_layout)
        self.add_widget(layout)

    def update_data(self, data_snapshot):
        pass

# ============================================================================
# UI: MARKET
# ============================================================================
class MarketScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        layout = BoxLayout(orientation='vertical', padding=10, spacing=10)

        title = Label(
            text="Рынок",
            font_size=dp(24),
            bold=True,
            size_hint_y=None,
            height=dp(60),
            color=(1, 1, 1, 1)
        )
        layout.add_widget(title)

        scroll = ScrollView(size_hint=(1, 1))
        self.assets_layout = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            height=dp(500),
            spacing=5
        )
        scroll.add_widget(self.assets_layout)
        layout.add_widget(scroll)

        nav_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(60), spacing=5)
        for name, screen in [("Главная", "dashboard"), ("История", "history"),
                             ("Параметры", "settings"), ("Рынок", "market")]:
            btn = Button(text=name, font_size=dp(14), background_color=(0.2, 0.6, 0.9, 1))
            btn.bind(on_press=lambda x, s=screen: setattr(self.manager, 'current', s))
            nav_layout.add_widget(btn)
        layout.add_widget(nav_layout)
        self.add_widget(layout)

    def update_data(self, data_snapshot):
        self.assets_layout.clear_widgets()
        market = data_snapshot.get("market", {})

        for ticker in BASE_ASSETS:
            info = market.get(ticker, {"iq": 0, "price": 0, "iq_slow": 5.0})
            iq = info["iq"]
            iq_slow = info.get("iq_slow", 5.0)
            price = info["price"]

            if iq >= 7:
                iq_color = (0.2, 0.9, 0.2, 1)
            elif iq >= 3:
                iq_color = (0.9, 0.9, 0.2, 1)
            else:
                iq_color = (0.9, 0.2, 0.2, 1)

            trend = "▲" if iq > iq_slow else "▼" if iq < iq_slow else "—"
            asset_label = Label(
                text=f"{ticker} {trend}\nЦена: {price:.2f}р | IQ: {iq:.2f} | Slow: {iq_slow:.2f}",
                size_hint_y=None,
                height=dp(60),
                color=iq_color
            )
            self.assets_layout.add_widget(asset_label)

# ============================================================================
# ОСНОВНОЕ ПРИЛОЖЕНИЕ
# ============================================================================
class TITANProApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = "TITAN Pro"
        self.bot = TitanAbsoluteMonolith()
        self._loop_thread = None

    def build(self):
        sm = ScreenManager()

        dashboard = DashboardScreen(name='dashboard')
        dashboard.bot = self.bot
        sm.add_widget(dashboard)

        sm.add_widget(HistoryScreen(name='history'))
        sm.add_widget(SettingsScreen(name='settings'))
        sm.add_widget(MarketScreen(name='market'))

        Clock.schedule_once(self.start_bot, 0.5)
        Clock.schedule_interval(self.update_ui, 1.0)

        return sm

    async def async_main(self):
        self.bot._loop = asyncio.get_running_loop()
        await self.bot.start()
        await ws_market_data_feed(self.bot)

    def start_bot(self, dt):
        def run_loop():
            asyncio.run(self.async_main())
        self._loop_thread = threading.Thread(target=run_loop, daemon=True)
        self._loop_thread.start()

    def update_ui(self, dt):
        data_snapshot = self.bot.get_safe_data()
        sm = self.root
        sm.get_screen('dashboard').update_data(data_snapshot)
        sm.get_screen('history').update_data(data_snapshot)
        sm.get_screen('settings').update_data(data_snapshot)
        sm.get_screen('market').update_data(data_snapshot)

    def on_stop(self):
        if self.bot:
            self.bot.run_async_threadsafe(self.bot.stop())

if __name__ == '__main__':
    TITANProApp().run()
