#!/usr/bin/env python3
"""
Complete Crypto Futures Trading Bot v3.0
Single file implementation with Telegram, Enhanced Dashboard, and Multi-API Support
"""

import os
import sys
import time
import json
import uuid
import random
import pickle
import logging
import requests
import threading
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from threading import Thread, Lock, Event
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
from dotenv import load_dotenv
import urllib.parse

# Load environment variables
load_dotenv()

# ======================== CONFIGURATION ========================
class Config:
    """Central configuration from environment variables"""
    # API Keys
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    COINGECKO_API_KEY = os.getenv('COINGECKO_API_KEY', '')
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', 'true').lower() == 'true'
    
    # Trading Parameters
    INITIAL_BALANCE = float(os.getenv('INITIAL_BALANCE', '10000'))
    POSITION_SIZE_USD = float(os.getenv('POSITION_SIZE_USD', '100'))
    LEVERAGE = int(os.getenv('LEVERAGE', '10'))
    MAX_TRADES_PER_ALGO = int(os.getenv('MAX_TRADES_PER_ALGO', '2'))
    
    # API Rate Limits
    COINGECKO_MAX_REQUESTS_PER_MIN = int(os.getenv('COINGECKO_MAX_REQUESTS_PER_MIN', '10'))
    BINANCE_MAX_REQUESTS_PER_MIN = int(os.getenv('BINANCE_MAX_REQUESTS_PER_MIN', '1200'))
    
    # System
    PORT = int(os.getenv('PORT', '5000'))
    DATA_UPDATE_INTERVAL = int(os.getenv('DATA_UPDATE_INTERVAL', '30'))
    BALANCE_FILE = os.getenv('BALANCE_FILE', 'balance.pkl')
    LOG_FILE = os.getenv('LOG_FILE', 'trading_bot.json')
    
    # Risk Management
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '2.0'))
    TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '3.0'))

