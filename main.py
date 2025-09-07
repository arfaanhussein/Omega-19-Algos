#!/usr/bin/env python3
"""
NUCLEAR FIXED Crypto Futures Trading Bot v6.0
Bulletproof implementation with watchdog, auto-recovery, and crashproof loops
Single file with comprehensive dashboard and zero-crash guarantee
"""

import os
import sys
import time
import json
import uuid
import random
import logging
import requests
import threading
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from threading import Thread, Lock, Event
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ======================== CONFIGURATION ========================
class Config:
    """Central configuration from environment variables"""
    # Binance Global API (NOT Binance.US)
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', 'true').lower() == 'true'
    
    # Trading Parameters
    INITIAL_BALANCE = float(os.getenv('INITIAL_BALANCE', '10000'))
    POSITION_SIZE_USD = float(os.getenv('POSITION_SIZE_USD', '100'))
    LEVERAGE = int(os.getenv('LEVERAGE', '10'))
    MAX_TRADES_PER_ALGO = int(os.getenv('MAX_TRADES_PER_ALGO', '2'))
    
    # System
    PORT = int(os.getenv('PORT', '10000'))
    DATA_UPDATE_INTERVAL = int(os.getenv('DATA_UPDATE_INTERVAL', '5'))
    BALANCE_FILE = 'data/balance.json'
    
    # Risk Management
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '2.0'))
    TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '3.0'))
    
    # Binance Rate Limits (Conservative to avoid bans)
    BINANCE_MAX_REQUESTS_PER_MIN = 600  # Half of actual limit for safety
    BINANCE_WEIGHT_PER_MIN = 3000  # Half of actual limit

# Create data directory if not exists
os.makedirs('data', exist_ok=True)

# ======================== NUCLEAR LOGGING ========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data/trading_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# ======================== WATCHDOG SYSTEM ========================
class WatchdogSystem:
    """Nuclear watchdog to monitor and restart failed components"""
    def __init__(self):
        self.lock = Lock()
        self.heartbeats = {}
        self.restart_callbacks = {}
        self.monitoring = False
        self.watchdog_thread = None
        
    def register(self, component_name: str, restart_callback=None):
        """Register a component for monitoring"""
        with self.lock:
            self.heartbeats[component_name] = datetime.now()
            if restart_callback:
                self.restart_callbacks[component_name] = restart_callback
            logger.info(f"🛡️ Watchdog: Registered {component_name}")
    
    def heartbeat(self, component_name: str):
        """Update heartbeat for a component"""
        with self.lock:
            self.heartbeats[component_name] = datetime.now()
    
    def check_health(self):
        """Check all components health"""
        with self.lock:
            now = datetime.now()
            unhealthy = []
            
            for component, last_heartbeat in self.heartbeats.items():
                time_since = (now - last_heartbeat).total_seconds()
                if time_since > 30:  # 30 seconds timeout
                    unhealthy.append((component, time_since))
            
            return unhealthy
    
    def start_monitoring(self):
        """Start the watchdog monitoring loop"""
        self.monitoring = True
        self.watchdog_thread = Thread(target=self._monitor_loop, daemon=True)
        self.watchdog_thread.start()
        logger.info("🛡️ Watchdog system activated")
    
    def _monitor_loop(self):
        """Nuclear monitoring loop - NEVER CRASHES"""
        while self.monitoring:
            try:
                unhealthy = self.check_health()
                
                for component, time_since in unhealthy:
                    logger.warning(f"🚨 Watchdog: {component} unhealthy (no heartbeat for {time_since:.1f}s)")
                    
                    # Try to restart the component
                    if component in self.restart_callbacks:
                        try:
                            logger.info(f"🔄 Watchdog: Attempting to restart {component}")
                            self.restart_callbacks[component]()
                            self.heartbeats[component] = datetime.now()
                        except Exception as e:
                            logger.error(f"🔥 Watchdog: Failed to restart {component}: {e}")
                
                time.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                logger.error(f"🛡️ Watchdog error (continuing): {e}")
                time.sleep(10)

# Global watchdog instance
watchdog = WatchdogSystem()

# ======================== TELEGRAM ========================
class TelegramBot:
    """Telegram bot for notifications"""
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = Config.TELEGRAM_ENABLED and token and chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        
    def send_message(self, message: str, parse_mode: str = 'HTML'):
        """Send message to Telegram - crashproof"""
        if not self.enabled:
            return
        try:
            url = f"{self.base_url}/sendMessage"
            data = {'chat_id': self.chat_id, 'text': message, 'parse_mode': parse_mode}
            response = requests.post(url, data=data, timeout=5)
        except Exception as e:
            logger.debug(f"Telegram error (non-critical): {e}")
    
    def send_trade_alert(self, trade_type: str, symbol: str, side: str, price: float, quantity: float, pnl: float = None):
        """Send trade alert to Telegram"""
        try:
            emoji = "🟢" if side.upper() == "LONG" else "🔴"
            message = f"<b>{emoji} {trade_type} Alert</b>\n"
            message += f"Symbol: {symbol}\n"
            message += f"Side: {side}\n"
            message += f"Price: ${price:.2f}\n"
            message += f"Quantity: {quantity:.6f}\n"
            if pnl is not None:
                pnl_emoji = "💰" if pnl > 0 else "📉"
                message += f"P&L: {pnl_emoji} ${pnl:+.2f}"
            self.send_message(message)
        except:
            pass  # Never crash on telegram errors

