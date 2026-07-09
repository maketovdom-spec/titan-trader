import sys
import os
import time
import json
import asyncio
import uuid
import logging
import ssl
from datetime import datetime as dt
from typing import Dict, Optional, Callable

import aiohttp
import pytz

# Kivy imports
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelHeader
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.core.window import Window
from kivy.clock import Clock

# ============================================================================
# КОНФИГУРАЦИЯ И БЕЗОПАСНОСТЬ
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TITAN")

CONFIG = {
    "ALOR_TOKEN": os.getenv("ALOR_TOKEN", "c4a6afed-0dae-4057-8289-d516c7b09feb"),
    "MAX_BOT_TOKEN": os.getenv("MAX_BOT_TOKEN", "f9LHodD0cOIkLfmwmhn9Z7G5NZAX1PyrIjcM0vE7R0TayOs0aHOIRtkXNbC8d6KocFcxoeNQttyjiNWRgkH-"),
    "MAX_CHAT_ID": os.getenv("MAX_CHAT_ID", "352713016"),
    "MAX_API_URL": "https://platform-api2.max.ru",
    "SALT": os.getenv("TITAN_SALT", "NEVINNOMYSSK_TITAN_2026"),
    "MODE": os.getenv("TITAN_MODE", "TEST"),
    "CHANNEL_MODE": os.getenv("TITAN_CHANNEL", "MAX"),
}

DAILY_LIMIT_PCT = 3.5
MARGIN_FACTOR = 0.15
WALL_MULTIPLIER = 5.5
MAX_SPREAD_LIMIT = 0.0006
IQ_STOCKS_THRESHOLD = 7.0
IQ_FUTURES_THRESHOLD = 3.0
VOL_BREATH_THRESHOLD = 0.4
DIANA_TIGHT_TRAIL = 0.0015
MAX_POSITION_LOTS = 1000

PORTFOLIOS = {"FUT": "7502Y5H", "STK": "D101327", "FX": "G68390"}
BASE_ASSETS = ["SBER", "GAZP", "Si", "CNY", "GOLD", "VTBR", "MGNT", "LKOH"]
moscow_tz = pytz.timezone('Europe/Moscow')

STATE_FILE = "titan_monolith.json"
STATE_TMP_FILE = "titan_monolith.tmp.json"

Window.clearcolor = (0.1, 0.1, 0.15, 1)


def verify_auth(config: dict) -> bool:
    return config.get('SALT', '') == "NEVINNOMYSSK_TITAN_2026"