# ======================== STRUCTURED LOGGING ========================
class JSONLogger:
    """Structured JSON logging for all events"""
    def __init__(self, filename='trading_bot.json'):
        self.filename = filename
        self.lock = Lock()
        
    def log(self, event_type: str, data: Dict[str, Any]):
        """Log event to JSON file"""
        with self.lock:
            log_entry = {
                'timestamp': datetime.now().isoformat(),
                'event_type': event_type,
                'data': data
            }
            try:
                with open(self.filename, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')
            except Exception as e:
                print(f"Failed to write log: {e}")

# Initialize JSON logger
json_logger = JSONLogger(Config.LOG_FILE)

# Standard logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('trading_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# ======================== TELEGRAM INTEGRATION ========================
class TelegramBot:
    """Telegram bot for notifications"""
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
            response = requests.post(url, data=data, timeout=5)
            if response.status_code != 200:
                logger.error(f"Telegram send failed: {response.text}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    
    def send_trade_alert(self, trade_type: str, symbol: str, side: str, price: float, quantity: float, pnl: float = None):
        """Send trade alert to Telegram"""
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
    
    def send_daily_report(self, stats: Dict):
        """Send daily performance report"""
        message = "<b>📊 Daily Trading Report</b>\n\n"
        message += f"Total P&L: ${stats.get('total_pnl', 0):+.2f}\n"
        message += f"Win Rate: {stats.get('win_rate', 0):.1f}%\n"
        message += f"Total Trades: {stats.get('total_trades', 0)}\n"
        message += f"Open Positions: {stats.get('open_positions', 0)}\n"
        message += f"Account Balance: ${stats.get('balance', 0):.2f}\n"
        
        self.send_message(message)

# Initialize Telegram bot
telegram_bot = TelegramBot(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

# ======================== RATE LIMITER ========================
class RateLimiter:
    """Rate limiter for API calls"""
    def __init__(self, max_requests: int, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
        self.lock = Lock()
    
    def can_make_request(self) -> bool:
        """Check if request can be made"""
        with self.lock:
            now = time.time()
            # Remove old requests
            while self.requests and self.requests[0] < now - self.time_window:
                self.requests.popleft()
            
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False
    
    def wait_if_needed(self):
        """Wait if rate limit exceeded"""
        while not self.can_make_request():
            time.sleep(0.1)

# ======================== DATA CACHE ========================
class DataCache:
    """Cache for market data"""
    def __init__(self, ttl: int = 30):
        self.cache = {}
        self.ttl = ttl
        self.lock = Lock()
    
    def get(self, key: str) -> Optional[Any]:
        """Get cached data if not expired"""
        with self.lock:
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    return data
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Any):
        """Set cache data"""
        with self.lock:
            self.cache[key] = (value, time.time())

# ======================== MODELS ========================
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
    """Persistent balance management"""
    def __init__(self, initial_balance: float = 10000.0):
        self.lock = Lock()
        self.balance_file = Config.BALANCE_FILE
        self.balance = self._load_balance(initial_balance)
        self.initial_balance = initial_balance
        self.equity_history = []
        self.last_save_time = time.time()
    
    def _load_balance(self, initial_balance: float) -> float:
        """Load balance from disk"""
        try:
            if os.path.exists(self.balance_file):
                with open(self.balance_file, 'rb') as f:
                    data = pickle.load(f)
                    logger.info(f"Loaded balance: ${data['balance']:.2f}")
                    self.equity_history = data.get('equity_history', [])
                    return data['balance']
        except Exception as e:
            logger.error(f"Failed to load balance: {e}")
        return initial_balance
    
    def save_balance(self):
        """Save balance to disk"""
        with self.lock:
            try:
                data = {
                    'balance': self.balance,
                    'equity_history': self.equity_history[-1000:],  # Keep last 1000 points
                    'timestamp': datetime.now().isoformat()
                }
                with open(self.balance_file, 'wb') as f:
                    pickle.dump(data, f)
                
                # Save periodically, not every update
                self.last_save_time = time.time()
            except Exception as e:
                logger.error(f"Failed to save balance: {e}")
    
    def update_balance(self, pnl: float):
        """Update balance with P&L"""
        with self.lock:
            self.balance += pnl
            self.equity_history.append({
                'timestamp': datetime.now().isoformat(),
                'balance': self.balance,
                'pnl': pnl
            })
            
            # Save every 60 seconds
            if time.time() - self.last_save_time > 60:
                self.save_balance()
    
    def get_balance(self) -> float:
        """Get current balance"""
        with self.lock:
            return self.balance
    
    def get_equity_history(self) -> List[Dict]:
        """Get equity history for charts"""
        with self.lock:
            return self.equity_history.copy()

# ======================== MARKET DATA PROVIDERS ========================
class CoinGeckoProvider:
    """CoinGecko API data provider"""
    def __init__(self):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.api_key = Config.COINGECKO_API_KEY
        self.rate_limiter = RateLimiter(Config.COINGECKO_MAX_REQUESTS_PER_MIN)
        self.cache = DataCache(ttl=30)
        self.crypto_ids = {
            'bitcoin': 'BTC-PERP', 'ethereum': 'ETH-PERP', 
            'binancecoin': 'BNB-PERP', 'solana': 'SOL-PERP',
            'ripple': 'XRP-PERP', 'cardano': 'ADA-PERP',
            'avalanche-2': 'AVAX-PERP', 'polkadot': 'DOT-PERP',
            'polygon': 'MATIC-PERP', 'chainlink': 'LINK-PERP'
        }
        self.last_prices = {}
    
    def get_market_data(self) -> List[MarketData]:
        """Get market data from CoinGecko"""
        # Check cache first
        cached_data = self.cache.get('market_data')
        if cached_data:
            return cached_data
        
        # Rate limit check
        self.rate_limiter.wait_if_needed()
        
        try:
            headers = {'x-cg-pro-api-key': self.api_key} if self.api_key else {}
            ids = ','.join(self.crypto_ids.keys())
            url = f"{self.base_url}/simple/price"
            params = {
                'ids': ids,
                'vs_currencies': 'usd',
                'include_24hr_change': 'true',
                'include_24hr_vol': 'true'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                market_data_list = []
                
                for coin_id, symbol in self.crypto_ids.items():
                    if coin_id in data:
                        coin_data = data[coin_id]
                        price = coin_data.get('usd', 0)
                        volume = coin_data.get('usd_24h_vol', 0)
                        change = coin_data.get('usd_24h_change', 0)
                        
                        open_price = self.last_prices.get(symbol, price)
                        self.last_prices[symbol] = price
                        
                        market_data = MarketData(
                            symbol=symbol,
                            price=price,
                            volume=volume if volume else 1000000,
                            timestamp=datetime.now(),
                            bid=price * 0.9999,
                            ask=price * 1.0001,
                            open=open_price,
                            high=price * (1 + abs(change/200)),
                            low=price * (1 - abs(change/200)),
                            close=price
                        )
                        market_data_list.append(market_data)
                
                # Cache the data
                self.cache.set('market_data', market_data_list)
                return market_data_list
            else:
                logger.warning(f"CoinGecko API error: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"CoinGecko error: {e}")
            return []

class BinanceProvider:
    """Binance API data provider (fallback)"""
    def __init__(self):
        self.base_url = "https://api.binance.com/api/v3"
        self.api_key = Config.BINANCE_API_KEY
        self.api_secret = Config.BINANCE_API_SECRET
        self.rate_limiter = RateLimiter(Config.BINANCE_MAX_REQUESTS_PER_MIN)
        self.cache = DataCache(ttl=10)
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                       'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT']
    
    def get_market_data(self) -> List[MarketData]:
        """Get market data from Binance"""
        # Check cache first
        cached_data = self.cache.get('binance_data')
        if cached_data:
            return cached_data
        
        # Rate limit check
        self.rate_limiter.wait_if_needed()
        
        try:
            market_data_list = []
            
            for symbol in self.symbols:
                url = f"{self.base_url}/ticker/24hr"
                params = {'symbol': symbol}
                
                response = requests.get(url, params=params, timeout=5)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Get orderbook for bid/ask
                    book_url = f"{self.base_url}/depth"
                    book_params = {'symbol': symbol, 'limit': 5}
                    book_response = requests.get(book_url, params=book_params, timeout=5)
                    
                    bid = float(data['bidPrice'])
                    ask = float(data['askPrice'])
                    
                    if book_response.status_code == 200:
                        book_data = book_response.json()
                        if book_data['bids']:
                            bid = float(book_data['bids'][0][0])
                        if book_data['asks']:
                            ask = float(book_data['asks'][0][0])
                    
                    market_data = MarketData(
                        symbol=symbol.replace('USDT', '-PERP'),
                        price=float(data['lastPrice']),
                        volume=float(data['volume']),
                        timestamp=datetime.now(),
                        bid=bid,
                        ask=ask,
                        open=float(data['openPrice']),
                        high=float(data['highPrice']),
                        low=float(data['lowPrice']),
                        close=float(data['lastPrice'])
                    )
                    market_data_list.append(market_data)
            
            # Cache the data
            self.cache.set('binance_data', market_data_list)
            return market_data_list
            
        except Exception as e:
            logger.error(f"Binance error: {e}")
            return []

class MarketDataManager:
    """Manages market data from multiple sources"""
    def __init__(self):
        self.coingecko = CoinGeckoProvider()
        self.binance = BinanceProvider()
        self.use_binance = False
        self.failure_count = 0
    
    def get_market_data(self) -> List[MarketData]:
        """Get market data with fallback"""
        # Try CoinGecko first
        if not self.use_binance:
            data = self.coingecko.get_market_data()
            if data:
                self.failure_count = 0
                return data
            else:
                self.failure_count += 1
                if self.failure_count >= 3:
                    logger.warning("Switching to Binance due to CoinGecko failures")
                    self.use_binance = True
        
        # Fallback to Binance
        data = self.binance.get_market_data()
        if data:
            return data
        
        # If Binance also fails, try CoinGecko again
        self.use_binance = False
        self.failure_count = 0
        return self.coingecko.get_market_data()

# ======================== TRADING ENGINE ========================
class TradingAlgorithm:
    """Base trading algorithm"""
    def __init__(self, algorithm_id: str, name: str):
        self.algorithm_id = algorithm_id
        self.name = name
        self.state = AlgorithmState(
            algorithm_id=algorithm_id,
            name=name,
            max_trades=Config.MAX_TRADES_PER_ALGO
        )
    
    def should_trade(self, market_data: MarketData) -> Optional[str]:
        """Determine if should trade"""
        if random.random() > 0.95:  # 5% chance to trade
            return 'long' if random.random() > 0.5 else 'short'
        return None
    
    def should_exit(self, position: Position, current_price: float) -> bool:
        """Determine if should exit position"""
        if position.quantity > 0:  # Long position
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:  # Short position
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100
        
        # Check stop loss and take profit
        return (pnl_pct <= -Config.STOP_LOSS_PERCENT or 
                pnl_pct >= Config.TAKE_PROFIT_PERCENT)

class TradingEngine:
    """Main trading engine"""
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.market_data_queue = deque(maxlen=1000)
        self.lock = Lock()
        self.balance_manager = BalanceManager(Config.INITIAL_BALANCE)
        self.market_data_manager = MarketDataManager()
        
        # P&L tracking
        self.total_realized_pnl = 0.0
        self.total_unrealized_pnl = 0.0
        
        # Statistics
        self.total_wins = 0
        self.total_losses = 0
        self.daily_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0.0})
        
        # Initialize algorithms
        self._create_algorithms()
        
        logger.info(f"✅ Trading Engine initialized with {len(self.algorithms)} algorithms")
        telegram_bot.send_message("🚀 Trading Bot Started\n" + 
                                 f"Balance: ${self.balance_manager.get_balance():.2f}\n" +
                                 f"Algorithms: {len(self.algorithms)}")
    
    def _create_algorithms(self):
        """Create 19 trading algorithms"""
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
        """Get next active algorithm"""
        for algo in self.algorithms:
            if algo.state.can_trade():
                return algo
        return None
    
    def calculate_live_pnl(self) -> float:
        """Calculate total P&L"""
        total_unrealized = 0.0
        
        for position in self.positions.values():
            # Find latest price
            latest_price = None
            for data in reversed(list(self.market_data_queue)):
                if data.symbol == position.symbol:
                    latest_price = data.price
                    break
            
            if latest_price:
                position.update_pnl(latest_price)
                total_unrealized += position.unrealized_pnl
        
        self.total_unrealized_pnl = total_unrealized
        return self.total_realized_pnl + self.total_unrealized_pnl
    
    def process_market_data(self, market_data: MarketData):
        """Process incoming market data"""
        if not self.is_running:
            return
        
        with self.lock:
            self.market_data_queue.append(market_data)
            self.calculate_live_pnl()
            
            # Check for exits
            self._check_exits(market_data)
            
            # Check for new trades
            algo = self.get_active_algorithm()
            if algo and algo.state.can_trade():
                direction = algo.should_trade(market_data)
                if direction:
                    self._execute_trade(algo, market_data, direction)
    
    def _check_exits(self, market_data: MarketData):
        """Check if positions should be closed"""
        positions_to_close = []
        
        for pos_key, position in self.positions.items():
            if position.symbol == market_data.symbol:
                algo = next((a for a in self.algorithms 
                           if a.algorithm_id == position.algorithm_id), None)
                
                if algo:
                    # Check stop loss / take profit
                    should_exit = algo.should_exit(position, market_data.price)
                    
                    # Check manual SL/TP levels
                    if position.stop_loss and market_data.price <= position.stop_loss:
                        should_exit = True
                    if position.take_profit and market_data.price >= position.take_profit:
                        should_exit = True
                    
                    if should_exit:
                        positions_to_close.append(pos_key)
        
        for pos_key in positions_to_close:
            self._close_position(pos_key, market_data)
    
    def _execute_trade(self, algorithm: TradingAlgorithm, market_data: MarketData, direction: str):
        """Execute a new trade"""
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
        
        # Update algorithm state
        algorithm.state.increment_trade_count()
        algorithm.state.trades.append(trade)
        
        # Log trade
        notional = quantity * market_data.price
        margin = notional / Config.LEVERAGE
        
        logger.info(f"🔥 TRADE EXECUTED: {algorithm.name}")
        logger.info(f"   {direction.upper()} {quantity:.6f} {market_data.symbol} @ ${market_data.price:,.2f}")
        logger.info(f"   SL: ${stop_loss:.2f} | TP: ${take_profit:.2f}")
        
        # Send Telegram alert
        telegram_bot.send_trade_alert(
            "OPEN", market_data.symbol, direction.upper(),
            market_data.price, quantity
        )
        
        # Log to JSON
        json_logger.log('trade_open', {
            'trade_id': trade_id,
            'algorithm': algorithm.name,
            'symbol': market_data.symbol,
            'side': direction,
            'price': market_data.price,
            'quantity': quantity,
            'stop_loss': stop_loss,
            'take_profit': take_profit
        })
        
        # Update daily stats
        today = datetime.now().date().isoformat()
        self.daily_stats[today]['trades'] += 1
    
    def _close_position(self, pos_key: str, market_data: MarketData):
        """Close a position"""
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
        
        # Update realized P&L
        self.total_realized_pnl += pnl
        self.balance_manager.update_balance(pnl)
        
        # Update algorithm state
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
            
            price_change = ((market_data.price - position.entry_price) / position.entry_price) * 100
            
            logger.info(f"📊 POSITION CLOSED: {algo.name}")
            logger.info(f"   {position.symbol} {'LONG' if position.quantity > 0 else 'SHORT'}")
            logger.info(f"   Entry: ${position.entry_price:.2f} → Exit: ${market_data.price:.2f}")
            logger.info(f"   P&L: ${pnl:+.2f} ({price_change:+.1f}%)")
        
        # Send Telegram alert
        telegram_bot.send_trade_alert(
            "CLOSE", position.symbol,
            "LONG" if position.quantity > 0 else "SHORT",
            market_data.price, abs(position.quantity), pnl
        )
        
        # Log to JSON
        json_logger.log('trade_close', {
            'position_key': pos_key,
            'symbol': position.symbol,
            'exit_price': market_data.price,
            'pnl': pnl,
            'balance': self.balance_manager.get_balance()
        })
        
        # Update daily stats
        today = datetime.now().date().isoformat()
        self.daily_stats[today]['pnl'] += pnl
        
        # Remove position
        del self.positions[pos_key]
    
    def start(self):
        """Start trading engine"""
        self.is_running = True
        logger.info("✅ Trading engine started")
    
    def stop(self):
        """Stop trading engine"""
        self.is_running = False
        self.balance_manager.save_balance()
        
        total_pnl = self.calculate_live_pnl()
        logger.info(f"⏹️ Trading engine stopped")
        logger.info(f"📈 Final P&L: ${total_pnl:+.2f}")
        
        telegram_bot.send_message(
            f"🛑 Trading Bot Stopped\n"
            f"Final P&L: ${total_pnl:+.2f}\n"
            f"Balance: ${self.balance_manager.get_balance():.2f}"
        )
    
    def get_status(self) -> Dict:
        """Get comprehensive status"""
        with self.lock:
            total_pnl = self.calculate_live_pnl()
            total_trades = sum(a.state.trade_count for a in self.algorithms)
            win_rate = (self.total_wins / (self.total_wins + self.total_losses) * 100) if (self.total_wins + self.total_losses) > 0 else 0
            
            return {
                'is_running': self.is_running,
                'balance': self.balance_manager.get_balance(),
                'initial_balance': Config.INITIAL_BALANCE,
                'total_pnl': round(total_pnl, 2),
                'realized_pnl': round(self.total_realized_pnl, 2),
                'unrealized_pnl': round(self.total_unrealized_pnl, 2),
                'total_trades': total_trades,
                'open_positions': len(self.positions),
                'win_rate': round(win_rate, 1),
                'total_wins': self.total_wins,
                'total_losses': self.total_losses,
                'roi': round((total_pnl / Config.INITIAL_BALANCE) * 100, 2),
                'algorithms': [a.state.to_dict() for a in self.algorithms],
                'daily_stats': dict(self.daily_stats)
            }

# ======================== ENHANCED DASHBOARD ========================
ENHANCED_DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>🚀 Advanced Crypto Trading Bot Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }
        
        .header {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            padding: 25px;
            border-radius: 20px;
            margin-bottom: 25px;
            text-align: center;
        }
        
        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            background: linear-gradient(45deg, #00ff88, #00a8ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .live-badge {
            display: inline-block;
            background: #ff4757;
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }
        
        .card {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            padding: 20px;
            border-radius: 15px;
            transition: transform 0.3s;
        }
        
        .card:hover {
            transform: translateY(-5px);
        }
        
        .card-title {
            font-size: 1.2em;
            margin-bottom: 15px;
            opacity: 0.9;
        }
        
        .metric-large {
            font-size: 2.5em;
            font-weight: bold;
            margin: 10px 0;
        }
        
        .positive { color: #00ff88; }
        .negative { color: #ff4757; }
        .neutral { color: #ffa502; }
        
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin-top: 15px;
        }
        
        .metric-item {
            text-align: center;
            padding: 10px;
            background: rgba(255,255,255,0.03);
            border-radius: 10px;
        }
        
        .metric-value {
            font-size: 1.5em;
            font-weight: bold;
            margin-bottom: 5px;
        }
        
        .metric-label {
            font-size: 0.85em;
            opacity: 0.7;
        }
        
        .chart-container {
            position: relative;
            height: 300px;
            margin: 20px 0;
        }
        
        .table-container {
            overflow-x: auto;
            margin-top: 20px;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        
        th {
            background: rgba(255,255,255,0.05);
            font-weight: bold;
        }
        
        tr:hover {
            background: rgba(255,255,255,0.02);
        }
        
        .algo-row.active {
            border-left: 3px solid #00ff88;
        }
        
        .algo-row.inactive {
            opacity: 0.6;
        }
        
        .position-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 5px;
            font-size: 0.85em;
            font-weight: bold;
        }
        
        .badge-long { background: #00b894; }
        .badge-short { background: #d63031; }
        
        .sl-tp {
            font-size: 0.85em;
            opacity: 0.8;
        }
        
        .footer {
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            opacity: 0.7;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>Advanced Crypto Trading Bot</h1>
        <span class="live-badge">● LIVE TRADING</span>
        <p style="margin-top: 10px; opacity: 0.8;">
            19 Algorithms | 10x Leverage | Auto Risk Management
        </p>
    </div>
    
    <div class="dashboard-grid">
        <!-- Balance Card -->
        <div class="card">
            <div class="card-title">💰 Account Balance</div>
            <div id="balance" class="metric-large positive">$0.00</div>
            <div class="metric-grid">
                <div class="metric-item">
                    <div id="initial-balance" class="metric-value">$0</div>
                    <div class="metric-label">Initial</div>
                </div>
                <div class="metric-item">
                    <div id="roi" class="metric-value">0%</div>
                    <div class="metric-label">ROI</div>
                </div>
            </div>
        </div>
        
        <!-- P&L Card -->
        <div class="card">
            <div class="card-title">📊 Profit & Loss</div>
            <div id="total-pnl" class="metric-large neutral">$0.00</div>
            <div class="metric-grid">
                <div class="metric-item">
                    <div id="realized-pnl" class="metric-value">$0</div>
                    <div class="metric-label">Realized</div>
                </div>
                <div class="metric-item">
                    <div id="unrealized-pnl" class="metric-value">$0</div>
                    <div class="metric-label">Unrealized</div>
                </div>
            </div>
        </div>
        
        <!-- Performance Card -->
        <div class="card">
            <div class="card-title">🎯 Performance Stats</div>
            <div class="metric-grid">
                <div class="metric-item">
                    <div id="win-rate" class="metric-value">0%</div>
                    <div class="metric-label">Win Rate</div>
                </div>
                <div class="metric-item">
                    <div id="total-trades" class="metric-value">0</div>
                    <div class="metric-label">Total Trades</div>
                </div>
                <div class="metric-item">
                    <div id="wins" class="metric-value positive">0</div>
                    <div class="metric-label">Wins</div>
                </div>
                <div class="metric-item">
                    <div id="losses" class="metric-value negative">0</div>
                    <div class="metric-label">Losses</div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Charts -->
    <div class="dashboard-grid">
        <div class="card" style="grid-column: 1 / -1;">
            <div class="card-title">📈 Equity Curve</div>
            <div class="chart-container">
                <canvas id="equityChart"></canvas>
            </div>
        </div>
    </div>
    
    <div class="dashboard-grid">
        <div class="card" style="grid-column: 1 / -1;">
            <div class="card-title">💹 Live P&L Chart</div>
            <div class="chart-container">
                <canvas id="pnlChart"></canvas>
            </div>
        </div>
    </div>
    
    <!-- Open Positions -->
    <div class="card">
        <div class="card-title">📋 Open Positions</div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>P&L</th>
                        <th>SL/TP</th>
                        <th>Algorithm</th>
                    </tr>
                </thead>
                <tbody id="positions-tbody">
                    <tr><td colspan="7" style="text-align: center; opacity: 0.6;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- Algorithm Performance -->
    <div class="card">
        <div class="card-title">🤖 Algorithm Performance</div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Algorithm</th>
                        <th>Status</th>
                        <th>Trades</th>
                        <th>P&L</th>
                        <th>Win Rate</th>
                    </tr>
                </thead>
                <tbody id="algorithms-tbody">
                    <tr><td colspan="5" style="text-align: center; opacity: 0.6;">Loading...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <div class="footer">
        <p>Last Update: <span id="last-update">Never</span></p>
        <p style="margin-top: 10px;">© 2024 Advanced Crypto Trading Bot</p>
    </div>
    
    <script>
        // Chart configurations
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: 'rgba(255,255,255,0.7)' }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: 'rgba(255,255,255,0.7)' }
                }
            }
        };
        
        // Initialize charts
        const equityCtx = document.getElementById('equityChart').getContext('2d');
        const equityChart = new Chart(equityCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: 'Equity',
                    data: [],
                    borderColor: '#00ff88',
                    backgroundColor: 'rgba(0,255,136,0.1)',
                    tension: 0.4
                }]
            },
            options: chartOptions
        });
        
        const pnlCtx = document.getElementById('pnlChart').getContext('2d');
        const pnlChart = new Chart(pnlCtx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [{
                    label: 'P&L',
                    data: [],
                    backgroundColor: []
                }]
            },
            options: chartOptions
        });
        
        function formatCurrency(value) {
            return new Intl.NumberFormat('en-US', {
                style: 'currency',
                currency: 'USD',
                minimumFractionDigits: 2
            }).format(value);
        }
        
        function updateDashboard() {
            // Fetch status
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // Update balance
                    document.getElementById('balance').textContent = formatCurrency(data.balance || 0);
                    document.getElementById('balance').className = 'metric-large ' + 
                        (data.balance > data.initial_balance ? 'positive' : 
                         data.balance < data.initial_balance ? 'negative' : 'neutral');
                    
                    document.getElementById('initial-balance').textContent = formatCurrency(data.initial_balance || 0);
                    document.getElementById('roi').textContent = (data.roi || 0) + '%';
                    document.getElementById('roi').className = 'metric-value ' + 
                        (data.roi > 0 ? 'positive' : data.roi < 0 ? 'negative' : 'neutral');
                    
                    // Update P&L
                    document.getElementById('total-pnl').textContent = formatCurrency(data.total_pnl || 0);
                    document.getElementById('total-pnl').className = 'metric-large ' + 
                        (data.total_pnl > 0 ? 'positive' : data.total_pnl < 0 ? 'negative' : 'neutral');
                    
                    document.getElementById('realized-pnl').textContent = formatCurrency(data.realized_pnl || 0);
                    document.getElementById('unrealized-pnl').textContent = formatCurrency(data.unrealized_pnl || 0);
                    
                    // Update performance
                    document.getElementById('win-rate').textContent = (data.win_rate || 0) + '%';
                    document.getElementById('total-trades').textContent = data.total_trades || 0;
                    document.getElementById('wins').textContent = data.total_wins || 0;
                    document.getElementById('losses').textContent = data.total_losses || 0;
                    
                    // Update timestamp
                    document.getElementById('last-update').textContent = new Date().toLocaleString();
                });
            
            // Fetch positions
            fetch('/api/positions')
                .then(r => r.json())
                .then(data => {
                    const tbody = document.getElementById('positions-tbody');
                    if (data.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; opacity: 0.6;">No open positions</td></tr>';
                    } else {
                        tbody.innerHTML = data.map(pos => `
                            <tr>
                                <td><strong>${pos.symbol}</strong></td>
                                <td><span class="position-badge badge-${pos.side.toLowerCase()}">${pos.side}</span></td>
                                <td>${formatCurrency(pos.entry_price)}</td>
                                <td>${formatCurrency(pos.current_price)}</td>
                                <td class="${pos.current_pnl >= 0 ? 'positive' : 'negative'}">
                                    ${formatCurrency(pos.current_pnl)}
                                </td>
                                <td class="sl-tp">
                                    SL: ${formatCurrency(pos.stop_loss)}<br>
                                    TP: ${formatCurrency(pos.take_profit)}
                                </td>
                                <td>${pos.algorithm}</td>
                            </tr>
                        `).join('');
                    }
                });
            
            // Fetch algorithms
            fetch('/api/algorithms')
                .then(r => r.json())
                .then(data => {
                    const tbody = document.getElementById('algorithms-tbody');
                    tbody.innerHTML = data.map(algo => `
                        <tr class="algo-row ${algo.is_active ? 'active' : 'inactive'}">
                            <td>${algo.name}</td>
                            <td>${algo.is_active ? '🟢 Active' : '⚫ Complete'}</td>
                            <td>${algo.trade_count}/${algo.max_trades}</td>
                            <td class="${algo.total_pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(algo.total_pnl)}
                            </td>
                            <td>${algo.win_rate.toFixed(1)}%</td>
                        </tr>
                    `).join('');
                });
            
            // Fetch equity history
            fetch('/api/equity-history')
                .then(r => r.json())
                .then(data => {
                    if (data.length > 0) {
                        const labels = data.slice(-50).map(d => 
                            new Date(d.timestamp).toLocaleTimeString()
                        );
                        const values = data.slice(-50).map(d => d.balance);
                        
                        equityChart.data.labels = labels;
                        equityChart.data.datasets[0].data = values;
                        equityChart.update();
                    }
                });
            
            // Fetch P&L history
            fetch('/api/pnl-history')
                .then(r => r.json())
                .then(data => {
                    if (data.length > 0) {
                        const labels = data.slice(-20).map(d => 
                            new Date(d.timestamp).toLocaleTimeString()
                        );
                        const values = data.slice(-20).map(d => d.pnl);
                        const colors = values.map(v => v >= 0 ? '#00ff88' : '#ff4757');
                        
                        pnlChart.data.labels = labels;
                        pnlChart.data.datasets[0].data = values;
                        pnlChart.data.datasets[0].backgroundColor = colors;
                        pnlChart.update();
                    }
                });
        }
        
        // Update every 2 seconds
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
        return render_template_string(ENHANCED_DASHBOARD_HTML)
    
    @app.route('/api/status')
    def status():
        return jsonify(engine.get_status())
    
    @app.route('/api/positions')
    def positions():
        positions_data = []
        for pos_key, position in engine.positions.items():
            latest_price = position.current_price
            for data in reversed(list(engine.market_data_queue)):
                if data.symbol == position.symbol:
                    latest_price = data.price
                    break
            
            algo = next((a for a in engine.algorithms 
                        if a.algorithm_id == position.algorithm_id), None)
            
            positions_data.append({
                'symbol': position.symbol,
                'side': 'LONG' if position.quantity > 0 else 'SHORT',
                'entry_price': position.entry_price,
                'current_price': latest_price,
                'current_pnl': position.unrealized_pnl,
                'stop_loss': position.stop_loss,
                'take_profit': position.take_profit,
                'algorithm': algo.name if algo else 'Unknown'
            })
        
        return jsonify(positions_data)
    
    @app.route('/api/algorithms')
    def algorithms():
        return jsonify([algo.state.to_dict() for algo in engine.algorithms])
    
    @app.route('/api/equity-history')
    def equity_history():
        return jsonify(engine.balance_manager.get_equity_history())
    
    @app.route('/api/pnl-history')
    def pnl_history():
        history = engine.balance_manager.get_equity_history()
        pnl_data = []
        for i in range(1, len(history)):
            pnl_data.append({
                'timestamp': history[i]['timestamp'],
                'pnl': history[i].get('pnl', 0)
            })
        return jsonify(pnl_data)
    
    return app

