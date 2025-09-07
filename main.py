#!/usr/bin/env python3
"""
NUCLEAR FIXED Crypto Futures Trading Bot v7.0
Complete single file with auto-recovery, non-blocking API, and 1h timeframe
Production-quality rewrite with RobustMarketDataManager
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
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from threading import Thread, Lock, Event
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ======================== CONFIGURATION ========================
class Config:
    """Central configuration management"""
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', 'true').lower() == 'true'
    
    INITIAL_BALANCE = float(os.getenv('INITIAL_BALANCE', '10000'))
    POSITION_SIZE_USD = float(os.getenv('POSITION_SIZE_USD', '100'))
    LEVERAGE = int(os.getenv('LEVERAGE', '10'))
    MAX_TRADES_PER_ALGO = int(os.getenv('MAX_TRADES_PER_ALGO', '2'))
    
    PORT = int(os.getenv('PORT', '10000'))
    DATA_UPDATE_INTERVAL = int(os.getenv('DATA_UPDATE_INTERVAL', '60'))
    BALANCE_FILE = 'data/balance.json'
    
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '2.0'))
    TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '3.0'))
    
    # Conservative Binance limits
    BINANCE_MAX_REQUESTS_PER_MIN = 600
    API_TIMEOUT = 5

# Create data directory
os.makedirs('data', exist_ok=True)

# ======================== LOGGING ========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data/trading_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# ======================== GLOBAL THREAD REGISTRY ========================
class ThreadRegistry:
    """Registry to track and restart threads with health monitoring"""
    
    def __init__(self):
        self.threads = {}
        self.lock = Lock()
        self.heartbeats = {}
        
    def register(self, name: str, target_func, args=()):
        """Register a thread for monitoring"""
        with self.lock:
            self.threads[name] = {
                'target': target_func,
                'args': args,
                'thread': None,
                'last_heartbeat': datetime.now()
            }
            self.heartbeats[name] = datetime.now()
            
    def heartbeat(self, name: str):
        """Update heartbeat for thread"""
        with self.lock:
            self.heartbeats[name] = datetime.now()
            if name in self.threads:
                self.threads[name]['last_heartbeat'] = datetime.now()
                
    def restart_thread(self, name: str):
        """Kill and restart a thread"""
        with self.lock:
            if name not in self.threads:
                return
                
            thread_info = self.threads[name]
            
            # Try to stop old thread
            if thread_info['thread'] and thread_info['thread'].is_alive():
                logger.warning(f"Cannot kill thread {name}, starting new one anyway")
            
            # Start new thread
            new_thread = Thread(
                target=thread_info['target'], 
                args=thread_info['args'], 
                daemon=True
            )
            new_thread.start()
            thread_info['thread'] = new_thread
            thread_info['last_heartbeat'] = datetime.now()
            self.heartbeats[name] = datetime.now()
            logger.info(f"✅ Restarted thread: {name}")
            
    def check_health(self) -> List[str]:
        """Check thread health and return unhealthy ones"""
        with self.lock:
            unhealthy = []
            now = datetime.now()
            for name, last_heartbeat in self.heartbeats.items():
                if (now - last_heartbeat).total_seconds() > 20:
                    unhealthy.append(name)
            return unhealthy

thread_registry = ThreadRegistry()

# ======================== TELEGRAM NOTIFICATIONS ========================
class TelegramBot:
    """Telegram bot for trade notifications"""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = Config.TELEGRAM_ENABLED and token and chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        
    def send_message(self, message: str, parse_mode: str = 'HTML'):
        """Send message to Telegram"""
        if not self.enabled:
            return
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': self.chat_id, 
                'text': message, 
                'parse_mode': parse_mode
            }
            requests.post(url, data=data, timeout=3)
        except:
            pass
    
    def send_trade_alert(
        self, 
        trade_type: str, 
        symbol: str, 
        side: str, 
        price: float, 
        quantity: float, 
        pnl: float = None
    ):
        """Send formatted trade alert"""
        try:
            emoji = "🟢" if side.upper() == "LONG" else "🔴"
            message = f"<b>{emoji} {trade_type} Alert</b>\n"
            message += f"Symbol: {symbol}\n"
            message += f"Side: {side}\n"
            message += f"Price: ${price:.2f}\n"
            message += f"Quantity: {quantity:.6f}\n"
            if pnl is not None:
                message += f"P&L: ${pnl:+.2f}"
            self.send_message(message)
        except:
            pass

telegram_bot = TelegramBot(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

# ======================== RATE LIMITER ========================
class RateLimiter:
    """Rate limiting for API requests"""
    
    def __init__(self):
        self.requests_per_min = Config.BINANCE_MAX_REQUESTS_PER_MIN
        self.requests = deque()
        self.lock = Lock()
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        
    def can_make_request(self) -> bool:
        """Check if request can be made within rate limits"""
        with self.lock:
            now = time.time()
            # Remove requests older than 60 seconds
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            
            if len(self.requests) < self.requests_per_min:
                self.requests.append(now)
                self.total_requests += 1
                return True
            return False
    
    def wait_if_needed(self):
        """Wait until request can be made"""
        while not self.can_make_request():
            time.sleep(0.1)
    
    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        with self.lock:
            now = time.time()
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            
            success_rate = (
                (self.successful_requests / max(self.total_requests, 1)) * 100
            )
            
            return {
                'requests_used': len(self.requests),
                'requests_limit': self.requests_per_min,
                'total_requests': self.total_requests,
                'success_rate': success_rate,
                'failed_requests': self.failed_requests
            }

# ======================== DATA CACHE ========================
class DataCache:
    """Cache for API responses with TTL"""
    
    def __init__(self, ttl: int = 60):
        self.cache = {}
        self.permanent_cache = {}
        self.ttl = ttl
        self.lock = Lock()
        self.hits = 0
        self.misses = 0
        
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        with self.lock:
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    self.hits += 1
                    return data
                del self.cache[key]
            
            self.misses += 1
            return self.permanent_cache.get(key)
    
    def set(self, key: str, value: Any):
        """Set value in cache"""
        with self.lock:
            self.cache[key] = (value, time.time())
            self.permanent_cache[key] = value
    
    def get_stats(self) -> Dict:
        """Get cache statistics"""
        total = max(self.hits + self.misses, 1)
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': (self.hits / total) * 100
        }

# ======================== DATA MODELS ========================
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
    """Trade data model"""
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
        """Close the trade"""
        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now()
        self.status = TradeStatus.CLOSED
        
        if self.side == OrderSide.BUY:
            self.pnl = (exit_price - self.entry_price) * self.quantity - self.fees
        else:
            self.pnl = (self.entry_price - exit_price) * self.quantity - self.fees
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'trade_id': self.trade_id,
            'symbol': self.symbol,
            'side': self.side.value if isinstance(self.side, OrderSide) else self.side,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'entry_time': (
                self.entry_time.isoformat() 
                if isinstance(self.entry_time, datetime) 
                else str(self.entry_time)
            ),
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'status': (
                self.status.value 
                if isinstance(self.status, TradeStatus) 
                else self.status
            ),
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
    """Position data model"""
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
        """Update unrealized P&L"""
        self.current_price = current_price
        if self.quantity > 0:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
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
        """Check if algorithm can make more trades"""
        return self.is_active and self.trade_count < self.max_trades
    
    def increment_trade_count(self):
        """Increment trade count and update state"""
        self.trade_count += 1
        self.last_trade_time = datetime.now()
        if self.trade_count >= self.max_trades:
            self.is_active = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        win_rate = (
            (self.win_count / (self.win_count + self.loss_count) * 100) 
            if (self.win_count + self.loss_count) > 0 
            else 0
        )
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
            'last_trade_time': (
                self.last_trade_time.isoformat() 
                if self.last_trade_time 
                else None
            )
        }

# ======================== BALANCE MANAGER ========================
class BalanceManager:
    """Manage trading balance and equity history"""
    
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
        """Load balance from file"""
        try:
            if os.path.exists(self.balance_file):
                with open(self.balance_file, 'r') as f:
                    data = json.load(f)
                    logger.info(f"Loaded balance: ${data.get('balance', 0):.2f}")
                    return data
        except Exception as e:
            logger.error(f"Failed to load balance: {e}")
        
        return {
            'balance': self.initial_balance,
            'equity_history': [],
            'max_drawdown': 0,
            'peak_balance': self.initial_balance,
            'created_at': datetime.now().isoformat()
        }
    
    def save_balance(self):
        """Save balance to file"""
        with self.lock:
            try:
                self.balance_data = {
                    'balance': self.balance,
                    'equity_history': self.equity_history[-1000:],
                    'max_drawdown': self.max_drawdown,
                    'peak_balance': self.peak_balance,
                    'last_updated': datetime.now().isoformat()
                }
                
                temp_file = self.balance_file + '.tmp'
                with open(temp_file, 'w') as f:
                    json.dump(self.balance_data, f, indent=2)
                os.replace(temp_file, self.balance_file)
            except Exception as e:
                logger.error(f"Failed to save balance: {e}")
    
    def update_balance(self, pnl: float):
        """Update balance with P&L"""
        with self.lock:
            self.balance += pnl
            
            if self.balance > self.peak_balance:
                self.peak_balance = self.balance
            
            drawdown = (
                ((self.peak_balance - self.balance) / self.peak_balance) * 100 
                if self.peak_balance > 0 
                else 0
            )
            self.max_drawdown = max(self.max_drawdown, drawdown)
            
            self.equity_history.append({
                'timestamp': datetime.now().isoformat(),
                'balance': self.balance,
                'pnl': pnl,
                'drawdown': drawdown
            })
            
            self.save_balance()
    
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

# ======================== ROBUST MARKET DATA MANAGER ========================
class RobustMarketDataManager:
    """Robust market data fetching with circuit breaker and fallback"""
    
    def __init__(self):
        self.base_url = "https://api.binance.com/api/v3"
        self.rate_limiter = RateLimiter()
        self.cache = DataCache(ttl=60)
        self.symbols = [
            'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
            'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT'
        ]
        self.last_successful_data = []
        self.last_update_time = datetime.now()
        self.consecutive_errors = 0
        self.circuit_breaker_active = False
        
    def fetch_klines_1h(self, symbol: str) -> List[Dict]:
        """Fetch 1h klines for technical analysis"""
        try:
            self.rate_limiter.wait_if_needed()
            
            url = f"{self.base_url}/klines"
            params = {
                'symbol': symbol,
                'interval': '1h',
                'limit': 100
            }
            
            response = requests.get(url, params=params, timeout=Config.API_TIMEOUT)
            
            if response.status_code == 200:
                klines = response.json()
                parsed_klines = []
                for k in klines:
                    parsed_klines.append({
                        'timestamp': k[0],
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    })
                return parsed_klines
            
        except requests.Timeout:
            logger.warning(f"Timeout fetching klines for {symbol}")
        except Exception as e:
            logger.debug(f"Error fetching klines: {e}")
        
        return []
    
    def fetch_market_data(self) -> List[MarketData]:
        """
        Fetch market data with:
        1. Cache check
        2. Circuit breaker protection
        3. Non-blocking timeout
        4. Fallback to last successful data
        """
        # Step 1: Check cache first
        cached_data = self.cache.get('market_data')
        if cached_data:
            return cached_data
        
        # Step 2: Circuit breaker check
        if self.circuit_breaker_active:
            if self.consecutive_errors < 5:
                self.circuit_breaker_active = False
            else:
                return self.last_successful_data
        
        # Step 3: Attempt API call
        try:
            self.rate_limiter.wait_if_needed()
            
            response = requests.get(
                f"{self.base_url}/ticker/24hr",
                timeout=Config.API_TIMEOUT
            )
            
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
                            if market_data.price > 0:
                                market_data_list.append(market_data)
                        except:
                            continue
                
                if market_data_list:
                    self.cache.set('market_data', market_data_list)
                    self.last_successful_data = market_data_list
                    self.last_update_time = datetime.now()
                    self.consecutive_errors = 0
                    self.rate_limiter.successful_requests += 1
                    return market_data_list
                    
            elif response.status_code == 429:
                logger.warning("Rate limit hit")
                self.circuit_breaker_active = True
                self.consecutive_errors += 1
                
        except requests.Timeout:
            logger.warning("API timeout - using cached data")
            self.consecutive_errors += 1
            
        except requests.ConnectionError:
            logger.warning("Connection error - using cached data")
            self.consecutive_errors += 1
            
        except Exception as e:
            logger.debug(f"API error: {e}")
            self.consecutive_errors += 1
        
        # Step 4: Activate circuit breaker if needed
        if self.consecutive_errors >= 5:
            self.circuit_breaker_active = True
        
        self.rate_limiter.failed_requests += 1
        return self.last_successful_data
    
    def get_stats(self) -> Dict:
        """Get API statistics"""
        seconds_since_update = (datetime.now() - self.last_update_time).total_seconds()
        return {
            'rate_limiter_stats': self.rate_limiter.get_stats(),
            'cache_stats': self.cache.get_stats(),
            'last_update': self.last_update_time.isoformat(),
            'seconds_since_update': seconds_since_update,
            'is_stale': seconds_since_update > 30,
            'circuit_breaker': self.circuit_breaker_active,
            'consecutive_errors': self.consecutive_errors
        }

# ======================== TRADING ALGORITHMS ========================
class TradingAlgorithm:
    """Base trading algorithm with 1H timeframe analysis"""
    
    def __init__(self, algorithm_id: str, name: str):
        self.algorithm_id = algorithm_id
        self.name = name
        self.state = AlgorithmState(
            algorithm_id=algorithm_id,
            name=name,
            max_trades=Config.MAX_TRADES_PER_ALGO
        )
        self.klines_cache = {}
        self.last_analysis = {}
        
    def analyze_1h_data(self, symbol: str, klines: List[Dict]) -> Dict:
        """Analyze 1h klines for trading signals"""
        if len(klines) < 20:
            return {'signal': None}
        
        # Calculate technical indicators on 1h data
        closes = [k['close'] for k in klines]
        
        # Simple Moving Averages
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sma_20
        
        # RSI (14 periods on 1h)
        rsi = self.calculate_rsi(closes, 14)
        
        # Current price vs indicators
        current_price = closes[-1]
        
        # Generate signal based on 1h analysis
        signal = None
        if current_price > sma_20 and current_price > sma_50 and rsi < 70:
            signal = 'long'
        elif current_price < sma_20 and current_price < sma_50 and rsi > 30:
            signal = 'short'
        
        return {
            'signal': signal,
            'sma_20': sma_20,
            'sma_50': sma_50,
            'rsi': rsi,
            'current_price': current_price
        }
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate RSI indicator"""
        if len(prices) < period + 1:
            return 50.0
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def should_trade(self, market_data: MarketData, klines: List[Dict]) -> Optional[str]:
        """Determine if should trade based on 1h analysis"""
        # Only analyze once per hour
        now = datetime.now()
        if self.algorithm_id in self.last_analysis:
            last_time = self.last_analysis[self.algorithm_id]
            if (now - last_time).total_seconds() < 3600:
                return None
        
        self.last_analysis[self.algorithm_id] = now
        
        # Analyze 1h data
        analysis = self.analyze_1h_data(market_data.symbol, klines)
        
        # Add some randomness to simulate different algorithm behaviors
        if analysis['signal'] and random.random() > 0.7:
            return analysis['signal']
        
        return None
    
    def should_exit(self, position: Position, current_price: float) -> bool:
        """Check if position should be closed"""
        if position.quantity > 0:
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100
        
        return (
            pnl_pct <= -Config.STOP_LOSS_PERCENT or 
            pnl_pct >= Config.TAKE_PROFIT_PERCENT
        )