if not verify_auth(CONFIG):
    logger.critical("❌ ОШИБКА АВТОРИЗАЦИИ.")
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
# УВЕДОМЛЕНИЯ (MAX + TELEGRAM)
# ============================================================================
class ChannelManager:
    def __init__(self, config: dict):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_context)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def notify(self, text: str):
        mode = self.config.get("CHANNEL_MODE", "MAX").upper()
        if mode in ("MAX", "BOTH"):
            await self._send_max(text)
        if mode in ("TELEGRAM_ONLY", "BOTH"):
            await self._send_tg(text)

    async def _send_max(self, text: str):
        token = self.config.get("MAX_BOT_TOKEN", "")
        chat_id = self.config.get("MAX_CHAT_ID", "")
        api_url = self.config.get("MAX_API_URL", "https://platform-api2.max.ru")
        
        if not token or not chat_id:
            return
        
        try:
            session = await self._get_session()
            url = f"{api_url}/messages?chat_id={chat_id}"
            payload = {"text": text, "format": "markdown"}
            headers = {"Authorization": token, "Content-Type": "application/json"}
            
            async with session.post(url, json=payload, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15),
                                   ssl=self._ssl_context) as resp:
                if resp.status == 200:
                    logger.info(f"✅ [MAX] Отправлено")
        except Exception as e:
            logger.warning(f"❌ [MAX] Ошибка: {e}")

    async def _send_tg(self, text: str):
        token = self.config.get("TG_BOT_TOKEN", "")
        chat_id = self.config.get("TG_CHAT_ID", "")
        if not token or not chat_id:
            return
        try:
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if not resp.ok:
                    logger.warning(f"Telegram error: {resp.status}")
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================================
# TITAN MONOLITH CORE
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

    def __init__(self, notifier: ChannelManager):
        self.notify = notifier
        self.mode = CONFIG.get("MODE", "TEST")
        self.tz = moscow_tz
        self.data = load_state()

        self.price_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.iq_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.tick_timestamps: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.range_history: Dict[str, list] = {t: [] for t in BASE_ASSETS}
        self.walls: Dict[str, dict] = {
            t: {"bid_wall": 0, "ask_wall": 0, "bid_power": 0, "ask_power": 0}
            for t in BASE_ASSETS
        }

        self.jwt: str = ""
        self.jwt_expiry: float = 0
        self._jwt_lock = asyncio.Lock()
        self._http: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._http = aiohttp.ClientSession()
        await self.notify.notify(f"🚀 TITAN запущен!\n📊 Режим: {self.mode}\n⏰ {dt.now(self.tz).strftime('%H:%M:%S')}")

    async def stop(self):
        if self._http and not self._http.closed:
            await self._http.close()
        await self.notify.notify("🛑 TITAN остановлен")
        await self.notify.close()

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
            logger.info(f"🧪 [TEST] {side} {ticker} {qty} @ {price}")
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
                    logger.info(f"✅ ОРДЕР: {side} {ticker} {qty} @ {fp}")
                    return body.get('orderNumber')
                else:
                    text = await r.text()
                    logger.error(f"❌ Order HTTP {r.status}: {text}")
        except Exception as e:
            logger.error(f"❌ Order error: {e}")
        return None

    @staticmethod
    def _sort_book(bids: list, asks: list):
        bids_sorted = sorted(bids, key=lambda x: x['price'], reverse=True)
        asks_sorted = sorted(asks, key=lambda x: x['price'])
        return bids_sorted, asks_sorted

    def analyze_book(self, ticker: str, bids: list, asks: list) -> Optional[dict]:
        if not bids or not asks:
            return None
        bids, asks = self._sort_book(bids, asks)

        bp = sum(b['volume'] for b in bids[:5])
        ap = sum(a['volume'] for a in asks[:5])
        abv = sum(b['volume'] for b in bids) / len(bids) if bids else 1
        aav = sum(a['volume'] for a in asks) / len(asks) if asks else 1
        bb = [b for b in bids if b['volume'] > abv * WALL_MULTIPLIER]
        ba = [a for a in asks if a['volume'] > aav * WALL_MULTIPLIER]

        wall_data = {
            "bid_wall": bb[0]['price'] if bb else 0,
            "ask_wall": ba[0]['price'] if ba else 0,
            "bid_power": bp,
            "ask_power": ap
        }
        self.walls[ticker] = wall_data
        return wall_data

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
        await self.notify.notify(
            f"🏁 ВЫХОД {ticker} ({reason}) | Профит: {round(prof * 100, 3)}% | Чистыми: {net}₽"
        )

    async def process_tick(self, ticker: str, price: float, book: dict):
        if self.mode == "TEST" and not book.get('bids'):
            book = {
                'bids': [{'price': price * 0.9999, 'volume': 1000}],
                'asks': [{'price': price * 1.0001, 'volume': 1000}]
            }

        self.tick_timestamps[ticker].append(time.time())
        bids = book.get('bids', [])
        asks = book.get('asks', [])

        snap = self.analyze_book(ticker, bids, asks)
        if not snap or not self.is_logical_trade(ticker, snap):
            return

        if bids and asks and asks[0]['price'] and bids[0]['price']:
            spread = (asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 0.001)
            if spread > MAX_SPREAD_LIMIT:
                return

        is_fut = ticker in ("GOLD", "Si")
        is_fx = ticker == "CNY"
        mkt = "FUT" if is_fut else ("FX" if is_fx else "STK")
        spec = self.ASSET_PARAMS.get(ticker, self.ASSET_PARAMS["DEFAULT"])

        ph = self.price_history[ticker]
        ph.append(price)
        if len(ph) > 10:
            ph.pop(0)

        wb = 1.35 if snap["bid_wall"] > 0 else 1.0
        raw_iq = (snap["bid_power"] / max(1, snap["ask_power"])) * wb
        prev_iq = self.iq_history[ticker][-1] if self.iq_history[ticker] else raw_iq
        cur_iq = round((raw_iq * 0.7) + (prev_iq * 0.3), 2)

        iqh = self.iq_history[ticker]
        iqh.append(cur_iq)
        if len(iqh) > 15:
            iqh.pop(0)

        if self.data["day_open_prices"].get(ticker, 0) == 0:
            self.data["day_open_prices"][ticker] = price
            await self.safe_save()

        active_pos = self.data["pos"] if self.mode == "REAL" else self.data["test_pos"]
        active_limits = self.data["limits"] if self.mode == "REAL" else self.data["test_limits"]

        if ticker in active_pos:
            p = active_pos[ticker]

            if p.get("status") == "SHADOW":
                if cur_iq >= 3.0:
                    p["status"] = "FIRM"
                    p["entry_iq_real"] = cur_iq
                    await self.safe_save()
                    await self.notify.notify(f"💎 ПОДТВЕРЖДЕНО: {ticker}")
                return

            prof = ((price - p["p"]) / p["p"]) if p["side"] == "BUY" else ((p["p"] - price) / p["p"])
            hold_time = (dt.now(self.tz) - dt.fromtimestamp(p.get("entry_time", time.time()), self.tz)).seconds

            if p["side"] == "BUY" and snap["ask_wall"] > 0 and price < snap["ask_wall"] and cur_iq < 0.8:
                await self.exit_trade(ticker, price, "WALL-REJECTION", prof)
                return

            cr = spec.get("comm_buffer", 0.0005)
            if cur_iq < 1.0 and prof < -(cr * 1.5):
                await self.exit_trade(ticker, price, "REVERSAL-EXIT", prof)
                return

            hold_mult = 0.50 if hold_time < 15 else (0.80 if prof < 0.0015 else 0.60)
            if cur_iq <= p.get("entry_iq_real", 3.0) * hold_mult:
                await self.exit_trade(ticker, price, "IQ-DYNAMIC-EXIT", prof)
                return

            if prof > 0.003:
                tr = (
                    p["p"] + (p["p"] * DIANA_TIGHT_TRAIL)
                    if p["side"] == "BUY"
                    else p["p"] - (p["p"] * DIANA_TIGHT_TRAIL)
                )
                if (price > tr and p["side"] == "BUY") or (price < tr and p["side"] == "SELL"):
                    p["p"] = tr
                    await self.safe_save()

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

            iq_thr = IQ_FUTURES_THRESHOLD if ticker in ("GOLD", "Si") else IQ_STOCKS_THRESHOLD
            if cur_iq < iq_thr:
                return

            risk = MARGIN_FACTOR * active_limits.get(mkt, 10000)
            lot = min(max(1, int(risk / (price * 0.01))), MAX_POSITION_LOTS)
            frozen = (risk * 0.3) + (spec.get("comm_fixed", 2.0) * lot)
            active_limits[mkt] = round(active_limits.get(mkt, 0.0) - frozen, 2)

            side = "BUY" if snap["bid_power"] > snap["ask_power"] else "SELL"
            pos = {
                "ticker": ticker, "side": side, "lot": lot, "p": price, "mkt": mkt,
                "entry_time": time.time(), "status": "SHADOW", "frozen_margin": frozen,
                "comm_paid": 0.0, "entry_iq_real": cur_iq, "peak_iq": cur_iq, "max_prof": 0.0
            }
            active_pos[ticker] = pos
            await self.safe_save()
            await self.notify.notify(
                f"🚀 ОТКРЫТА: {ticker} ({side}) {lot} лотов @ {price}\n🎯 IQ: {cur_iq}"
            )