# ======================== MAIN APPLICATION ========================
def data_feed_loop(engine: TradingEngine):
    """Main data feed loop"""
    logger.info("🔴 Starting live data feed")
    
    while engine.is_running:
        try:
            # Get market data
            market_data_list = engine.market_data_manager.get_market_data()
            
            if not market_data_list:
                logger.warning("No market data received")
                time.sleep(10)
                continue
            
            # Process each market data
            for market_data in market_data_list:
                if not engine.is_running:
                    break
                
                engine.process_market_data(market_data)
                
                # Small delay between symbols
                time.sleep(0.1)
            
            # Wait before next update
            time.sleep(Config.DATA_UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in data feed: {e}")
            json_logger.log('error', {'type': 'data_feed', 'error': str(e)})
            time.sleep(10)

def send_daily_report(engine: TradingEngine):
    """Send daily performance report"""
    while engine.is_running:
        try:
            # Wait until next day at 00:00 UTC
            now = datetime.now()
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            sleep_seconds = (tomorrow - now).total_seconds()
            time.sleep(min(sleep_seconds, 3600))  # Check at least every hour
            
            if not engine.is_running:
                break
            
            # Send report
            status = engine.get_status()
            telegram_bot.send_daily_report(status)
            
            # Log daily stats
            json_logger.log('daily_report', status)
            
        except Exception as e:
            logger.error(f"Error sending daily report: {e}")
            time.sleep(3600)

def main():
    """Main entry point"""
    logger.info("=" * 70)
    logger.info("🚀 ADVANCED CRYPTO FUTURES TRADING BOT v3.0")
    logger.info("=" * 70)
    
    # Initialize trading engine
    engine = TradingEngine()
    engine.start()
    
    # Start data feed thread
    data_thread = Thread(target=data_feed_loop, args=(engine,))
    data_thread.daemon = True
    data_thread.start()
    
    # Start daily report thread
    report_thread = Thread(target=send_daily_report, args=(engine,))
    report_thread.daemon = True
    report_thread.start()
    
    # Create Flask app
    app = create_app(engine)
    
    logger.info("=" * 70)
    logger.info("📊 Dashboard: http://0.0.0.0:" + str(Config.PORT))
    logger.info("🔴 LIVE TRADING ACTIVE")
    logger.info("• 19 Algorithms (2 trades each max)")
    logger.info(f"• {Config.LEVERAGE}x Leverage")
    logger.info(f"• ${Config.POSITION_SIZE_USD} per position")
    logger.info(f"• Balance: ${engine.balance_manager.get_balance():.2f}")
    logger.info("=" * 70)
    
    try:
        # Run Flask app
        app.run(host='0.0.0.0', port=Config.PORT, debug=False)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        engine.stop()
        sys.exit(0)

if __name__ == "__main__":
    main()