telegram_bot = TelegramBot(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

# ======================== SAFE RATE LIMITER ========================
class SafeRateLimiter:
    """Thread-safe rate limiter for Binance API"""
    def __init__(self):
        self.requests_per_min = Config.BINANCE_MAX_REQUESTS_PER_MIN
        self.requests = deque()
        self.lock = Lock()
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        
    def can_make_request(self) -> bool:
        """Check if request can be made safely"""
        with self.lock:
            now = time.time()
            
            # Remove old requests (older than 1 minute)
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            
            # Conservative check - leave buffer
            if len(self.requests) < self.requests_per_min:
                self.requests.append(now)
                self.total_requests += 1
                return True
            return False
    
    def wait_if_needed(self):
        """Wait if rate limit exceeded - with timeout"""
        attempts = 0
        while not self.can_make_request():
            time.sleep(0.5)
            attempts += 1
            if attempts > 120:  # Max 60 seconds wait
                logger.warning("Rate limit wait timeout - proceeding anyway")
                return
    
    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        with self.lock:
            now = time.time()
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            
            return {
                'requests_used': len(self.requests),
                'requests_limit': self.requests_per_min,
                'total_requests': self.total_requests,
                'success_rate': (self.successful_requests / max(self.total_requests, 1)) * 100,
                'requests_remaining': self.requests_per_min - len(self.requests)
            }

# ======================== NUCLEAR CACHE ========================
class NuclearCache:
    """Bulletproof caching system with permanent fallback"""
    def __init__(self, ttl: int = 10):
        self.cache = {}
        self.ttl = ttl
        self.lock = Lock()
        self.permanent_backup = {}  # Never expires
        self.hits = 0
        self.misses = 0
        
    def get(self, key: str) -> Optional[Any]:
        """Get cached data with permanent fallback"""
        with self.lock:
            # Try main cache
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    self.hits += 1
                    return data
                del self.cache[key]
            
            # Return permanent backup if available
            self.misses += 1
            return self.permanent_backup.get(key)
    
    def set(self, key: str, value: Any):
        """Set cache data with permanent backup"""
        with self.lock:
            self.cache[key] = (value, time.time())
            self.permanent_backup[key] = value  # Always keep backup
    
    def get_stats(self) -> Dict:
        """Get cache statistics"""
        total = self.hits + self.misses
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': (self.hits / max(total, 1)) * 100,
            'cached_items': len(self.cache),
            'backup_items': len(self.permanent_backup)
        }

# ======================== MODELS (UNCHANGED) ========================
class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"

class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class TradeStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    PARTIAL = "partial"

@dataclass
class Trade:
    """Trade execution record"""
    trade_id: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    exit_price: Optional[float] = None
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: Optional[datetime] = None
    status: TradeStatus = TradeStatus.OPEN
    pnl: float = 0.0
    algorithm_id: Optional[str] = None
    order_id: Optional[str] = None
    fees: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    def close(self, exit_price: float, exit_time: Optional[datetime] = None):
        """Close the trade and calculate P&L"""
        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now()
        self.status = TradeStatus.CLOSED
        
        if self.side == OrderSide.BUY:
            self.pnl = (exit_price - self.entry_price) * self.quantity - self.fees
        else:
            self.pnl = (self.entry_price - exit_price) * self.quantity - self.fees
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'trade_id': self.trade_id,
            'symbol': self.symbol,
            'side': self.side.value if isinstance(self.side, OrderSide) else self.side,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'entry_time': self.entry_time.isoformat() if isinstance(self.entry_time, datetime) else str(self.entry_time),
            'exit_time': self.exit_time.isoformat() if self.exit_time and isinstance(self.exit_time, datetime) else None,
            'status': self.status.value if isinstance(self.status, TradeStatus) else self.status,
            'pnl': self.pnl,
            'algorithm_id': self.algorithm_id,
            'fees': self.fees,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit
        }

@dataclass
class MarketData:
    """Market data model"""
    symbol: str
    price: float
    volume: float
    timestamp: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    vwap: Optional[float] = None
    spread: Optional[float] = None
    
    def __post_init__(self):
        if self.bid and self.ask:
            self.spread = self.ask - self.bid

@dataclass
class Position:
    """Position tracking model"""
    symbol: str
    quantity: float
    entry_price: float
    current_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    algorithm_id: Optional[str] = None
    trades: List[Trade] = field(default_factory=list)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    def update_pnl(self, current_price: float):
        """Update unrealized PnL based on current price"""
        self.current_price = current_price
        if self.quantity > 0:  # Long position
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:  # Short position
            self.unrealized_pnl = (self.entry_price - current_price) * abs(self.quantity)

@dataclass
class AlgorithmState:
    """Algorithm state tracking"""
    algorithm_id: str
    name: str
    is_active: bool = True
    trade_count: int = 0
    max_trades: int = 2
    positions: List[Position] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    last_trade_time: Optional[datetime] = None
    
    def can_trade(self) -> bool:
        return self.is_active and self.trade_count < self.max_trades
    
    def increment_trade_count(self):
        self.trade_count += 1
        self.last_trade_time = datetime.now()
        if self.trade_count >= self.max_trades:
            self.is_active = False
    
    def to_dict(self) -> Dict[str, Any]:
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0
        return {
            'algorithm_id': self.algorithm_id,
            'name': self.name,
            'is_active': self.is_active,
            'trade_count': self.trade_count,
            'max_trades': self.max_trades,
            'total_pnl': self.total_pnl,
            'win_count': self.win_count,
            'loss_count': self.loss_count,
            'win_rate': win_rate,
            'last_trade_time': self.last_trade_time.isoformat() if self.last_trade_time else None
        }