# ============================================================================
# СИСТЕМА УПРАВЛЕНИЯ: МЕНЮ И КНОПКИ
# ============================================================================
class MenuBuilder:
    @staticmethod
    def main_menu() -> str:
        return (
            "🎛️ **TITAN CONTROL PANEL** 🎛️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Выберите команду (напишите цифру):\n\n"
            "1️⃣ ▶️ **Старт / Пауза** (поиск сделок)\n"
            "2️⃣ 🔄 **Сменить режим** (TEST ↔ REAL)\n"
            "3️⃣ 📊 **Мои позиции** (открытые сделки)\n"
            "4️⃣ 💰 **Отчет PnL** (прибыль/убыток)\n"
            "5️⃣ 🧠 **IQ Армии** (состояние активов)\n"
            "6️⃣ 🛑 **ЭКСТРЕННЫЙ ВЫХОД** (закрыть всё)\n"
            "7️⃣ 📡 **Статус системы** (пинг, WS)\n"
            "8️⃣ ⚙️ **Настройки** (факторы риска)\n"
            "9️⃣ 📜 **История сделок** (последние 10)\n\n"
            "💡 *Или введите команду: /help*"
        )

    @staticmethod
    def positions_menu(positions: dict) -> str:
        if not positions:
            return "📭 Позиций нет. Армия в тени."
        
        text = "📊 **АКТИВНЫЕ ПОЗИЦИИ** 📊\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, (ticker, pos) in enumerate(positions.items(), 1):
            side_icon = "🟢" if pos["side"] == "BUY" else "🔴"
            text += f"{idx}. {side_icon} **{ticker}** ({pos['side']}) {pos['lot']} лотов\n"
            text += f"   Вход: {pos['p']:.2f}₽ | Рынок: {pos['mkt']}\n"
            text += f"   *Напишите номер, чтобы закрыть*\n\n"
        
        return text

    @staticmethod
    def settings_menu() -> str:
        return (
            "⚙️ **НАСТРОЙКИ РИСКА** ⚙️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Фактор маржи: {MARGIN_FACTOR}\n"
            f"Дневной лимит: {DAILY_LIMIT_PCT}%\n"
            f"IQ порог (акции): {IQ_STOCKS_THRESHOLD}\n"
            f"IQ порог (фьючи): {IQ_FUTURES_THRESHOLD}\n\n"
            "Что изменить?\n"
            "1. Фактор маржи\n"
            "2. Дневной лимит\n"
            "3. IQ пороги\n"
            "4. Назад в главное меню"
        )


