#!/usr/bin/env python3
"""
Professional Crypto Futures Trading Bot v4.0
Binance Global Only - Fixed PnL - Extensive Dashboard
Single file implementation with all requirements
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
    
    # Binance Rate Limits
    BINANCE_MAX_REQUESTS_PER_MIN = 1200
    BINANCE_WEIGHT_PER_MIN = 6000

# Create data directory if not exists
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

# ======================== TELEGRAM ========================
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
            data = {'chat_id': self.chat_id, 'text': message, 'parse_mode': parse_mode}
            response = requests.post(url, data=data, timeout=5)
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

telegram_bot = TelegramBot(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

# ======================== BINANCE RATE LIMITER ========================
class BinanceRateLimiter:
    """Advanced rate limiter for Binance Global API"""
    def __init__(self):
        self.requests_per_min = Config.BINANCE_MAX_REQUESTS_PER_MIN
        self.weight_per_min = Config.BINANCE_WEIGHT_PER_MIN
        self.requests = deque()
        self.weights = deque()
        self.lock = Lock()
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.last_reset = time.time()
        
    def can_make_request(self, weight: int = 1) -> bool:
        """Check if request can be made"""
        with self.lock:
            now = time.time()
            
            # Remove old requests/weights (older than 1 minute)
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            while self.weights and self.weights[0][0] < now - 60:
                self.weights.popleft()
            
            # Calculate current usage
            current_requests = len(self.requests)
            current_weight = sum(w[1] for w in self.weights)
            
            # Check limits
            if current_requests < self.requests_per_min and current_weight + weight <= self.weight_per_min:
                self.requests.append(now)
                self.weights.append((now, weight))
                self.total_requests += 1
                return True
            return False
    
    def wait_if_needed(self, weight: int = 1):
        """Wait if rate limit exceeded"""
        while not self.can_make_request(weight):
            time.sleep(0.1)
    
    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        with self.lock:
            now = time.time()
            # Clean old entries
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            while self.weights and self.weights[0][0] < now - 60:
                self.weights.popleft()
            
            current_requests = len(self.requests)
            current_weight = sum(w[1] for w in self.weights)
            
            return {
                'requests_used': current_requests,
                'requests_limit': self.requests_per_min,
                'weight_used': current_weight,
                'weight_limit': self.weight_per_min,
                'total_requests': self.total_requests,
                'success_rate': (self.successful_requests / max(self.total_requests, 1)) * 100,
                'requests_remaining': self.requests_per_min - current_requests,
                'reset_in': 60 - (now - self.requests[0]) if self.requests else 60
            }

# ======================== DATA CACHE ========================
class MarketDataCache:
    """Smart caching for market data"""
    def __init__(self, ttl: int = 5):
        self.cache = {}
        self.ttl = ttl
        self.lock = Lock()
        self.hits = 0
        self.misses = 0
        
    def get(self, key: str) -> Optional[Any]:
        """Get cached data if not expired"""
        with self.lock:
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    self.hits += 1
                    return data
                del self.cache[key]
            self.misses += 1
        return None
    
    def set(self, key: str, value: Any):
        """Set cache data"""
        with self.lock:
            self.cache[key] = (value, time.time())
    
    def get_stats(self) -> Dict:
        """Get cache statistics"""
        total = self.hits + self.misses
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': (self.hits / max(total, 1)) * 100,
            'cached_items': len(self.cache)
        }

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
        """Load balance from disk"""
        try:
            if os.path.exists(self.balance_file):
                with open(self.balance_file, 'r') as f:
                    data = json.load(f)
                    logger.info(f"Loaded balance from disk: ${data.get('balance', 0):.2f}")
                    return data
        except Exception as e:
            logger.error(f"Failed to load balance: {e}")
        
        # Return default if no file exists
        return {
            'balance': self.initial_balance,
            'equity_history': [],
            'max_drawdown': 0,
            'peak_balance': self.initial_balance,
            'created_at': datetime.now().isoformat()
        }
    
    def save_balance(self):
        """Save balance to disk"""
        with self.lock:
            try:
                self.balance_data = {
                    'balance': self.balance,
                    'equity_history': self.equity_history[-1000:],  # Keep last 1000 points
                    'max_drawdown': self.max_drawdown,
                    'peak_balance': self.peak_balance,
                    'last_updated': datetime.now().isoformat()
                }
                
                with open(self.balance_file, 'w') as f:
                    json.dump(self.balance_data, f, indent=2)
                    
            except Exception as e:
                logger.error(f"Failed to save balance: {e}")
    
    def update_balance(self, pnl: float):
        """Update balance with P&L"""
        with self.lock:
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

# ======================== BINANCE MARKET DATA ========================
class BinanceMarketData:
    """Binance Global market data provider with smart caching"""
    def __init__(self):
        self.base_url = "https://api.binance.com/api/v3"
        self.rate_limiter = BinanceRateLimiter()
        self.cache = MarketDataCache(ttl=5)
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                       'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT']
        self.last_request_time = {}
        self.circuit_breaker_active = False
        self.consecutive_failures = 0
        
    def batch_get_market_data(self) -> List[MarketData]:
        """Get market data for all symbols in one batch request"""
        # Check cache first
        cached_data = self.cache.get('batch_market_data')
        if cached_data:
            return cached_data
        
        # Check circuit breaker
        if self.circuit_breaker_active:
            if self.consecutive_failures < 3:
                self.circuit_breaker_active = False
            else:
                return []
        
        try:
            # Use batch endpoint for efficiency (weight: 40)
            self.rate_limiter.wait_if_needed(weight=40)
            
            url = f"{self.base_url}/ticker/24hr"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                all_data = response.json()
                market_data_list = []
                
                # Filter for our symbols
                for ticker in all_data:
                    if ticker['symbol'] in self.symbols:
                        market_data = MarketData(
                            symbol=ticker['symbol'].replace('USDT', '-PERP'),
                            price=float(ticker['lastPrice']),
                            volume=float(ticker['volume']),
                            timestamp=datetime.now(),
                            bid=float(ticker['bidPrice']),
                            ask=float(ticker['askPrice']),
                            open=float(ticker['openPrice']),
                            high=float(ticker['highPrice']),
                            low=float(ticker['lowPrice']),
                            close=float(ticker['lastPrice'])
                        )
                        market_data_list.append(market_data)
                
                # Cache the data
                self.cache.set('batch_market_data', market_data_list)
                self.rate_limiter.successful_requests += 1
                self.consecutive_failures = 0
                
                return market_data_list
            else:
                self.rate_limiter.failed_requests += 1
                self.consecutive_failures += 1
                logger.warning(f"Binance API error: {response.status_code}")
                
        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.circuit_breaker_active = True
            logger.error(f"Binance error: {e}")
        
        return []
    
    def get_stats(self) -> Dict:
        """Get market data provider statistics"""
        return {
            'cache_stats': self.cache.get_stats(),
            'rate_limiter_stats': self.rate_limiter.get_stats(),
            'circuit_breaker': self.circuit_breaker_active,
            'consecutive_failures': self.consecutive_failures
        }

# ======================== TRADING ENGINE ========================
class TradingAlgorithm:
    """Base trading algorithm - NO CHANGES TO LOGIC"""
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
    """Main trading engine - LOGIC UNCHANGED, PNL FIXES APPLIED"""
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.closed_trades = []  # Track closed trades separately
        self.market_data_queue = deque(maxlen=1000)
        self.lock = Lock()
        self.balance_manager = BalanceManager(Config.INITIAL_BALANCE)
        self.market_data_provider = BinanceMarketData()
        
        # Fixed P&L tracking
        self.total_realized_pnl = 0.0  # Sum of closed trades
        self.total_unrealized_pnl = 0.0  # Sum of open positions
        
        # Statistics
        self.total_wins = 0
        self.total_losses = 0
        self.daily_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0.0})
        
        # Initialize algorithms
        self._create_algorithms()
        
        logger.info(f"✅ Trading Engine initialized with {len(self.algorithms)} algorithms")
        logger.info(f"💰 Starting balance: ${self.balance_manager.get_balance():.2f}")
        
        telegram_bot.send_message(
            "🚀 Trading Bot Started\n" + 
            f"Balance: ${self.balance_manager.get_balance():.2f}\n" +
            f"Algorithms: {len(self.algorithms)}"
        )
    
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
        """FIXED: Calculate accurate P&L"""
        # Calculate unrealized P&L from open positions
        total_unrealized = 0.0
        
        for position in self.positions.values():
            # Get latest price for this position
            latest_price = position.current_price
            
            # Find most recent price from market data
            for data in reversed(list(self.market_data_queue)):
                if data.symbol == position.symbol:
                    latest_price = data.price
                    position.current_price = latest_price  # Update position's current price
                    break
            
            # Calculate unrealized P&L
            if position.quantity > 0:  # Long position
                position.unrealized_pnl = (latest_price - position.entry_price) * position.quantity
            else:  # Short position
                position.unrealized_pnl = (position.entry_price - latest_price) * abs(position.quantity)
            
            total_unrealized += position.unrealized_pnl
        
        # Calculate realized P&L from closed trades
        total_realized = sum(trade.pnl for trade in self.closed_trades)
        
        self.total_unrealized_pnl = total_unrealized
        self.total_realized_pnl = total_realized
        
        return {
            'realized': total_realized,
            'unrealized': total_unrealized,
            'total': total_realized + total_unrealized
        }
    
    def process_market_data(self, market_data: MarketData):
        """Process incoming market data - UNCHANGED LOGIC"""
        if not self.is_running:
            return
        
        with self.lock:
            self.market_data_queue.append(market_data)
            
            # Update P&L with new prices
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
        """Check if positions should be closed - UNCHANGED"""
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
    
    def _execute_trade(self, algorithm: TradingAlgorithm, market_data: MarketData, direction: str):
        """Execute a new trade - UNCHANGED"""
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
        
        logger.info(f"🔥 TRADE EXECUTED: {algorithm.name}")
        logger.info(f"   {direction.upper()} {quantity:.6f} {market_data.symbol} @ ${market_data.price:,.2f}")
        
        telegram_bot.send_trade_alert(
            "OPEN", market_data.symbol, direction.upper(),
            market_data.price, quantity
        )
        
        # Update daily stats
        today = datetime.now().date().isoformat()
        self.daily_stats[today]['trades'] += 1
    
    def _close_position(self, pos_key: str, market_data: MarketData):
        """Close a position - FIXED PNL CALCULATION"""
        position = self.positions.get(pos_key)
        if not position:
            return
        
        # Calculate P&L correctly
        if position.quantity > 0:  # Long
            pnl = (market_data.price - position.entry_price) * position.quantity
        else:  # Short
            pnl = (position.entry_price - market_data.price) * abs(position.quantity)
        
        # Update trades
        for trade in position.trades:
            trade.close(market_data.price)
            trade.pnl = pnl
            # Move to closed trades list
            self.closed_trades.append(trade)
        
        # Update balance
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
            
            logger.info(f"📊 POSITION CLOSED: {algo.name}")
            logger.info(f"   P&L: ${pnl:+.2f}")
        
        telegram_bot.send_trade_alert(
            "CLOSE", position.symbol,
            "LONG" if position.quantity > 0 else "SHORT",
            market_data.price, abs(position.quantity), pnl
        )
        
        # Update daily stats
        today = datetime.now().date().isoformat()
        self.daily_stats[today]['pnl'] += pnl
        
        # Remove position
        del self.positions[pos_key]
        
        # Recalculate P&L
        self.calculate_live_pnl()
    
    def start(self):
        """Start trading engine"""
        self.is_running = True
        logger.info("✅ Trading engine started")
    
    def stop(self):
        """Stop trading engine"""
        self.is_running = False
        self.balance_manager.save_balance()
        
        pnl_data = self.calculate_live_pnl()
        logger.info(f"⏹️ Trading engine stopped")
        logger.info(f"📈 Final P&L: ${pnl_data['total']:+.2f}")
    
    def get_comprehensive_status(self) -> Dict:
        """Get comprehensive status for dashboard"""
        with self.lock:
            pnl_data = self.calculate_live_pnl()
            balance_stats = self.balance_manager.get_stats()
            
            total_trades = sum(a.state.trade_count for a in self.algorithms)
            win_rate = (self.total_wins / (self.total_wins + self.total_losses) * 100) if (self.total_wins + self.total_losses) > 0 else 0
            
            # Get open positions data
            open_positions = []
            for pos_key, position in self.positions.items():
                algo = next((a for a in self.algorithms if a.algorithm_id == position.algorithm_id), None)
                open_positions.append({
                    'symbol': position.symbol,
                    'side': 'LONG' if position.quantity > 0 else 'SHORT',
                    'quantity': abs(position.quantity),
                    'entry_price': position.entry_price,
                    'current_price': position.current_price,
                    'unrealized_pnl': position.unrealized_pnl,
                    'pnl_percent': ((position.current_price - position.entry_price) / position.entry_price * 100) if position.quantity > 0 else ((position.entry_price - position.current_price) / position.entry_price * 100),
                    'stop_loss': position.stop_loss,
                    'take_profit': position.take_profit,
                    'algorithm': algo.name if algo else 'Unknown',
                    'leverage': Config.LEVERAGE
                })
            
            # Get closed trades history
            trade_history = []
            for trade in self.closed_trades[-50:]:  # Last 50 trades
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
            
            # System stats
            market_stats = self.market_data_provider.get_stats()
            
            return {
                # Balance and PnL
                'balance': balance_stats['balance'],
                'initial_balance': balance_stats['initial_balance'],
                'total_pnl': pnl_data['total'],
                'realized_pnl': pnl_data['realized'],
                'unrealized_pnl': pnl_data['unrealized'],
                
                # Performance metrics
                'roi': ((balance_stats['balance'] - balance_stats['initial_balance']) / balance_stats['initial_balance'] * 100),
                'max_drawdown': balance_stats['max_drawdown'],
                'peak_balance': balance_stats['peak_balance'],
                'win_rate': win_rate,
                'total_wins': self.total_wins,
                'total_losses': self.total_losses,
                
                # Trading stats
                'total_trades': total_trades,
                'open_positions_count': len(self.positions),
                'closed_trades_count': len(self.closed_trades),
                
                # Detailed data
                'open_positions': open_positions,
                'trade_history': trade_history,
                'algorithms': [a.state.to_dict() for a in self.algorithms],
                'equity_history': balance_stats['equity_history'][-100:],  # Last 100 points
                'daily_stats': dict(self.daily_stats),
                
                # System stats
                'api_stats': market_stats['rate_limiter_stats'],
                'cache_stats': market_stats['cache_stats'],
                'circuit_breaker': market_stats['circuit_breaker'],
                
                # Metadata
                'is_running': self.is_running,
                'leverage': Config.LEVERAGE,
                'position_size': Config.POSITION_SIZE_USD
            }

# ======================== PROFESSIONAL DASHBOARD ========================
PROFESSIONAL_DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Professional Crypto Trading Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #0a0e27;
            color: #e4e4e7;
            line-height: 1.6;
        }
        
        /* Header */
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
        
        /* Main Container */
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        /* Grid Layout */
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
            transition: transform 0.2s;
        }
        
        .metric-card:hover {
            transform: translateY(-2px);
            border-color: #667eea;
        }
        
        .metric-label {
            font-size: 0.875rem;
            color: #9ca3af;
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        .metric-value {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
        }
        
        .metric-change {
            font-size: 0.875rem;
            display: flex;
            align-items: center;
            gap: 0.25rem;
        }
        
        .positive {
            color: #10b981;
        }
        
        .negative {
            color: #ef4444;
        }
        
        .neutral {
            color: #6b7280;
        }
        
        /* Chart Container */
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
        
        .chart-title {
            font-size: 1.25rem;
            font-weight: 600;
        }
        
        .chart-canvas {
            position: relative;
            height: 300px;
        }
        
        /* Tables */
        .table-container {
            background: #1a1f3a;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            border: 1px solid #2a3f5f;
            overflow-x: auto;
        }
        
        .table-header {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 1rem;
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
            border-bottom: 2px solid #2a3f5f;
        }
        
        td {
            padding: 0.75rem;
            border-bottom: 1px solid #2a3f5f;
            font-size: 0.875rem;
        }
        
        tr:hover {
            background: rgba(102, 126, 234, 0.1);
        }
        
        /* Position Badge */
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
        
        /* API Stats */
        .api-stats {
            display: flex;
            gap: 2rem;
            padding: 1rem;
            background: #0f1729;
            border-radius: 8px;
            margin-top: 1rem;
        }
        
        .api-stat {
            flex: 1;
        }
        
        .api-stat-label {
            font-size: 0.75rem;
            color: #6b7280;
            margin-bottom: 0.25rem;
        }
        
        .api-stat-value {
            font-size: 1.25rem;
            font-weight: 600;
        }
        
        /* Progress Bar */
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #2a3f5f;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 0.5rem;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea, #764ba2);
            transition: width 0.3s ease;
        }
        
        /* Two Column Layout */
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
        
        /* Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        
        ::-webkit-scrollbar-track {
            background: #1a1f3a;
        }
        
        ::-webkit-scrollbar-thumb {
            background: #667eea;
            border-radius: 4px;
        }
        
        /* Footer */
        .footer {
            text-align: center;
            padding: 2rem;
            color: #6b7280;
            border-top: 1px solid #2a3f5f;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div>
                <h1>Professional Crypto Trading Dashboard</h1>
                <p>Binance Global • 19 Algorithms • 10x Leverage</p>
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
                <div class="metric-label">Live Balance</div>
                <div id="balance" class="metric-value">$0.00</div>
                <div id="balance-change" class="metric-change neutral">
                    <span>0.00%</span> from initial
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Total P&L</div>
                <div id="total-pnl" class="metric-value">$0.00</div>
                <div class="metric-change">
                    <span id="realized-pnl" class="neutral">R: $0.00</span>
                    <span> | </span>
                    <span id="unrealized-pnl" class="neutral">U: $0.00</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">ROI %</div>
                <div id="roi" class="metric-value">0.00%</div>
                <div class="metric-change">
                    <span>Max DD: </span>
                    <span id="max-drawdown" class="negative">0.00%</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div id="win-rate" class="metric-value">0.00%</div>
                <div class="metric-change">
                    <span id="wins" class="positive">0W</span>
                    <span> / </span>
                    <span id="losses" class="negative">0L</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">Open Trades</div>
                <div id="open-trades" class="metric-value">0</div>
                <div class="metric-change">
                    <span>Closed: </span>
                    <span id="closed-trades">0</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-label">API Usage</div>
                <div id="api-usage" class="metric-value">0/1200</div>
                <div class="progress-bar">
                    <div id="api-progress" class="progress-fill" style="width: 0%"></div>
                </div>
            </div>
        </div>
        
        <!-- Charts -->
        <div class="two-column">
            <div class="chart-container">
                <div class="chart-header">
                    <h3 class="chart-title">Equity Curve</h3>
                    <span id="equity-last-update" class="neutral">Live</span>
                </div>
                <div class="chart-canvas">
                    <canvas id="equityChart"></canvas>
                </div>
            </div>
            
            <div class="chart-container">
                <div class="chart-header">
                    <h3 class="chart-title">Live P&L Chart</h3>
                    <span id="pnl-last-update" class="neutral">Live</span>
                </div>
                <div class="chart-canvas">
                    <canvas id="pnlChart"></canvas>
                </div>
            </div>
        </div>
        
        <!-- Open Positions -->
        <div class="table-container">
            <h3 class="table-header">Open Positions</h3>
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
                        <th>SL</th>
                        <th>TP</th>
                        <th>Algorithm</th>
                        <th>Leverage</th>
                    </tr>
                </thead>
                <tbody id="positions-tbody">
                    <tr><td colspan="11" style="text-align: center; padding: 2rem;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- Algorithm Performance -->
        <div class="table-container">
            <h3 class="table-header">Algorithm Performance</h3>
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
                        <th>Last Trade</th>
                    </tr>
                </thead>
                <tbody id="algorithms-tbody">
                    <tr><td colspan="8" style="text-align: center; padding: 2rem;">Loading...</td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- Trade History -->
        <div class="table-container">
            <h3 class="table-header">Trade History (Last 20)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>P&L</th>
                        <th>Algorithm</th>
                    </tr>
                </thead>
                <tbody id="history-tbody">
                    <tr><td colspan="7" style="text-align: center; padding: 2rem;">No trade history</td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- System Stats -->
        <div class="chart-container">
            <h3 class="chart-title">System Statistics</h3>
            <div class="api-stats">
                <div class="api-stat">
                    <div class="api-stat-label">API Requests/Min</div>
                    <div id="api-requests" class="api-stat-value">0</div>
                </div>
                <div class="api-stat">
                    <div class="api-stat-label">Success Rate</div>
                    <div id="api-success" class="api-stat-value">100%</div>
                </div>
                <div class="api-stat">
                    <div class="api-stat-label">Cache Hit Rate</div>
                    <div id="cache-hit" class="api-stat-value">0%</div>
                </div>
                <div class="api-stat">
                    <div class="api-stat-label">Circuit Breaker</div>
                    <div id="circuit-breaker" class="api-stat-value positive">CLOSED</div>
                </div>
                <div class="api-stat">
                    <div class="api-stat-label">Reset In</div>
                    <div id="api-reset" class="api-stat-value">60s</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="footer">
        <p>Last Update: <span id="last-update">Never</span></p>
        <p>© 2024 Professional Crypto Trading Bot • Binance Global</p>
    </div>
    
    <script>
        // Chart configurations
        const chartConfig = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#6b7280' }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#6b7280' }
                }
            }
        };
        
        // Initialize Equity Chart
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
                    tension: 0.4,
                    fill: true
                }]
            },
            options: chartConfig
        });
        
        // Initialize P&L Chart
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
            options: chartConfig
        });
        
        // Format functions
        function formatCurrency(value) {
            const formatted = new Intl.NumberFormat('en-US', {
                style: 'currency',
                currency: 'USD',
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            }).format(value);
            return formatted;
        }
        
        function formatPercent(value) {
            return value.toFixed(2) + '%';
        }
        
        function formatTime(isoString) {
            if (!isoString) return '-';
            return new Date(isoString).toLocaleString();
        }
        
        function updateMetricColor(element, value) {
            element.classList.remove('positive', 'negative', 'neutral');
            if (value > 0) {
                element.classList.add('positive');
            } else if (value < 0) {
                element.classList.add('negative');
            } else {
                element.classList.add('neutral');
            }
        }
        
        // Update dashboard
        async function updateDashboard() {
            try {
                const response = await fetch('/api/comprehensive-status');
                const data = await response.json();
                
                // Update balance
                const balanceEl = document.getElementById('balance');
                balanceEl.textContent = formatCurrency(data.balance || 0);
                updateMetricColor(balanceEl, data.balance - data.initial_balance);
                
                const balanceChange = ((data.balance - data.initial_balance) / data.initial_balance * 100);
                document.getElementById('balance-change').innerHTML = 
                    `<span class="${balanceChange >= 0 ? 'positive' : 'negative'}">${balanceChange >= 0 ? '+' : ''}${formatPercent(balanceChange)}</span> from initial`;
                
                // Update P&L
                const totalPnlEl = document.getElementById('total-pnl');
                totalPnlEl.textContent = formatCurrency(data.total_pnl || 0);
                updateMetricColor(totalPnlEl, data.total_pnl);
                
                const realizedEl = document.getElementById('realized-pnl');
                realizedEl.textContent = 'R: ' + formatCurrency(data.realized_pnl || 0);
                updateMetricColor(realizedEl, data.realized_pnl);
                
                const unrealizedEl = document.getElementById('unrealized-pnl');
                unrealizedEl.textContent = 'U: ' + formatCurrency(data.unrealized_pnl || 0);
                updateMetricColor(unrealizedEl, data.unrealized_pnl);
                
                // Update ROI and Drawdown
                const roiEl = document.getElementById('roi');
                roiEl.textContent = formatPercent(data.roi || 0);
                updateMetricColor(roiEl, data.roi);
                
                document.getElementById('max-drawdown').textContent = formatPercent(data.max_drawdown || 0);
                
                // Update Win Rate
                document.getElementById('win-rate').textContent = formatPercent(data.win_rate || 0);
                document.getElementById('wins').textContent = (data.total_wins || 0) + 'W';
                document.getElementById('losses').textContent = (data.total_losses || 0) + 'L';
                
                // Update Trade Counts
                document.getElementById('open-trades').textContent = data.open_positions_count || 0;
                document.getElementById('closed-trades').textContent = data.closed_trades_count || 0;
                
                // Update API Usage
                const apiUsage = data.api_stats || {};
                document.getElementById('api-usage').textContent = 
                    `${apiUsage.requests_used || 0}/${apiUsage.requests_limit || 1200}`;
                const apiPercent = ((apiUsage.requests_used || 0) / (apiUsage.requests_limit || 1200)) * 100;
                document.getElementById('api-progress').style.width = apiPercent + '%';
                
                // Update System Stats
                document.getElementById('api-requests').textContent = apiUsage.requests_used || 0;
                document.getElementById('api-success').textContent = formatPercent(apiUsage.success_rate || 100);
                document.getElementById('cache-hit').textContent = formatPercent(data.cache_stats?.hit_rate || 0);
                document.getElementById('circuit-breaker').textContent = data.circuit_breaker ? 'OPEN' : 'CLOSED';
                document.getElementById('circuit-breaker').className = 'api-stat-value ' + (data.circuit_breaker ? 'negative' : 'positive');
                document.getElementById('api-reset').textContent = Math.round(apiUsage.reset_in || 60) + 's';
                
                // Update Open Positions Table
                const positionsTbody = document.getElementById('positions-tbody');
                if (data.open_positions && data.open_positions.length > 0) {
                    positionsTbody.innerHTML = data.open_positions.map(pos => `
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
                                ${formatPercent(pos.pnl_percent)}
                            </td>
                            <td>${formatCurrency(pos.stop_loss)}</td>
                            <td>${formatCurrency(pos.take_profit)}</td>
                            <td>${pos.algorithm}</td>
                            <td>${pos.leverage}x</td>
                        </tr>
                    `).join('');
                } else {
                    positionsTbody.innerHTML = '<tr><td colspan="11" style="text-align: center; padding: 2rem;">No open positions</td></tr>';
                }
                
                // Update Algorithm Performance Table
                const algorithmsTbody = document.getElementById('algorithms-tbody');
                if (data.algorithms && data.algorithms.length > 0) {
                    algorithmsTbody.innerHTML = data.algorithms.map(algo => `
                        <tr>
                            <td><strong>${algo.name}</strong></td>
                            <td>${algo.is_active ? '<span class="positive">Active</span>' : '<span class="neutral">Complete</span>'}</td>
                            <td>${algo.trade_count}/${algo.max_trades}</td>
                            <td class="positive">${algo.win_count}</td>
                            <td class="negative">${algo.loss_count}</td>
                            <td>${formatPercent(algo.win_rate)}</td>
                            <td class="${algo.total_pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(algo.total_pnl)}
                            </td>
                            <td>${algo.last_trade_time ? formatTime(algo.last_trade_time) : '-'}</td>
                        </tr>
                    `).join('');
                }
                
                // Update Trade History Table
                const historyTbody = document.getElementById('history-tbody');
                if (data.trade_history && data.trade_history.length > 0) {
                    historyTbody.innerHTML = data.trade_history.slice(0, 20).map(trade => `
                        <tr>
                            <td>${formatTime(trade.exit_time)}</td>
                            <td><strong>${trade.symbol}</strong></td>
                            <td>${trade.side.toUpperCase()}</td>
                            <td>${formatCurrency(trade.entry_price)}</td>
                            <td>${formatCurrency(trade.exit_price)}</td>
                            <td class="${trade.pnl >= 0 ? 'positive' : 'negative'}">
                                ${formatCurrency(trade.pnl)}
                            </td>
                            <td>${trade.algorithm}</td>
                        </tr>
                    `).join('');
                } else {
                    historyTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 2rem;">No trade history</td></tr>';
                }
                
                // Update Equity Chart
                if (data.equity_history && data.equity_history.length > 0) {
                    const labels = data.equity_history.map(h => 
                        new Date(h.timestamp).toLocaleTimeString()
                    );
                    const values = data.equity_history.map(h => h.balance);
                    
                    equityChart.data.labels = labels;
                    equityChart.data.datasets[0].data = values;
                    equityChart.update();
                    
                    document.getElementById('equity-last-update').textContent = 
                        new Date().toLocaleTimeString();
                }
                
                // Update P&L Chart
                if (data.equity_history && data.equity_history.length > 1) {
                    const pnlData = [];
                    const pnlColors = [];
                    const pnlLabels = [];
                    
                    for (let i = 1; i < Math.min(data.equity_history.length, 20); i++) {
                        const pnl = data.equity_history[i].pnl || 0;
                        pnlData.push(pnl);
                        pnlColors.push(pnl >= 0 ? 'rgba(16, 185, 129, 0.8)' : 'rgba(239, 68, 68, 0.8)');
                        pnlLabels.push(new Date(data.equity_history[i].timestamp).toLocaleTimeString());
                    }
                    
                    pnlChart.data.labels = pnlLabels;
                    pnlChart.data.datasets[0].data = pnlData;
                    pnlChart.data.datasets[0].backgroundColor = pnlColors;
                    pnlChart.update();
                    
                    document.getElementById('pnl-last-update').textContent = 
                        new Date().toLocaleTimeString();
                }
                
                // Update last update time
                document.getElementById('last-update').textContent = new Date().toLocaleString();
                
            } catch (error) {
                console.error('Error updating dashboard:', error);
            }
        }
        
        // Update every 2 seconds
        setInterval(updateDashboard, 2000);
        
        // Initial update
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
        return render_template_string(PROFESSIONAL_DASHBOARD_HTML)
    
    @app.route('/api/comprehensive-status')
    def comprehensive_status():
        """Get comprehensive status for dashboard"""
        return jsonify(engine.get_comprehensive_status())
    
    return app

# ======================== MAIN APPLICATION ========================
def data_feed_loop(engine: TradingEngine):
    """Main data feed loop using Binance Global"""
    logger.info("🔴 Starting Binance Global live data feed")
    
    while engine.is_running:
        try:
            # Get market data from Binance
            market_data_list = engine.market_data_provider.batch_get_market_data()
            
            if not market_data_list:
                logger.warning("No market data received from Binance")
                time.sleep(10)
                continue
            
            # Process each market data
            for market_data in market_data_list:
                if not engine.is_running:
                    break
                
                engine.process_market_data(market_data)
                
                # Small delay between processing
                time.sleep(0.05)
            
            # Wait before next update
            time.sleep(Config.DATA_UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in data feed: {e}")
            time.sleep(10)

def main():
    """Main entry point"""
    logger.info("=" * 70)
    logger.info("🚀 PROFESSIONAL CRYPTO FUTURES TRADING BOT v4.0")
    logger.info("📊 Using Binance Global (NOT Binance.US)")
    logger.info("=" * 70)
    
    # Initialize trading engine
    engine = TradingEngine()
    engine.start()
    
    # Start data feed thread
    data_thread = Thread(target=data_feed_loop, args=(engine,))
    data_thread.daemon = True
    data_thread.start()
    
    # Create Flask app
    app = create_app(engine)
    
    logger.info("=" * 70)
    logger.info(f"📊 Professional Dashboard: http://0.0.0.0:{Config.PORT}")
    logger.info("🔴 LIVE TRADING ACTIVE")
    logger.info(f"• 19 Algorithms ({Config.MAX_TRADES_PER_ALGO} trades each max)")
    logger.info(f"• {Config.LEVERAGE}x Leverage")
    logger.info(f"• ${Config.POSITION_SIZE_USD} per position")
    logger.info(f"• Balance: ${engine.balance_manager.get_balance():.2f}")
    logger.info("• Binance Global API with smart caching")
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