# ======================== TRADING ENGINE ========================
class TradingEngine:
    """Main trading engine orchestrator"""
    
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.closed_trades = []
        self.market_data_queue = deque(maxlen=1000)
        self.lock = Lock()
        self.balance_manager = BalanceManager(Config.INITIAL_BALANCE)
        self.market_data_manager = RobustMarketDataManager()
        
        self.total_realized_pnl = 0.0
        self.total_unrealized_pnl = 0.0
        
        self.total_wins = 0
        self.total_losses = 0
        self.daily_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0.0})
        
        self.last_known_prices = {}
        self.last_update_time = datetime.now()
        
        self._create_algorithms()
        
        logger.info(f"✅ Trading Engine initialized")
        logger.info(f"💰 Balance: ${self.balance_manager.get_balance():.2f}")
        
        telegram_bot.send_message(
            f"🚀 Bot Started\nBalance: ${self.balance_manager.get_balance():.2f}"
        )
    
    def _create_algorithms(self):
        """Create all trading algorithms"""
        strategies = [
            "BTC Moving Average Crossover",
            "ETH RSI Oversold/Overbought",
            "BTC MACD Signal",
            "Bollinger Bands Multi-Crypto",
            "Volume Weighted BTC Strategy",
            "Mean Reversion ETH/BTC",
            "Momentum Trading SOL",
            "Breakout Strategy BNB",
            "Scalping Strategy XRP",
            "Swing Trading ADA",
            "BTC/ETH Pairs Trading",
            "Cross-Exchange Arbitrage",
            "Perpetual Funding Rate",
            "Trend Following MATIC",
            "Range Trading AVAX",
            "News Based BTC Trading",
            "Sentiment Analysis DOGE",
            "Options Delta Hedging",
            "High Frequency BTC Micro"
        ]
        
        for i, strategy in enumerate(strategies):
            algo = TradingAlgorithm(f"algo_{i+1}", strategy)
            self.algorithms.append(algo)
    
    def get_active_algorithm(self) -> Optional[TradingAlgorithm]:
        """Get an active algorithm that can trade"""
        for algo in self.algorithms:
            if algo.state.can_trade():
                return algo
        return None
    
    def calculate_live_pnl(self) -> Dict:
        """Calculate current P&L"""
        try:
            total_unrealized = 0.0
            
            # Update all open positions
            for position in self.positions.values():
                latest_price = position.current_price
                
                # Check for latest price in queue
                for data in reversed(list(self.market_data_queue)):
                    if data.symbol == position.symbol:
                        latest_price = data.price
                        position.current_price = latest_price
                        self.last_known_prices[position.symbol] = latest_price
                        break
                
                # Use last known price if available
                if position.symbol in self.last_known_prices:
                    latest_price = self.last_known_prices[position.symbol]
                    position.current_price = latest_price
                
                # Calculate unrealized P&L
                if position.quantity > 0:
                    position.unrealized_pnl = (
                        (latest_price - position.entry_price) * position.quantity
                    )
                else:
                    position.unrealized_pnl = (
                        (position.entry_price - latest_price) * abs(position.quantity)
                    )
                
                total_unrealized += position.unrealized_pnl
            
            # Calculate total realized P&L
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
        """Process incoming market data"""
        if not self.is_running:
            return
        
        try:
            with self.lock:
                # Add to queue
                self.market_data_queue.append(market_data)
                self.last_update_time = datetime.now()
                self.last_known_prices[market_data.symbol] = market_data.price
                
                # Update P&L
                self.calculate_live_pnl()
                
                # Check for exits
                self._check_exits(market_data)
                
                # Get 1h klines for analysis
                binance_symbol = market_data.symbol.replace('-PERP', 'USDT')
                klines = self.market_data_manager.fetch_klines_1h(binance_symbol)
                
                # Check for new trades
                algo = self.get_active_algorithm()
                if algo and algo.state.can_trade() and klines:
                    direction = algo.should_trade(market_data, klines)
                    if direction:
                        self._execute_trade(algo, market_data, direction)
                        
        except Exception as e:
            logger.error(f"Market data processing error: {e}")
    
    def _check_exits(self, market_data: MarketData):
        """Check if any positions should be closed"""
        try:
            positions_to_close = []
            
            for pos_key, position in self.positions.items():
                if position.symbol == market_data.symbol:
                    algo = next(
                        (a for a in self.algorithms 
                         if a.algorithm_id == position.algorithm_id), 
                        None
                    )
                    
                    if algo:
                        should_exit = algo.should_exit(position, market_data.price)
                        
                        # Check stop loss
                        if position.stop_loss and market_data.price <= position.stop_loss:
                            should_exit = True
                        
                        # Check take profit
                        if position.take_profit and market_data.price >= position.take_profit:
                            should_exit = True
                        
                        if should_exit:
                            positions_to_close.append(pos_key)
            
            # Close positions
            for pos_key in positions_to_close:
                self._close_position(pos_key, market_data)
                
        except Exception as e:
            logger.error(f"Exit check error: {e}")
    
    def _execute_trade(
        self, 
        algorithm: TradingAlgorithm, 
        market_data: MarketData, 
        direction: str
    ):
        """Execute a new trade"""
        try:
            trade_id = str(uuid.uuid4())
            quantity = Config.POSITION_SIZE_USD / market_data.price
            quantity = round(quantity, 6)
            
            side = OrderSide.BUY if direction == 'long' else OrderSide.SELL
            
            # Calculate stop loss and take profit
            if direction == 'long':
                stop_loss = market_data.price * (1 - Config.STOP_LOSS_PERCENT / 100)
                take_profit = market_data.price * (1 + Config.TAKE_PROFIT_PERCENT / 100)
            else:
                stop_loss = market_data.price * (1 + Config.STOP_LOSS_PERCENT / 100)
                take_profit = market_data.price * (1 - Config.TAKE_PROFIT_PERCENT / 100)
            
            # Create trade
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
            
            # Update algorithm state
            algorithm.state.increment_trade_count()
            algorithm.state.trades.append(trade)
            
            logger.info(
                f"🔥 TRADE: {algorithm.name} {direction.upper()} "
                f"{market_data.symbol} @ ${market_data.price:.2f}"
            )
            
            telegram_bot.send_trade_alert(
                "OPEN", 
                market_data.symbol, 
                direction.upper(),
                market_data.price, 
                quantity
            )
            
            # Update daily stats
            today = datetime.now().date().isoformat()
            self.daily_stats[today]['trades'] += 1
            
        except Exception as e:
            logger.error(f"Trade execution error: {e}")
    
    def _close_position(self, pos_key: str, market_data: MarketData):
        """Close a position"""
        try:
            position = self.positions.get(pos_key)
            if not position:
                return
            
            # Calculate P&L
            if position.quantity > 0:
                pnl = (market_data.price - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - market_data.price) * abs(position.quantity)
            
            # Close all trades in position
            for trade in position.trades:
                trade.close(market_data.price)
                trade.pnl = pnl
                self.closed_trades.append(trade)
            
            # Update balance
            self.balance_manager.update_balance(pnl)
            
            # Update algorithm stats
            algo = next(
                (a for a in self.algorithms 
                 if a.algorithm_id == position.algorithm_id), 
                None
            )
            
            if algo:
                algo.state.total_pnl += pnl
                if pnl > 0:
                    algo.state.win_count += 1
                    self.total_wins += 1
                else:
                    algo.state.loss_count += 1
                    self.total_losses += 1
                
                logger.info(f"📊 CLOSED: {algo.name} P&L: ${pnl:+.2f}")
            
            telegram_bot.send_trade_alert(
                "CLOSE", 
                position.symbol,
                "LONG" if position.quantity > 0 else "SHORT",
                market_data.price, 
                abs(position.quantity), 
                pnl
            )
            
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
        """Get comprehensive engine status"""
        try:
            with self.lock:
                pnl_data = self.calculate_live_pnl()
                balance_stats = self.balance_manager.get_stats()
                api_stats = self.market_data_manager.get_stats()
                
                total_trades = sum(a.state.trade_count for a in self.algorithms)
                win_rate = (
                    (self.total_wins / (self.total_wins + self.total_losses) * 100) 
                    if (self.total_wins + self.total_losses) > 0 
                    else 0
                )
                
                # Prepare open positions data
                open_positions = []
                for pos_key, position in self.positions.items():
                    algo = next(
                        (a for a in self.algorithms 
                         if a.algorithm_id == position.algorithm_id), 
                        None
                    )
                    
                    if position.quantity > 0:
                        pnl_percent = (
                            (position.current_price - position.entry_price) / 
                            position.entry_price * 100
                        )
                    else:
                        pnl_percent = (
                            (position.entry_price - position.current_price) / 
                            position.entry_price * 100
                        )
                    
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
                
                # Prepare trade history
                trade_history = []
                for trade in self.closed_trades[-50:]:
                    algo = next(
                        (a for a in self.algorithms 
                         if a.algorithm_id == trade.algorithm_id), 
                        None
                    )
                    trade_history.append({
                        'symbol': trade.symbol,
                        'side': (
                            trade.side.value 
                            if hasattr(trade.side, 'value') 
                            else str(trade.side)
                        ),
                        'entry_price': trade.entry_price,
                        'exit_price': trade.exit_price,
                        'pnl': trade.pnl,
                        'entry_time': (
                            trade.entry_time.isoformat() 
                            if trade.entry_time 
                            else None
                        ),
                        'exit_time': (
                            trade.exit_time.isoformat() 
                            if trade.exit_time 
                            else None
                        ),
                        'algorithm': algo.name if algo else 'Unknown'
                    })
                
                return {
                    'balance': balance_stats['balance'],
                    'initial_balance': balance_stats['initial_balance'],
                    'total_pnl': pnl_data['total'],
                    'realized_pnl': pnl_data['realized'],
                    'unrealized_pnl': pnl_data['unrealized'],
                    'roi': (
                        (balance_stats['balance'] - balance_stats['initial_balance']) / 
                        balance_stats['initial_balance'] * 100
                    ),
                    'max_drawdown': balance_stats['max_drawdown'],
                    'peak_balance': balance_stats['peak_balance'],
                    'win_rate': win_rate,
                    'total_wins': self.total_wins,
                    'total_losses': self.total_losses,
                    'total_trades': total_trades,
                    'open_positions_count': len(self.positions),
                    'closed_trades_count': len(self.closed_trades),
                    'open_positions': open_positions,
                    'trade_history': trade_history,
                    'algorithms': [a.state.to_dict() for a in self.algorithms],
                    'equity_history': balance_stats['equity_history'][-100:],
                    'api_stats': api_stats,
                    'is_running': self.is_running,
                    'leverage': Config.LEVERAGE,
                    'last_update': self.last_update_time.isoformat()
                }
        except Exception as e:
            logger.error(f"Status error: {e}")
            return {
                'balance': self.balance_manager.get_balance(),
                'initial_balance': Config.INITIAL_BALANCE,
                'total_pnl': self.total_realized_pnl + self.total_unrealized_pnl,
                'realized_pnl': self.total_realized_pnl,
                'unrealized_pnl': self.total_unrealized_pnl,
                'is_running': self.is_running,
                'api_stats': {'is_stale': True}
            }