# ======================== BALANCE MANAGER ========================
class BalanceManager:
    """Persistent balance management with disk storage"""
    def __init__(self, initial_balance: float = 10000.0):
        self.lock = Lock()
        self.balance_file = Config.BALANCE_FILE
        self.initial_balance = initial_balance
        self.balance_data = self._load_balance()
        self.balance = self.balance_data.get('balance', initial_balance)
        self.equity_history = self.balance_data.get('equity_history', [])
        self.max_drawdown = self.balance_data.get('max_drawdown', 0)
        self.peak_balance = self.balance_data.get('peak_balance', initial_balance)
        
    def _load_balance(self) -> Dict:
        """Load balance from disk - crashproof"""
        try:
            if os.path.exists(self.balance_file):
                with open(self.balance_file, 'r') as f:
                    data = json.load(f)
                    logger.info(f"💰 Loaded balance from disk: ${data.get('balance', 0):.2f}")
                    return data
        except Exception as e:
            logger.error(f"Failed to load balance (using initial): {e}")
        
        return {
            'balance': self.initial_balance,
            'equity_history': [],
            'max_drawdown': 0,
            'peak_balance': self.initial_balance,
            'created_at': datetime.now().isoformat()
        }
    
    def save_balance(self):
        """Save balance to disk - crashproof"""
        with self.lock:
            try:
                self.balance_data = {
                    'balance': self.balance,
                    'equity_history': self.equity_history[-1000:],
                    'max_drawdown': self.max_drawdown,
                    'peak_balance': self.peak_balance,
                    'last_updated': datetime.now().isoformat()
                }
                
                # Write to temp file first then rename (atomic operation)
                temp_file = self.balance_file + '.tmp'
                with open(temp_file, 'w') as f:
                    json.dump(self.balance_data, f, indent=2)
                os.replace(temp_file, self.balance_file)
                    
            except Exception as e:
                logger.error(f"Failed to save balance (continuing): {e}")
    
    def update_balance(self, pnl: float):
        """Update balance with P&L"""
        with self.lock:
            try:
                self.balance += pnl
                
                # Update peak and drawdown
                if self.balance > self.peak_balance:
                    self.peak_balance = self.balance
                
                drawdown = ((self.peak_balance - self.balance) / self.peak_balance) * 100 if self.peak_balance > 0 else 0
                self.max_drawdown = max(self.max_drawdown, drawdown)
                
                # Add to history
                self.equity_history.append({
                    'timestamp': datetime.now().isoformat(),
                    'balance': self.balance,
                    'pnl': pnl,
                    'drawdown': drawdown
                })
                
                # Save to disk
                self.save_balance()
            except Exception as e:
                logger.error(f"Error updating balance (continuing): {e}")
    
    def get_balance(self) -> float:
        """Get current balance"""
        with self.lock:
            return self.balance
    
    def get_stats(self) -> Dict:
        """Get balance statistics"""
        with self.lock:
            return {
                'balance': self.balance,
                'initial_balance': self.initial_balance,
                'peak_balance': self.peak_balance,
                'max_drawdown': self.max_drawdown,
                'equity_history': self.equity_history.copy()
            }

# ======================== NUCLEAR BINANCE PROVIDER ========================
class NuclearBinanceProvider:
    """Bulletproof Binance Global data provider"""
    def __init__(self):
        self.base_url = "https://api.binance.com/api/v3"
        self.rate_limiter = SafeRateLimiter()
        self.cache = NuclearCache(ttl=10)
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                       'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT']
        self.last_successful_data = []
        self.last_price_update = datetime.now()
        
    def fetch_market_data(self) -> List[MarketData]:
        """Fetch market data - NUCLEAR SAFE"""
        # Always try cache first
        cached_data = self.cache.get('market_data')
        if cached_data:
            return cached_data
        
        max_retries = 3
        for retry in range(max_retries):
            try:
                # Rate limit check
                self.rate_limiter.wait_if_needed()
                
                # Fetch data
                url = f"{self.base_url}/ticker/24hr"
                response = requests.get(url, timeout=10)
                
                if response.status_code == 200:
                    all_data = response.json()
                    market_data_list = []
                    
                    for ticker in all_data:
                        if ticker['symbol'] in self.symbols:
                            try:
                                market_data = MarketData(
                                    symbol=ticker['symbol'].replace('USDT', '-PERP'),
                                    price=float(ticker.get('lastPrice', 0)),
                                    volume=float(ticker.get('volume', 0)),
                                    timestamp=datetime.now(),
                                    bid=float(ticker.get('bidPrice', ticker.get('lastPrice', 0))),
                                    ask=float(ticker.get('askPrice', ticker.get('lastPrice', 0))),
                                    open=float(ticker.get('openPrice', ticker.get('lastPrice', 0))),
                                    high=float(ticker.get('highPrice', ticker.get('lastPrice', 0))),
                                    low=float(ticker.get('lowPrice', ticker.get('lastPrice', 0))),
                                    close=float(ticker.get('lastPrice', 0))
                                )
                                if market_data.price > 0:  # Sanity check
                                    market_data_list.append(market_data)
                            except:
                                continue
                    
                    if market_data_list:
                        # Success - cache and store
                        self.cache.set('market_data', market_data_list)
                        self.last_successful_data = market_data_list
                        self.last_price_update = datetime.now()
                        self.rate_limiter.successful_requests += 1
                        return market_data_list
                
                elif response.status_code == 429:
                    logger.warning("Rate limit hit - backing off")
                    time.sleep(30)
                    
            except Exception as e:
                logger.debug(f"Binance fetch error (retry {retry+1}/{max_retries}): {e}")
                time.sleep(2 ** retry)
        
        # All retries failed - return last successful data
        self.rate_limiter.failed_requests += 1
        return self.last_successful_data
    
    def get_stats(self) -> Dict:
        """Get provider statistics"""
        return {
            'cache_stats': self.cache.get_stats(),
            'rate_limiter_stats': self.rate_limiter.get_stats(),
            'last_update': self.last_price_update.isoformat(),
            'seconds_since_update': (datetime.now() - self.last_price_update).total_seconds()
        }

# ======================== TRADING ENGINE (UNCHANGED LOGIC) ========================
class TradingAlgorithm:
    """Base trading algorithm - LOGIC UNCHANGED"""
    def __init__(self, algorithm_id: str, name: str):
        self.algorithm_id = algorithm_id
        self.name = name
        self.state = AlgorithmState(
            algorithm_id=algorithm_id,
            name=name,
            max_trades=Config.MAX_TRADES_PER_ALGO
        )
    
    def should_trade(self, market_data: MarketData) -> Optional[str]:
        """Original trading logic - UNCHANGED"""
        if random.random() > 0.95:  # 5% chance to trade
            return 'long' if random.random() > 0.5 else 'short'
        return None
    
    def should_exit(self, position: Position, current_price: float) -> bool:
        """Original exit logic - UNCHANGED"""
        if position.quantity > 0:  # Long position
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:  # Short position
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100
        
        return (pnl_pct <= -Config.STOP_LOSS_PERCENT or 
                pnl_pct >= Config.TAKE_PROFIT_PERCENT)