class CommandRouter:
    def __init__(self, bot: 'TitanAbsoluteMonolith'):
        self.bot = bot
        self.routes: Dict[str, Callable] = {
            "/menu": self.cmd_menu,
            "/help": self.cmd_help,
            "/start": self.cmd_toggle_pause,
            "/pause": self.cmd_toggle_pause,
            "/mode": self.cmd_switch_mode,
            "/pos": self.cmd_positions,
            "/pnl": self.cmd_pnl,
            "/iq": self.cmd_army_iq,
            "/kill": self.cmd_kill_all,
            "/status": self.cmd_status,
            "/settings": self.cmd_settings,
            "/history": self.cmd_history,
            "1": self.cmd_toggle_pause,
            "2": self.cmd_switch_mode,
            "3": self.cmd_positions,
            "4": self.cmd_pnl,
            "5": self.cmd_army_iq,
            "6": self.cmd_kill_all,
            "7": self.cmd_status,
            "8": self.cmd_settings,
            "9": self.cmd_history,
        }

    async def handle(self, command: str) -> str:
        cmd = command.lower().strip()
        
        if cmd in self.routes:
            try:
                return await self.routes[cmd]()
            except Exception as e:
                return f"❌ Ошибка выполнения: {e}"
        
        if cmd.isdigit():
            return await self._handle_position_choice(int(cmd))
        
        return "❓ Не понял команду. Напиши **/menu** для списка."

    async def cmd_menu(self) -> str:
        return MenuBuilder.main_menu()

    async def cmd_help(self) -> str:
        return (
            "🤖 **TITAN HELP** 🤖\n\n"
            "Я управляюсь через цифры или команды:\n"
            "• **/menu** - главное меню\n"
            "• **1-9** - выбор из меню\n"
            "• **/kill** - экстренный выход\n"
            "• **/pos** - мои позиции\n"
            "• **/pnl** - отчет по прибыли\n\n"
            "Напиши **/menu** чтобы начать."
        )

    async def cmd_toggle_pause(self) -> str:
        current = self.bot.data.get("search_active", True)
        self.bot.data["search_active"] = not current
        await self.bot.safe_save()
        
        if not current:
            return "▶️ **ВОЗОБНОВЛЕНО!** Титан ищет сделки."
        else:
            return "⏸️ **ПАУЗА.** Поиск остановлен, за позициями слежу."

    async def cmd_switch_mode(self) -> str:
        current = self.bot.mode
        new_mode = "REAL" if current == "TEST" else "TEST"
        self.bot.mode = new_mode
        CONFIG["MODE"] = new_mode
        await self.bot.safe_save()
        return f"🔄 **РЕЖИМ СМЕНЕН:** {current} → {new_mode}"

    async def cmd_positions(self) -> str:
        active_pos = self.bot.data["pos"] if self.bot.mode == "REAL" else self.bot.data["test_pos"]
        return MenuBuilder.positions_menu(active_pos)

    async def cmd_pnl(self) -> str:
        total = self.bot.data.get("total_pnl", 0)
        daily = self.bot.data.get("daily_pnl", 0)
        test = self.bot.data.get("test_pnl", 0)
        
        return (
            f"💰 **ОТЧЕТ PnL** 💰\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Режим: **{self.bot.mode}**\n"
            f"Всего: **{total:.2f}₽**\n"
            f"Сегодня: **{daily:.2f}₽**\n"
            f"Тестовый PnL: **{test:.2f}₽**\n\n"
            f"📅 Дата: {dt.now(self.bot.tz).strftime('%Y-%m-%d %H:%M')}"
        )

    async def cmd_army_iq(self) -> str:
        army = self.bot.data.get("army", {})
        text = "🧠 **IQ АРМИИ** 🧠\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for ticker, data in army.items():
            pnl = data.get("pnl_today", 0)
            state = data.get("state", "SHADOW")
            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            text += f"{icon} **{ticker}**: {pnl:.2f}₽ | {state}\n"
        
        return text

    async def cmd_kill_all(self) -> str:
        active_pos = self.bot.data["pos"] if self.bot.mode == "REAL" else self.bot.data["test_pos"]
        if not active_pos:
            return "📭 Нечего закрывать. Позиций нет."
        
        tasks = []
        for ticker in list(active_pos.keys()):
            price = self.bot.price_history[ticker][-1] if self.bot.price_history[ticker] else 0
            if price > 0:
                tasks.append(self.bot.exit_trade(ticker, price, "MANUAL KILL", 0))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            return "🛑 **ЭКСТРЕННЫЙ ВЫХОД!** Все позиции закрываются..."
        else:
            return "⚠️ Не удалось получить цены для закрытия."

    async def cmd_status(self) -> str:
        ws_alive = "✅" if self.bot._http else "❌"
        jwt_alive = "✅" if self.bot.jwt else "❌"
        
        return (
            f"📡 **СТАТУС СИСТЕМЫ** 📡\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Режим: **{self.bot.mode}**\n"
            f"Поиск: {'▶️ Активен' if self.bot.data.get('search_active') else '⏸️ Пауза'}\n"
            f"WebSocket: {ws_alive}\n"
            f"JWT Token: {jwt_alive}\n"
            f"Время: {dt.now(self.bot.tz).strftime('%H:%M:%S')}"
        )

    async def cmd_settings(self) -> str:
        return MenuBuilder.settings_menu()

    async def cmd_history(self) -> str:
        history = self.bot.data.get("trade_history", [])
        if not history:
            return "📜 **ИСТОРИЯ СДЕЛОК** 📜\n━━━━━━━━━━━━━━━━━━━━━━━\n\n📭 Сделок пока нет."
        
        text = "📜 **ИСТОРИЯ СДЕЛОК** 📜\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for trade in reversed(history[-10:]):
            icon = "✅" if trade["pnl"] > 0 else "❌"
            text += f"{icon} **{trade['ticker']}** ({trade['side']})\n"
            text += f"   Вход: {trade['entry_price']:.2f} → Выход: {trade['exit_price']:.2f}\n"
            text += f"   PnL: **{trade['pnl']:.2f}₽** | Причина: {trade['reason']}\n"
            text += f"   Время: {trade['time']}\n\n"
        
        return text

    async def _handle_position_choice(self, idx: int) -> str:
        active_pos = self.bot.data["pos"] if self.bot.mode == "REAL" else self.bot.data["test_pos"]
        
        if idx < 1 or idx > len(active_pos):
            return "❌ Нет такой позиции. Напишите **/menu**."
        
        ticker = list(active_pos.keys())[idx - 1]
        price = self.bot.price_history[ticker][-1] if self.bot.price_history[ticker] else 0
        
        if price > 0:
            await self.bot.exit_trade(ticker, price, "MANUAL CLOSE", 0)
            return f"🛑 Закрываю **{ticker}**..."
        else:
            return f"⚠️ Не удалось получить цену для **{ticker}**."