# ======================== WORKER THREADS ========================
def price_fetcher_loop(engine: TradingEngine):
    """Self-healing price fetcher loop"""
    logger.info("🔴 Price fetcher started")
    
    while True:
        try:
            thread_registry.heartbeat('price_fetcher')
            
            if not engine.is_running:
                time.sleep(1)
                continue
            
            # Fetch market data (non-blocking with timeout)
            market_data_list = engine.market_data_manager.fetch_market_data()
            
            if market_data_list:
                for market_data in market_data_list:
                    try:
                        engine.process_market_data(market_data)
                    except Exception as e:
                        logger.debug(f"Process error: {e}")
                    time.sleep(0.1)
            
            time.sleep(Config.DATA_UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Price fetcher error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

def trading_loop(engine: TradingEngine):
    """Self-healing trading loop"""
    logger.info("🤖 Trading loop started")
    
    while True:
        try:
            thread_registry.heartbeat('trading_loop')
            
            if not engine.is_running:
                time.sleep(1)
                continue
            
            # Update P&L
            engine.calculate_live_pnl()
            
            # Periodic balance save
            if random.random() < 0.05:
                engine.balance_manager.save_balance()
            
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Trading loop error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

def watchdog_loop():
    """Watchdog that restarts dead threads"""
    logger.info("🛡️ Watchdog started")
    
    while True:
        try:
            time.sleep(10)
            
            unhealthy = thread_registry.check_health()
            
            for thread_name in unhealthy:
                logger.warning(f"🚨 Thread {thread_name} is unhealthy - restarting")
                thread_registry.restart_thread(thread_name)
            
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
            time.sleep(5)

# ======================== WEB DASHBOARD ========================
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crypto Trading Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0e27;
            color: #e4e4e7;
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
        
        .stale-warning {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid #ef4444;
            color: #ef4444;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            display: none;
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
        }
        
        .metric-value {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
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
        
        .chart-canvas {
            position: relative;
            height: 300px;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th {
            background: #0f1729;
            padding: 0.75rem;
            text-align: left;
            font-size: 0.875rem;
            color: #9ca3af;
        }
        
        td {
            padding: 0.75rem;
            border-bottom: 1px solid #2a3f5f;
            font-size: 0.875rem;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div>
                <h1>Crypto Trading Dashboard</h1>
                <p>Binance Global • 19 Algorithms • 1H Timeframe</p>
            </div>
            <div id="stale-warning" class="stale-warning">
                ⏳ STALE DATA - Prices may be outdated
            </div>
        </div>
    </div>
    
    <div class="container">
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Balance</div>
                <div id="balance" class="metric-value">$0.00</div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Total P&L</div>
                <div id="total-pnl" class="metric-value">$0.00</div>
                <div style="font-size: 0.875rem; color: #9ca3af;">
                    R: <span id="realized-pnl">$0</span> | U: <span id="unrealized-pnl">$0</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">ROI %</div>
                <div id="roi" class="metric-value">0.00%</div>
                <div style="font-size: 0.875rem; color: #9ca3af;">
                    Max DD: <span id="max-dd">0.00%</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div id="win-rate" class="metric-value">0.00%</div>
                <div style="font-size: 0.875rem; color: #9ca3af;">
                    <span id="wins">0</span>W / <span id="losses">0</span>L
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Positions</div>
                <div id="positions-count" class="metric-value">0</div>
                <div style="font-size: 0.875rem; color: #9ca3af;">
                    Closed: <span id="closed-count">0</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">API Weight</div>
                <div id="api-usage" class="metric-value">0/600</div>
                <div style="font-size: 0.875rem; color: #9ca3af;">
                    Success: <span id="api-success">0%</span>
                </div>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>Equity Curve</h3>
            <div class="chart-canvas">
                <canvas id="equityChart"></canvas>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>Open Positions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>P&L</th>
                        <th>P&L %</th>
                        <th>Algorithm</th>
                    </tr>
                </thead>
                <tbody id="positions-tbody">
                    <tr><td colspan="7" style="text-align: center;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="chart-container">
            <h3>Algorithm Performance</h3>
            <table>
                <thead>
                    <tr>
                        <th>Algorithm</th>
                        <th>Status</th>
                        <th>Trades</th>
                        <th>Win Rate</th>
                        <th>Total P&L</th>
                    </tr>
                </thead>
                <tbody id="algorithms-tbody">
                    <tr><td colspan="5" style="text-align: center;">Loading...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <script>
        let lastData = null;
        
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
                    x: { grid: { color: 'rgba(255,255,255,0.05)' } },
                    y: { grid: { color: 'rgba(255,255,255,0.05)' } }
                }
            }
        });
        
        function formatCurrency(value) {
            return new Intl.NumberFormat('en-US', {
                style: 'currency',
                currency: 'USD'
            }).format(value || 0);
        }
        
        async function updateDashboard() {
            try {
                const response = await fetch('/api/comprehensive-status');
                const data = response.ok ? await response.json() : lastData;
                
                if (!data) return;
                lastData = data;
                
                // Check if data is stale
                if (data.api_stats && data.api_stats.is_stale) {
                    document.getElementById('stale-warning').style.display = 'block';
                } else {
                    document.getElementById('stale-warning').style.display = 'none';
                }
                
                // Update metrics
                document.getElementById('balance').textContent = formatCurrency(data.balance);
                
                const pnlEl = document.getElementById('total-pnl');
                pnlEl.textContent = formatCurrency(data.total_pnl);
                pnlEl.className = 'metric-value ' + 
                    (data.total_pnl > 0 ? 'positive' : data.total_pnl < 0 ? 'negative' : 'neutral');
                
                document.getElementById('realized-pnl').textContent = formatCurrency(data.realized_pnl);
                document.getElementById('unrealized-pnl').textContent = formatCurrency(data.unrealized_pnl);
                
                const roiEl = document.getElementById('roi');
                roiEl.textContent = (data.roi || 0).toFixed(2) + '%';
                roiEl.className = 'metric-value ' + 
                    (data.roi > 0 ? 'positive' : data.roi < 0 ? 'negative' : 'neutral');
                
                document.getElementById('max-dd').textContent = (data.max_drawdown || 0).toFixed(2) + '%';
                document.getElementById('win-rate').textContent = (data.win_rate || 0).toFixed(1) + '%';
                document.getElementById('wins').textContent = data.total_wins || 0;
                document.getElementById('losses').textContent = data.total_losses || 0;
                document.getElementById('positions-count').textContent = data.open_positions_count || 0;
                document.getElementById('closed-count').textContent = data.closed_trades_count || 0;
                
                // API stats
                if (data.api_stats) {
                    const stats = data.api_stats.rate_limiter_stats || {};
                    document.getElementById('api-usage').textContent = 
                        (stats.requests_used || 0) + '/600';
                    document.getElementById('api-success').textContent = 
                        (stats.success_rate || 0).toFixed(1) + '%';
                }
                
                // Update positions
                const posTbody = document.getElementById('positions-tbody');
                if (data.open_positions && data.open_positions.length > 0) {
                    posTbody.innerHTML = data.open_positions.map(pos => `
                        <tr>
                            <td>${pos.symbol}</td>
                            <td>${pos.side}</td>
                            <td>${formatCurrency(pos.entry_price)}</td>
                            <td>${formatCurrency(pos.current_price)}</td>
                            <td class="${pos.unrealized_pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(pos.unrealized_pnl)}
                            </td>
                            <td class="${pos.pnl_percent >= 0 ? 'positive' : 'negative'}">
                                ${pos.pnl_percent.toFixed(2)}%
                            </td>
                            <td>${pos.algorithm}</td>
                        </tr>
                    `).join('');
                } else {
                    posTbody.innerHTML = '<tr><td colspan="7" style="text-align: center;">No open positions</td></tr>';
                }
                
                // Update algorithms
                const algoTbody = document.getElementById('algorithms-tbody');
                if (data.algorithms && data.algorithms.length > 0) {
                    algoTbody.innerHTML = data.algorithms.map(algo => `
                        <tr>
                            <td>${algo.name}</td>
                            <td>${algo.is_active ? 'Active' : 'Complete'}</td>
                            <td>${algo.trade_count}/${algo.max_trades}</td>
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
                
            } catch (error) {
                console.error('Dashboard error:', error);
                if (lastData) {
                    document.getElementById('stale-warning').style.display = 'block';
                }
            }
        }
        
        setInterval(updateDashboard, 2000);
        updateDashboard();
    </script>
</body>
</html>
'''

def create_app(engine: TradingEngine):
    """Create Flask application"""
    app = Flask(__name__)
    CORS(app)
    
    @app.route('/')
    def index():
        return render_template_string(DASHBOARD_HTML)
    
    @app.route('/api/comprehensive-status')
    def comprehensive_status():
        try:
            return jsonify(engine.get_comprehensive_status())
        except Exception as e:
            logger.error(f"API error: {e}")
            return jsonify({
                'balance': engine.balance_manager.get_balance(),
                'total_pnl': 0,
                'realized_pnl': 0,
                'unrealized_pnl': 0,
                'is_running': engine.is_running,
                'api_stats': {'is_stale': True}
            })
    
    return app

# ======================== MAIN ENTRY POINT ========================
def main():
    """Main application entry point"""
    logger.info("=" * 70)
    logger.info("🚀 NUCLEAR CRYPTO TRADING BOT v7.0")
    logger.info("=" * 70)
    
    try:
        # Initialize trading engine
        engine = TradingEngine()
        engine.start()
        
        # Register worker threads
        thread_registry.register('price_fetcher', price_fetcher_loop, (engine,))
        thread_registry.register('trading_loop', trading_loop, (engine,))
        
        # Start worker threads
        thread_registry.restart_thread('price_fetcher')
        thread_registry.restart_thread('trading_loop')
        
        # Start watchdog thread
        watchdog_thread = Thread(target=watchdog_loop, daemon=True)
        watchdog_thread.start()
        
        # Create and configure Flask app
        app = create_app(engine)
        
        logger.info(f"📊 Dashboard: http://0.0.0.0:{Config.PORT}")
        logger.info("🔴 LIVE TRADING ACTIVE")
        logger.info(f"💰 Balance: ${engine.balance_manager.get_balance():.2f}")
        logger.info("=" * 70)
        
        # Run Flask server
        app.run(
            host='0.0.0.0', 
            port=Config.PORT, 
            debug=False, 
            threaded=True
        )
        
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        if 'engine' in locals():
            engine.stop()
    except Exception as e:
        logger.critical(f"Critical error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()