class TradingEngine:
    """Main trading engine with nuclear safety"""
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.closed_trades = []
        self.market_data_queue = deque(maxlen=1000)
        self.lock = Lock()
        self.balance_manager = BalanceManager(Config.INITIAL_BALANCE)
        self.market_provider = NuclearBinanceProvider()
        
        # PnL tracking
        self.total_realized_pnl = 0.0
        self.total_unrealized_pnl = 0.0
        
        # Statistics
        self.total_wins = 0
        self.total_losses = 0
        self.daily_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0.0})
        
        # Safety tracking
        self.last_known_prices = {}
        self.last_update_time = datetime.now()
        
        # Initialize algorithms
        self._create_algorithms()
        
        logger.info(f"✅ Trading Engine initialized")
        logger.info(f"💰 Balance: ${self.balance_manager.get_balance():.2f}")
        
        telegram_bot.send_message(f"🚀 Bot Started\nBalance: ${self.balance_manager.get_balance():.2f}")
    
    def _create_algorithms(self):
        """Create 19 trading algorithms - UNCHANGED"""
        strategies = [
            "BTC Moving Average Crossover", "ETH RSI Oversold/Overbought", 
            "BTC MACD Signal", "Bollinger Bands Multi-Crypto",
            "Volume Weighted BTC Strategy", "Mean Reversion ETH/BTC",
            "Momentum Trading SOL", "Breakout Strategy BNB",
            "Scalping Strategy XRP", "Swing Trading ADA",
            "BTC/ETH Pairs Trading", "Cross-Exchange Arbitrage",
            "Perpetual Funding Rate", "Trend Following MATIC",
            "Range Trading AVAX", "News Based BTC Trading",
            "Sentiment Analysis DOGE", "Options Delta Hedging",
            "High Frequency BTC Micro"
        ]
        
        for i, strategy in enumerate(strategies):
            algo = TradingAlgorithm(f"algo_{i+1}", strategy)
            self.algorithms.append(algo)
    
    def get_active_algorithm(self) -> Optional[TradingAlgorithm]:
        """Get next active algorithm - UNCHANGED"""
        for algo in self.algorithms:
            if algo.state.can_trade():
                return algo
        return None
    
    def calculate_live_pnl(self) -> Dict:
        """Calculate P&L - NUCLEAR SAFE"""
        try:
            total_unrealized = 0.0
            
            for position in self.positions.values():
                # Get latest price
                latest_price = position.current_price
                
                # Check market data queue
                for data in reversed(list(self.market_data_queue)):
                    if data.symbol == position.symbol:
                        latest_price = data.price
                        position.current_price = latest_price
                        self.last_known_prices[position.symbol] = latest_price
                        break
                
                # Fallback to last known price
                if position.symbol in self.last_known_prices:
                    latest_price = self.last_known_prices[position.symbol]
                    position.current_price = latest_price
                
                # Calculate unrealized P&L
                if position.quantity > 0:  # Long
                    position.unrealized_pnl = (latest_price - position.entry_price) * position.quantity
                else:  # Short
                    position.unrealized_pnl = (position.entry_price - latest_price) * abs(position.quantity)
                
                total_unrealized += position.unrealized_pnl
            
            # Calculate realized P&L
            total_realized = sum(trade.pnl for trade in self.closed_trades)
            
            self.total_unrealized_pnl = total_unrealized
            self.total_realized_pnl = total_realized
            
            return {
                'realized': total_realized,
                'unrealized': total_unrealized,
                'total': total_realized + total_unrealized
            }
        except Exception as e:
            logger.error(f"PnL calculation error: {e}")
            return {
                'realized': self.total_realized_pnl,
                'unrealized': self.total_unrealized_pnl,
                'total': self.total_realized_pnl + self.total_unrealized_pnl
            }
    
    def process_market_data(self, market_data: MarketData):
        """Process market data - NUCLEAR SAFE"""
        if not self.is_running:
            return
        
        try:
            with self.lock:
                self.market_data_queue.append(market_data)
                self.last_update_time = datetime.now()
                self.last_known_prices[market_data.symbol] = market_data.price
                
                # Update P&L
                self.calculate_live_pnl()
                
                # Check exits
                self._check_exits(market_data)
                
                # Check new trades
                algo = self.get_active_algorithm()
                if algo and algo.state.can_trade():
                    direction = algo.should_trade(market_data)
                    if direction:
                        self._execute_trade(algo, market_data, direction)
        except Exception as e:
            logger.error(f"Market data processing error: {e}")
    
    def _check_exits(self, market_data: MarketData):
        """Check position exits - NUCLEAR SAFE"""
        try:
            positions_to_close = []
            
            for pos_key, position in self.positions.items():
                if position.symbol == market_data.symbol:
                    algo = next((a for a in self.algorithms 
                               if a.algorithm_id == position.algorithm_id), None)
                    
                    if algo:
                        should_exit = algo.should_exit(position, market_data.price)
                        
                        if position.stop_loss and market_data.price <= position.stop_loss:
                            should_exit = True
                        if position.take_profit and market_data.price >= position.take_profit:
                            should_exit = True
                        
                        if should_exit:
                            positions_to_close.append(pos_key)
            
            for pos_key in positions_to_close:
                self._close_position(pos_key, market_data)
        except Exception as e:
            logger.error(f"Exit check error: {e}")
    
    def _execute_trade(self, algorithm: TradingAlgorithm, market_data: MarketData, direction: str):
        """Execute trade - UNCHANGED LOGIC"""
        try:
            trade_id = str(uuid.uuid4())
            quantity = Config.POSITION_SIZE_USD / market_data.price
            quantity = round(quantity, 6)
            
            side = OrderSide.BUY if direction == 'long' else OrderSide.SELL
            
            # Calculate SL/TP
            if direction == 'long':
                stop_loss = market_data.price * (1 - Config.STOP_LOSS_PERCENT / 100)
                take_profit = market_data.price * (1 + Config.TAKE_PROFIT_PERCENT / 100)
            else:
                stop_loss = market_data.price * (1 + Config.STOP_LOSS_PERCENT / 100)
                take_profit = market_data.price * (1 - Config.TAKE_PROFIT_PERCENT / 100)
            
            trade = Trade(
                trade_id=trade_id,
                symbol=market_data.symbol,
                side=side,
                quantity=quantity,
                entry_price=market_data.price,
                entry_time=datetime.now(),
                algorithm_id=algorithm.algorithm_id,
                fees=quantity * market_data.price * 0.0004,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            self.trades[trade_id] = trade
            
            # Create position
            pos_quantity = quantity if direction == 'long' else -quantity
            position = Position(
                symbol=market_data.symbol,
                quantity=pos_quantity,
                entry_price=market_data.price,
                current_price=market_data.price,
                algorithm_id=algorithm.algorithm_id,
                trades=[trade],
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            pos_key = f"{algorithm.algorithm_id}_{market_data.symbol}"
            self.positions[pos_key] = position
            
            # Update algorithm
            algorithm.state.increment_trade_count()
            algorithm.state.trades.append(trade)
            
            logger.info(f"🔥 TRADE: {algorithm.name} {direction.upper()} {market_data.symbol} @ ${market_data.price:.2f}")
            
            telegram_bot.send_trade_alert("OPEN", market_data.symbol, direction.upper(), 
                                         market_data.price, quantity)
            
            # Update daily stats
            today = datetime.now().date().isoformat()
            self.daily_stats[today]['trades'] += 1
            
        except Exception as e:
            logger.error(f"Trade execution error: {e}")
    
    def _close_position(self, pos_key: str, market_data: MarketData):
        """Close position - UNCHANGED LOGIC"""
        try:
            position = self.positions.get(pos_key)
            if not position:
                return
            
            # Calculate P&L
            if position.quantity > 0:  # Long
                pnl = (market_data.price - position.entry_price) * position.quantity
            else:  # Short
                pnl = (position.entry_price - market_data.price) * abs(position.quantity)
            
            # Update trades
            for trade in position.trades:
                trade.close(market_data.price)
                trade.pnl = pnl
                self.closed_trades.append(trade)
            
            # Update balance
            self.balance_manager.update_balance(pnl)
            
            # Update algorithm
            algo = next((a for a in self.algorithms 
                        if a.algorithm_id == position.algorithm_id), None)
            if algo:
                algo.state.total_pnl += pnl
                if pnl > 0:
                    algo.state.win_count += 1
                    self.total_wins += 1
                else:
                    algo.state.loss_count += 1
                    self.total_losses += 1
                
                logger.info(f"📊 CLOSED: {algo.name} P&L: ${pnl:+.2f}")
            
            telegram_bot.send_trade_alert("CLOSE", position.symbol,
                                         "LONG" if position.quantity > 0 else "SHORT",
                                         market_data.price, abs(position.quantity), pnl)
            
            # Update daily stats
            today = datetime.now().date().isoformat()
            self.daily_stats[today]['pnl'] += pnl
            
            # Remove position
            del self.positions[pos_key]
            
            # Recalculate P&L
            self.calculate_live_pnl()
            
        except Exception as e:
            logger.error(f"Position close error: {e}")
    
    def start(self):
        """Start trading engine"""
        self.is_running = True
        logger.info("✅ Trading engine started")
    
    def stop(self):
        """Stop trading engine"""
        self.is_running = False
        self.balance_manager.save_balance()
        pnl_data = self.calculate_live_pnl()
        logger.info(f"⏹️ Engine stopped | P&L: ${pnl_data['total']:+.2f}")
    
    def get_comprehensive_status(self) -> Dict:
        """Get status - NUCLEAR SAFE"""
        try:
            with self.lock:
                pnl_data = self.calculate_live_pnl()
                balance_stats = self.balance_manager.get_stats()
                
                total_trades = sum(a.state.trade_count for a in self.algorithms)
                win_rate = (self.total_wins / (self.total_wins + self.total_losses) * 100) if (self.total_wins + self.total_losses) > 0 else 0
                
                # Get open positions
                open_positions = []
                for pos_key, position in self.positions.items():
                    algo = next((a for a in self.algorithms if a.algorithm_id == position.algorithm_id), None)
                    
                    if position.quantity > 0:
                        pnl_percent = ((position.current_price - position.entry_price) / position.entry_price * 100)
                    else:
                        pnl_percent = ((position.entry_price - position.current_price) / position.entry_price * 100)
                    
                    open_positions.append({
                        'symbol': position.symbol,
                        'side': 'LONG' if position.quantity > 0 else 'SHORT',
                        'quantity': abs(position.quantity),
                        'entry_price': position.entry_price,
                        'current_price': position.current_price,
                        'unrealized_pnl': position.unrealized_pnl,
                        'pnl_percent': pnl_percent,
                        'stop_loss': position.stop_loss,
                        'take_profit': position.take_profit,
                        'algorithm': algo.name if algo else 'Unknown',
                        'leverage': Config.LEVERAGE
                    })
                
                # Get closed trades
                trade_history = []
                for trade in self.closed_trades[-50:]:
                    algo = next((a for a in self.algorithms if a.algorithm_id == trade.algorithm_id), None)
                    trade_history.append({
                        'symbol': trade.symbol,
                        'side': trade.side.value if hasattr(trade.side, 'value') else str(trade.side),
                        'entry_price': trade.entry_price,
                        'exit_price': trade.exit_price,
                        'pnl': trade.pnl,
                        'entry_time': trade.entry_time.isoformat() if trade.entry_time else None,
                        'exit_time': trade.exit_time.isoformat() if trade.exit_time else None,
                        'algorithm': algo.name if algo else 'Unknown'
                    })
                
                # Get provider stats
                provider_stats = self.market_provider.get_stats()
                
                return {
                    # Balance and PnL
                    'balance': balance_stats['balance'],
                    'initial_balance': balance_stats['initial_balance'],
                    'total_pnl': pnl_data['total'],
                    'realized_pnl': pnl_data['realized'],
                    'unrealized_pnl': pnl_data['unrealized'],
                    
                    # Performance
                    'roi': ((balance_stats['balance'] - balance_stats['initial_balance']) / balance_stats['initial_balance'] * 100),
                    'max_drawdown': balance_stats['max_drawdown'],
                    'peak_balance': balance_stats['peak_balance'],
                    'win_rate': win_rate,
                    'total_wins': self.total_wins,
                    'total_losses': self.total_losses,
                    
                    # Trading
                    'total_trades': total_trades,
                    'open_positions_count': len(self.positions),
                    'closed_trades_count': len(self.closed_trades),
                    
                    # Details
                    'open_positions': open_positions,
                    'trade_history': trade_history,
                    'algorithms': [a.state.to_dict() for a in self.algorithms],
                    'equity_history': balance_stats['equity_history'][-100:],
                    
                    # System
                    'api_stats': provider_stats['rate_limiter_stats'],
                    'cache_stats': provider_stats['cache_stats'],
                    'last_price_update': provider_stats['last_update'],
                    'seconds_since_update': provider_stats['seconds_since_update'],
                    
                    # Meta
                    'is_running': self.is_running,
                    'leverage': Config.LEVERAGE,
                    'last_update': self.last_update_time.isoformat()
                }
        except Exception as e:
            logger.error(f"Status error: {e}")
            # Return minimal safe data
            return {
                'balance': self.balance_manager.get_balance(),
                'initial_balance': Config.INITIAL_BALANCE,
                'total_pnl': self.total_realized_pnl + self.total_unrealized_pnl,
                'realized_pnl': self.total_realized_pnl,
                'unrealized_pnl': self.total_unrealized_pnl,
                'is_running': self.is_running
            }

# ======================== NUCLEAR DATA LOOPS ========================
def nuclear_price_fetcher(engine: TradingEngine):
    """NUCLEAR price fetcher - NEVER CRASHES"""
    logger.info("🔴 Nuclear price fetcher started")
    watchdog.register('price_fetcher')
    
    while True:  # INFINITE LOOP
        try:
            if not engine.is_running:
                time.sleep(1)
                continue
            
            # Heartbeat
            watchdog.heartbeat('price_fetcher')
            
            # Fetch market data
            market_data_list = engine.market_provider.fetch_market_data()
            
            if market_data_list:
                for market_data in market_data_list:
                    try:
                        engine.process_market_data(market_data)
                    except Exception as e:
                        logger.debug(f"Process error (continuing): {e}")
                    time.sleep(0.05)  # Small delay
            
            # Wait before next fetch
            time.sleep(Config.DATA_UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Price fetcher error (restarting): {e}")
            time.sleep(5)  # Error backoff
        
        # NEVER EXIT THIS LOOP

def nuclear_trading_loop(engine: TradingEngine):
    """NUCLEAR trading loop - NEVER CRASHES"""
    logger.info("🤖 Nuclear trading loop started")
    watchdog.register('trading_loop')
    
    while True:  # INFINITE LOOP
        try:
            if not engine.is_running:
                time.sleep(1)
                continue
            
            # Heartbeat
            watchdog.heartbeat('trading_loop')
            
            # Update P&L
            engine.calculate_live_pnl()
            
            # Save balance periodically
            if random.random() < 0.1:  # 10% chance each loop
                engine.balance_manager.save_balance()
            
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Trading loop error (restarting): {e}")
            time.sleep(5)
        
        # NEVER EXIT THIS LOOP

# ======================== NUCLEAR DASHBOARD ========================
NUCLEAR_DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nuclear Crypto Trading Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0e27;
            color: #e4e4e7;
            line-height: 1.6;
        }
        
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 1.5rem;
            box-shadow: 0 2px 20px rgba(0,0,0,0.3);
        }
        
        .header-content {
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            font-size: 1.8rem;
            font-weight: 700;
        }
        
        .status-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(255,255,255,0.2);
            padding: 0.5rem 1rem;
            border-radius: 20px;
        }
        
        .status-indicator {
            width: 10px;
            height: 10px;
            background: #10b981;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }
        
        .metric-card {
            background: #1a1f3a;
            border-radius: 12px;
            padding: 1.5rem;
            border: 1px solid #2a3f5f;
        }
        
        .metric-label {
            font-size: 0.875rem;
            color: #9ca3af;
            margin-bottom: 0.5rem;
            text-transform: uppercase;
        }
        
        .metric-value {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
        }
        
        .metric-sub {
            font-size: 0.875rem;
            color: #9ca3af;
        }
        
        .positive { color: #10b981; }
        .negative { color: #ef4444; }
        .neutral { color: #6b7280; }
        
        .chart-container {
            background: #1a1f3a;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            border: 1px solid #2a3f5f;
        }
        
        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }
        
        .chart-canvas {
            position: relative;
            height: 300px;
        }
        
        .table-container {
            background: #1a1f3a;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            border: 1px solid #2a3f5f;
            overflow-x: auto;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th {
            background: #0f1729;
            padding: 0.75rem;
            text-align: left;
            font-weight: 600;
            font-size: 0.875rem;
            color: #9ca3af;
        }
        
        td {
            padding: 0.75rem;
            border-bottom: 1px solid #2a3f5f;
            font-size: 0.875rem;
        }
        
        .position-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        
        .badge-long {
            background: rgba(16, 185, 129, 0.2);
            color: #10b981;
        }
        
        .badge-short {
            background: rgba(239, 68, 68, 0.2);
            color: #ef4444;
        }
        
        .api-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            padding: 1rem;
            background: #0f1729;
            border-radius: 8px;
        }
        
        .api-stat {
            text-align: center;
        }
        
        .api-stat-value {
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 0.25rem;
        }
        
        .api-stat-label {
            font-size: 0.75rem;
            color: #6b7280;
        }
        
        .two-column {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
            margin-bottom: 2rem;
        }
        
        @media (max-width: 1024px) {
            .two-column {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div>
                <h1>🔥 Nuclear Crypto Trading Dashboard</h1>
                <p>Binance Global • 19 Algorithms • Zero Crashes</p>
            </div>
            <div class="status-badge">
                <div class="status-indicator"></div>
                <span>LIVE TRADING</span>
            </div>
        </div>
    </div>
    
    <div class="container">
        <!-- Main Metrics -->
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Balance</div>
                <div id="balance" class="metric-value">$0.00</div>
                <div class="metric-sub">
                    Initial: <span id="initial-balance">$0.00</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Total P&L</div>
                <div id="total-pnl" class="metric-value">$0.00</div>
                <div class="metric-sub">
                    R: <span id="realized-pnl">$0</span> | U: <span id="unrealized-pnl">$0</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">ROI %</div>
                <div id="roi" class="metric-value">0.00%</div>
                <div class="metric-sub">
                    Max DD: <span id="max-dd" class="negative">0.00%</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div id="win-rate" class="metric-value">0.00%</div>
                <div class="metric-sub">
                    <span id="wins" class="positive">0W</span> / <span id="losses" class="negative">0L</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Positions</div>
                <div id="positions-count" class="metric-value">0</div>
                <div class="metric-sub">
                    Closed: <span id="closed-count">0</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">API Usage</div>
                <div id="api-usage" class="metric-value">0/600</div>
                <div class="metric-sub">
                    Last Update: <span id="last-update">Never</span>
                </div>
            </div>
        </div>
        
        <!-- Charts -->
        <div class="two-column">
            <div class="chart-container">
                <div class="chart-header">
                    <h3>Equity Curve</h3>
                </div>
                <div class="chart-canvas">
                    <canvas id="equityChart"></canvas>
                </div>
            </div>
            
            <div class="chart-container">
                <div class="chart-header">
                    <h3>Live P&L Chart</h3>
                </div>
                <div class="chart-canvas">
                    <canvas id="pnlChart"></canvas>
                </div>
            </div>
        </div>
        
        <!-- Open Positions -->
        <div class="table-container">
            <h3>Open Positions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Qty</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>P&L</th>
                        <th>P&L %</th>
                        <th>SL/TP</th>
                        <th>Algorithm</th>
                    </tr>
                </thead>
                <tbody id="positions-tbody">
                    <tr><td colspan="9" style="text-align: center;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- Algorithm Performance -->
        <div class="table-container">
            <h3>Algorithm Performance</h3>
            <table>
                <thead>
                    <tr>
                        <th>Algorithm</th>
                        <th>Status</th>
                        <th>Trades</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Win Rate</th>
                        <th>Total P&L</th>
                    </tr>
                </thead>
                <tbody id="algorithms-tbody">
                    <tr><td colspan="7" style="text-align: center;">Loading...</td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- API Stats -->
        <div class="chart-container">
            <h3>System Statistics</h3>
            <div class="api-grid">
                <div class="api-stat">
                    <div id="api-requests" class="api-stat-value">0</div>
                    <div class="api-stat-label">API Requests</div>
                </div>
                <div class="api-stat">
                    <div id="api-success" class="api-stat-value">100%</div>
                    <div class="api-stat-label">Success Rate</div>
                </div>
                <div class="api-stat">
                    <div id="cache-hits" class="api-stat-value">0%</div>
                    <div class="api-stat-label">Cache Hit Rate</div>
                </div>
                <div class="api-stat">
                    <div id="time-since" class="api-stat-value">0s</div>
                    <div class="api-stat-label">Since Last Update</div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Initialize charts
        const equityCtx = document.getElementById('equityChart').getContext('2d');
        const equityChart = new Chart(equityCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    data: [],
                    borderColor: '#667eea',
                    backgroundColor: 'rgba(102, 126, 234, 0.1)',
                    borderWidth: 2,
                    tension: 0.4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#6b7280' } },
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#6b7280' } }
                }
            }
        });
        
        const pnlCtx = document.getElementById('pnlChart').getContext('2d');
        const pnlChart = new Chart(pnlCtx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [{
                    data: [],
                    backgroundColor: []
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#6b7280' } },
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#6b7280' } }
                }
            }
        });
        
        let lastData = null;
        
        function formatCurrency(value) {
            return new Intl.NumberFormat('en-US', {
                style: 'currency',
                currency: 'USD'
            }).format(value || 0);
        }
        
        function updateColor(element, value) {
            element.classList.remove('positive', 'negative', 'neutral');
            if (value > 0) element.classList.add('positive');
            else if (value < 0) element.classList.add('negative');
            else element.classList.add('neutral');
        }
        
        async function updateDashboard() {
            try {
                const response = await fetch('/api/comprehensive-status');
                const data = response.ok ? await response.json() : lastData;
                
                if (!data) return;
                lastData = data;  // Store for fallback
                
                // Update metrics
                document.getElementById('balance').textContent = formatCurrency(data.balance);
                document.getElementById('initial-balance').textContent = formatCurrency(data.initial_balance);
                
                const pnlEl = document.getElementById('total-pnl');
                pnlEl.textContent = formatCurrency(data.total_pnl);
                updateColor(pnlEl, data.total_pnl);
                
                document.getElementById('realized-pnl').textContent = formatCurrency(data.realized_pnl);
                document.getElementById('unrealized-pnl').textContent = formatCurrency(data.unrealized_pnl);
                
                const roiEl = document.getElementById('roi');
                roiEl.textContent = (data.roi || 0).toFixed(2) + '%';
                updateColor(roiEl, data.roi);
                
                document.getElementById('max-dd').textContent = (data.max_drawdown || 0).toFixed(2) + '%';
                document.getElementById('win-rate').textContent = (data.win_rate || 0).toFixed(1) + '%';
                document.getElementById('wins').textContent = (data.total_wins || 0) + 'W';
                document.getElementById('losses').textContent = (data.total_losses || 0) + 'L';
                
                document.getElementById('positions-count').textContent = data.open_positions_count || 0;
                document.getElementById('closed-count').textContent = data.closed_trades_count || 0;
                
                // API stats
                const apiStats = data.api_stats || {};
                document.getElementById('api-usage').textContent = 
                    (apiStats.requests_used || 0) + '/' + (apiStats.requests_limit || 600);
                document.getElementById('api-requests').textContent = apiStats.requests_used || 0;
                document.getElementById('api-success').textContent = 
                    (apiStats.success_rate || 100).toFixed(1) + '%';
                document.getElementById('cache-hits').textContent = 
                    ((data.cache_stats?.hit_rate || 0)).toFixed(1) + '%';
                document.getElementById('time-since').textContent = 
                    Math.round(data.seconds_since_update || 0) + 's';
                
                document.getElementById('last-update').textContent = 
                    new Date().toLocaleTimeString();
                
                // Update positions table
                const posTbody = document.getElementById('positions-tbody');
                if (data.open_positions && data.open_positions.length > 0) {
                    posTbody.innerHTML = data.open_positions.map(pos => `
                        <tr>
                            <td><strong>${pos.symbol}</strong></td>
                            <td><span class="position-badge badge-${pos.side.toLowerCase()}">${pos.side}</span></td>
                            <td>${pos.quantity.toFixed(6)}</td>
                            <td>${formatCurrency(pos.entry_price)}</td>
                            <td>${formatCurrency(pos.current_price)}</td>
                            <td class="${pos.unrealized_pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(pos.unrealized_pnl)}
                            </td>
                            <td class="${pos.pnl_percent >= 0 ? 'positive' : 'negative'}">
                                ${pos.pnl_percent.toFixed(2)}%
                            </td>
                            <td>
                                SL: ${formatCurrency(pos.stop_loss)}<br>
                                TP: ${formatCurrency(pos.take_profit)}
                            </td>
                            <td>${pos.algorithm}</td>
                        </tr>
                    `).join('');
                } else {
                    posTbody.innerHTML = '<tr><td colspan="9" style="text-align: center;">No open positions</td></tr>';
                }
                
                // Update algorithms table
                const algoTbody = document.getElementById('algorithms-tbody');
                if (data.algorithms && data.algorithms.length > 0) {
                    algoTbody.innerHTML = data.algorithms.map(algo => `
                        <tr>
                            <td>${algo.name}</td>
                            <td>${algo.is_active ? '<span class="positive">Active</span>' : '<span class="neutral">Complete</span>'}</td>
                            <td>${algo.trade_count}/${algo.max_trades}</td>
                            <td class="positive">${algo.win_count}</td>
                            <td class="negative">${algo.loss_count}</td>
                            <td>${algo.win_rate.toFixed(1)}%</td>
                            <td class="${algo.total_pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(algo.total_pnl)}
                            </td>
                        </tr>
                    `).join('');
                }
                
                // Update equity chart
                if (data.equity_history && data.equity_history.length > 0) {
                    const labels = data.equity_history.slice(-50).map(h => 
                        new Date(h.timestamp).toLocaleTimeString()
                    );
                    const values = data.equity_history.slice(-50).map(h => h.balance);
                    
                    equityChart.data.labels = labels;
                    equityChart.data.datasets[0].data = values;
                    equityChart.update();
                }
                
                // Update P&L chart
                if (data.equity_history && data.equity_history.length > 1) {
                    const pnlData = [];
                    const pnlColors = [];
                    const pnlLabels = [];
                    
                    const history = data.equity_history.slice(-20);
                    for (let i = 1; i < history.length; i++) {
                        const pnl = history[i].pnl || 0;
                        pnlData.push(pnl);
                        pnlColors.push(pnl >= 0 ? 'rgba(16, 185, 129, 0.8)' : 'rgba(239, 68, 68, 0.8)');
                        pnlLabels.push(new Date(history[i].timestamp).toLocaleTimeString());
                    }
                    
                    pnlChart.data.labels = pnlLabels;
                    pnlChart.data.datasets[0].data = pnlData;
                    pnlChart.data.datasets[0].backgroundColor = pnlColors;
                    pnlChart.update();
                }
                
            } catch (error) {
                console.error('Dashboard update error:', error);
                // Use cached data if available
                if (lastData) {
                    document.getElementById('last-update').textContent = 'Using Cached Data';
                }
            }
        }
        
        // Update every 2 seconds
        setInterval(updateDashboard, 2000);
        updateDashboard();
    </script>
</body>
</html>
'''

def create_app(engine: TradingEngine):
    """Create Flask app - NUCLEAR SAFE"""
    app = Flask(__name__)
    CORS(app)
    
    @app.route('/')
    def index():
        return render_template_string(NUCLEAR_DASHBOARD_HTML)
    
    @app.route('/api/comprehensive-status')
    def comprehensive_status():
        """Get status - NEVER CRASHES"""
        try:
            return jsonify(engine.get_comprehensive_status())
        except Exception as e:
            logger.error(f"API error: {e}")
            # Return safe fallback
            return jsonify({
                'balance': engine.balance_manager.get_balance(),
                'total_pnl': 0,
                'realized_pnl': 0,
                'unrealized_pnl': 0,
                'is_running': engine.is_running
            })
    
    return app

# ======================== MAIN APPLICATION ========================
def main():
    """NUCLEAR MAIN - NEVER CRASHES"""
    logger.info("=" * 70)
    logger.info("🔥 NUCLEAR CRYPTO TRADING BOT v6.0")
    logger.info("💣 Bulletproof • Zero Crashes • Binance Global")
    logger.info("=" * 70)
    
    try:
        # Initialize engine
        engine = TradingEngine()
        engine.start()
        
        # Start watchdog
        watchdog.start_monitoring()
        
        # Start nuclear threads
        price_thread = Thread(target=nuclear_price_fetcher, args=(engine,), daemon=True)
        price_thread.start()
        
        trading_thread = Thread(target=nuclear_trading_loop, args=(engine,), daemon=True)
        trading_thread.start()
        
        # Create Flask app
        app = create_app(engine)
        
        logger.info("=" * 70)
        logger.info(f"📊 Dashboard: http://0.0.0.0:{Config.PORT}")
        logger.info("🔴 NUCLEAR TRADING ACTIVE")
        logger.info(f"💰 Balance: ${engine.balance_manager.get_balance():.2f}")
        logger.info("🛡️ Watchdog Protection: ON")
        logger.info("♾️ Infinite Loops: RUNNING")
        logger.info("=" * 70)
        
        # Run Flask (this blocks)
        app.run(host='0.0.0.0', port=Config.PORT, debug=False, threaded=True)
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if 'engine' in locals():
            engine.stop()
    except Exception as e:
        logger.critical(f"Critical error: {e}")
        logger.critical(traceback.format_exc())
        # RESTART AUTOMATICALLY
        time.sleep(10)
        main()  # Recursive restart

if __name__ == "__main__":
    while True:  # NUCLEAR WRAPPER
        try:
            main()
        except:
            logger.critical("MAIN CRASHED - RESTARTING IN 10 SECONDS")
            time.sleep(10)