# ============================================================================
# MAX ADAPTER
# ============================================================================
class MaxAdapter:
    def __init__(self, config: dict, router: CommandRouter):
        self.config = config
        self.router = router
        self._session: Optional[aiohttp.ClientSession] = None
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_context)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def send_message(self, text: str, attachments: dict = None):
        token = self.config.get("MAX_BOT_TOKEN", "")
        chat_id = self.config.get("MAX_CHAT_ID", "")
        api_url = self.config.get("MAX_API_URL", "https://platform-api2.max.ru")
        
        if not token or not chat_id:
            return
        
        try:
            session = await self._get_session()
            url = f"{api_url}/messages?chat_id={chat_id}"
            payload = {"text": text, "format": "markdown"}
            if attachments:
                payload["attachments"] = [attachments]
            
            headers = {"Authorization": token, "Content-Type": "application/json"}
            
            async with session.post(url, json=payload, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15),
                                   ssl=self._ssl_context) as resp:
                if resp.status == 200:
                    logger.info(f"✅ [MAX] Отправлено")
                else:
                    error_text = await resp.text()
                    logger.warning(f"❌ [MAX] Ошибка {resp.status}: {error_text[:200]}")
        except Exception as e:
            logger.warning(f"❌ [MAX] Ошибка: {e}")

    async def send_menu(self, menu_text: str):
        attachments = {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": "▶️ Старт/Пауза", "payload": "1"},
                     {"type": "callback", "text": "🔄 Режим", "payload": "2"}],
                    [{"type": "callback", "text": "📊 Позиции", "payload": "3"},
                     {"type": "callback", "text": "💰 PnL", "payload": "4"}],
                    [{"type": "callback", "text": "🧠 IQ Армии", "payload": "5"},
                     {"type": "callback", "text": "🛑 Экстренный выход", "payload": "6"}],
                    [{"type": "callback", "text": "📡 Статус", "payload": "7"},
                     {"type": "callback", "text": "⚙️ Настройки", "payload": "8"}],
                    [{"type": "callback", "text": "📜 История", "payload": "9"}]
                ]
            }
        }
        await self.send_message(menu_text, attachments)

    async def _get_updates(self) -> list:
        token = self.config.get("MAX_BOT_TOKEN", "")
        api_url = self.config.get("MAX_API_URL", "https://platform-api2.max.ru")
        
        if not token:
            return []
        
        try:
            session = await self._get_session()
            url = f"{api_url}/updates?timeout=5"
            headers = {"Authorization": token}
            
            async with session.get(url, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10),
                                  ssl=self._ssl_context) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
                else:
                    return []
        except Exception as e:
            logger.debug(f"MAX /updates error: {e}")
            return []

    async def start_listening(self):
        logger.info("🎧 MAX Adapter: слушаю команды...")
        await self.send_menu(MenuBuilder.main_menu())
        
        while True:
            try:
                updates = await self._get_updates()
                
                for update in updates:
                    if update.get("type") == "message_callback":
                        callback_data = update.get("payload", "")
                        logger.info(f"🔘 Callback: {callback_data}")
                        response = await self.router.handle(callback_data)
                        await self.send_message(response)
                        continue
                    
                    if update.get("type") == "message":
                        text = update.get("text", "").strip()
                        if text:
                            logger.info(f"💬 Message: {text}")
                            response = await self.router.handle(text)
                            await self.send_message(response)
            except Exception as e:
                logger.error(f"MAX listening error: {e}")
            
            await asyncio.sleep(1)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================================
# WEBSOCKET MARKET DATA CLIENT (ALOR)
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
                    logger.info("✅ AccessToken получен для WS")
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
                logger.warning("⚠️ Не удалось получить AccessToken. Повтор через 10с...")
                await asyncio.sleep(10)
                continue

            logger.info(f"🔗 Подключение к Alor WebSocket: {ALOR_WS_URL}")

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ALOR_WS_URL, heartbeat=30) as ws:
                    logger.info("✅ WS-соединение установлено")

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
                        logger.debug(f"📩 Подписка: {ticker}")

                    logger.info(f"✅ Подписано {len(BASE_ASSETS)} активов")

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
        
        logger.warning("🔄 WS переподключение через 5с...")
        await asyncio.sleep(5)


# ============================================================================
# KIVY UI
# ============================================================================
class TITANButton(Button):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint_y = None
        self.height = 60
        self.font_size = 16
        self.bold = True
        self.background_color = (0.2, 0.4, 0.6, 1)
        self.color = (1, 1, 1, 1)


class ControlTab(BoxLayout):
    def __init__(self, bot: TitanAbsoluteMonolith, **kwargs):
        super().__init__(**kwargs)
        self.bot = bot
        self.orientation = 'vertical'
        self.padding = 10
        self.spacing = 10
        
        title = Label(text="🎛️ TITAN CONTROL", font_size=24, bold=True, 
                     size_hint_y=None, height=50, color=(1, 1, 1, 1))
        self.add_widget(title)
        
        self.btn_start = TITANButton(text="▶️ СТАРТ / ПАУЗА")
        self.btn_start.bind(on_press=self.toggle_pause)
        self.add_widget(self.btn_start)
        
        self.btn_mode = TITANButton(text=f"🔄 РЕЖИМ: {self.bot.mode}")
        self.btn_mode.bind(on_press=self.switch_mode)
        self.add_widget(self.btn_mode)
        
        self.btn_pnl = TITANButton(text="💰 PnL ОТЧЕТ")
        self.btn_pnl.bind(on_press=self.show_pnl)
        self.add_widget(self.btn_pnl)
        
        self.btn_positions = TITANButton(text="📊 ПОЗИЦИИ")
        self.btn_positions.bind(on_press=self.show_positions)
        self.add_widget(self.btn_positions)
        
        self.btn_kill = TITANButton(text="🛑 ЭКСТРЕННЫЙ ВЫХОД")
        self.btn_kill.background_color = (0.7, 0.2, 0.2, 1)
        self.btn_kill.bind(on_press=self.kill_all)
        self.add_widget(self.btn_kill)
        
        self.info_text = TextInput(
            text="Нажми кнопку для управления",
            readonly=True, multiline=True,
            size_hint_y=None, height=250
        )
        self.info_text.background_color = (0.15, 0.15, 0.2, 1)
        self.info_text.color = (0.9, 1, 0.9, 1)
        self.add_widget(self.info_text)
    
    def toggle_pause(self, instance):
        self.bot.data["search_active"] = not self.bot.data.get("search_active", True)
        status = "▶️ ВОЗОБНОВЛЕНО" if self.bot.data["search_active"] else "⏸️ ПАУЗА"
        self.info_text.text = f"{status}\n\nПоиск: {'активен' if self.bot.data['search_active'] else 'остановлен'}"
        asyncio.create_task(self.bot.safe_save())
    
    def switch_mode(self, instance):
        old = self.bot.mode
        self.bot.mode = "REAL" if old == "TEST" else "TEST"
        CONFIG["MODE"] = self.bot.mode
        self.btn_mode.text = f"🔄 РЕЖИМ: {self.bot.mode}"
        self.info_text.text = f"Режим: {old} → {self.bot.mode}"
        asyncio.create_task(self.bot.safe_save())
    
    def show_pnl(self, instance):
        total = self.bot.data.get("total_pnl", 0)
        daily = self.bot.data.get("daily_pnl", 0)
        test = self.bot.data.get("test_pnl", 0)
        
        self.info_text.text = (
            f"💰 PnL ОТЧЕТ\n{'='*40}\n\n"
            f"Режим: {self.bot.mode}\n\n"
            f"Всего: {total:.2f}₽\n"
            f"Сегодня: {daily:.2f}₽\n"
            f"Тест: {test:.2f}₽"
        )
    
    def show_positions(self, instance):
        active = self.bot.data["pos"] if self.bot.mode == "REAL" else self.bot.data["test_pos"]
        if not active:
            self.info_text.text = "📭 Позиций нет"
            return
        
        text = "📊 ПОЗИЦИИ\n" + "="*40 + "\n\n"
        for ticker, pos in active.items():
            text += f"{ticker} ({pos['side']}) {pos['lot']} лотов @ {pos['p']:.2f}₽\n"
        self.info_text.text = text
    
    def kill_all(self, instance):
        active = self.bot.data["pos"] if self.bot.mode == "REAL" else self.bot.data["test_pos"]
        if not active:
            self.info_text.text = "📭 Нечего закрывать"
            return
        
        self.info_text.text = "🛑 ЗАКРЫВАЮ ВСЕ ПОЗИЦИИ..."
        
        async def do_kill():
            for ticker in list(active.keys()):
                price = self.bot.price_history[ticker][-1] if self.bot.price_history[ticker] else 100.0
                await self.bot.exit_trade(ticker, price, "MANUAL KILL", 0)
            self.info_text.text = "✅ Все позиции закрыты!"
        
        asyncio.create_task(do_kill())


class TITANApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bot = None
        self.notifier = None
        self._tasks = []
    
    def build(self):
        self.title = "🚀 TITAN Trader"
        
        self.notifier = ChannelManager(CONFIG)
        self.bot = TitanAbsoluteMonolith(self.notifier)
        
        main = BoxLayout(orientation='vertical')
        
        tabs = TabbedPanel()
        tabs.do_default_tab = False
        
        control = ControlTab(self.bot)
        tab1 = TabbedPanelHeader(text="🎛️ Управление")
        tab1.content = control
        tabs.add_widget(tab1)
        
        main.add_widget(tabs)
        
        Clock.schedule_once(self.start_bot, 0.5)
        return main
    
    async def start_bot_async(self):
        await self.bot.start()
        ws_task = asyncio.create_task(ws_market_data_feed(self.bot))
        self._tasks.append(ws_task)
    
    def start_bot(self, dt):
        asyncio.create_task(self.start_bot_async())
    
    def on_stop(self):
        if self.bot:
            asyncio.create_task(self.bot.stop())
        for task in self._tasks:
            task.cancel()


if __name__ == "__main__":
    TITANApp().run()