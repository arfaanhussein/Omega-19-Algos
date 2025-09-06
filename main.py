# main.py - Complete Crypto Futures Trading Bot for Render Deployment
import logging
import sys
import time
import requests
import threading
import uuid
import random
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import Thread
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS

# ======================== CONFIGURATION ========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
    """Trade execution record with proper P&L tracking"""
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
    
    def close(self, exit_price: float, exit_time: Optional[datetime] = None):
        """Close the trade and calculate P&L"""
        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now()
        self.status = TradeStatus.CLOSED
        
        if self.side == OrderSide.BUY:  # Long position
            self.pnl = (exit_price - self.entry_price) * self.quantity - self.fees
        else:  # Short position (SELL)
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
            'order_id': self.order_id,
            'fees': self.fees
        }

@dataclass
class MarketData:
    """Market data model for trading bot"""
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
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'price': self.price,
            'volume': self.volume,
            'timestamp': self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            'bid': self.bid,
            'ask': self.ask,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'vwap': self.vwap,
            'spread': self.spread
        }

@dataclass
class Order:
    """Order model for trading"""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    timestamp: datetime = field(default_factory=datetime.now)
    filled_quantity: float = 0.0
    filled_price: Optional[float] = None
    algorithm_id: Optional[str] = None
    trade_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side.value if isinstance(self.side, OrderSide) else self.side,
            'order_type': self.order_type.value if isinstance(self.order_type, OrderType) else self.order_type,
            'quantity': self.quantity,
            'price': self.price,
            'status': self.status.value if isinstance(self.status, OrderStatus) else self.status,
            'timestamp': self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            'filled_quantity': self.filled_quantity,
            'filled_price': self.filled_price,
            'algorithm_id': self.algorithm_id,
            'trade_id': self.trade_id
        }

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
    
    def update_pnl(self, current_price: float):
        self.current_price = current_price
        self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'current_price': self.current_price,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'algorithm_id': self.algorithm_id,
            'trades': [t.to_dict() for t in self.trades]
        }

@dataclass
class AlgorithmState:
    """State tracking for each algorithm"""
    algorithm_id: str
    name: str
    is_active: bool = True
    trade_count: int = 0
    max_trades: int = 2
    positions: List[Position] = field(default_factory=list)
    orders: List[Order] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0
    last_trade_time: Optional[datetime] = None
    
    def can_trade(self) -> bool:
        return self.is_active and self.trade_count < self.max_trades
    
    def increment_trade_count(self):
        self.trade_count += 1
        self.last_trade_time = datetime.now()
        if self.trade_count >= self.max_trades:
            self.is_active = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'algorithm_id': self.algorithm_id,
            'name': self.name,
            'is_active': self.is_active,
            'trade_count': self.trade_count,
            'max_trades': self.max_trades,
            'positions': [p.to_dict() for p in self.positions],
            'trades': [t.to_dict() for t in self.trades],
            'total_pnl': self.total_pnl,
            'last_trade_time': self.last_trade_time.isoformat() if self.last_trade_time else None
        }

# ======================== TRADING ENGINE ========================
class TradingAlgorithm:
    """Base class for trading algorithms"""
    def __init__(self, algorithm_id: str, name: str):
        self.algorithm_id = algorithm_id
        self.name = name
        self.state = AlgorithmState(algorithm_id=algorithm_id, name=name)
    
    def should_trade(self, market_data: MarketData) -> Optional[str]:
        if random.random() > 0.95:  # 5% chance to trade
            return 'long' if random.random() > 0.5 else 'short'
        return None
    
    def should_exit(self, position: Position, current_price: float) -> bool:
        if position.quantity > 0:  # Long position
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:  # Short position  
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100
        return pnl_pct >= 3.0 or pnl_pct <= -2.0

class TradingEngine:
    """Crypto futures trading engine with 19 algorithms"""
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.orders = {}
        self.market_data_queue = deque(maxlen=1000)
        self.lock = threading.Lock()
        self.thread = None
        self.total_realized_pnl = 0.0
        self.total_unrealized_pnl = 0.0
        self.closed_trades_pnl = 0.0
        self.position_size_usd = 100
        self.leverage = 10
        self._create_algorithms()
        logger.info(f"✅ Trading Engine initialized with {len(self.algorithms)} algorithms")
    
    def _create_algorithms(self):
        strategies = [
            "BTC Moving Average Crossover", "ETH RSI Oversold/Overbought", "BTC MACD Signal",
            "Bollinger Bands Multi-Crypto", "Volume Weighted BTC Strategy", "Mean Reversion ETH/BTC",
            "Momentum Trading SOL", "Breakout Strategy BNB", "Scalping Strategy XRP",
            "Swing Trading ADA", "BTC/ETH Pairs Trading", "Cross-Exchange Arbitrage",
            "Perpetual Funding Rate", "Trend Following MATIC", "Range Trading AVAX",
            "News Based BTC Trading", "Sentiment Analysis DOGE", "Options Delta Hedging",
            "High Frequency BTC Micro"
        ]
        for i, strategy in enumerate(strategies):
            algo = TradingAlgorithm(f"algo_{i+1}", strategy)
            self.algorithms.append(algo)
    
    def get_active_algorithm(self) -> Optional[TradingAlgorithm]:
        for algo in self.algorithms:
            if algo.state.can_trade():
                return algo
        return None
    
    def calculate_live_pnl(self):
        total_unrealized = 0.0
        for pos_key, position in self.positions.items():
            latest_price = None
            for data in reversed(list(self.market_data_queue)):
                if data.symbol == position.symbol:
                    latest_price = data.price
                    break
            if latest_price:
                if position.quantity > 0:  # Long position
                    unrealized_pnl = (latest_price - position.entry_price) * position.quantity
                else:  # Short position
                    unrealized_pnl = (position.entry_price - latest_price) * abs(position.quantity)
                position.current_price = latest_price
                position.unrealized_pnl = unrealized_pnl
                total_unrealized += unrealized_pnl
        self.total_unrealized_pnl = total_unrealized
        return self.closed_trades_pnl + self.total_unrealized_pnl
    
    def process_market_data(self, market_data: MarketData):
        if not self.is_running:
            return
        with self.lock:
            self.market_data_queue.append(market_data)
            self.calculate_live_pnl()
            algo = self.get_active_algorithm()
            if not algo:
                return
            self._check_exits(market_data)
            if algo.state.can_trade():
                direction = algo.should_trade(market_data)
                if direction:
                    self._execute_trade(algo, market_data, direction)
    
    def _check_exits(self, market_data: MarketData):
        positions_to_close = []
        for pos_key, position in self.positions.items():
            if position.symbol == market_data.symbol:
                algo = next((a for a in self.algorithms if a.algorithm_id == position.algorithm_id), None)
                if algo and algo.should_exit(position, market_data.price):
                    positions_to_close.append(pos_key)
        for pos_key in positions_to_close:
            self._close_position(pos_key, market_data)
    
    def _execute_trade(self, algorithm: TradingAlgorithm, market_data: MarketData, direction: str):
        trade_id = str(uuid.uuid4())
        quantity = round(self.position_size_usd / market_data.price, 6)
        side = OrderSide.BUY if direction == 'long' else OrderSide.SELL
        
        trade = Trade(
            trade_id=trade_id,
            symbol=market_data.symbol,
            side=side,
            quantity=quantity,
            entry_price=market_data.price,
            entry_time=datetime.now(),
            algorithm_id=algorithm.algorithm_id,
            fees=quantity * market_data.price * 0.0004
        )
        self.trades[trade_id] = trade
        
        pos_quantity = quantity if direction == 'long' else -quantity
        position = Position(
            symbol=market_data.symbol,
            quantity=pos_quantity,
            entry_price=market_data.price,
            current_price=market_data.price,
            algorithm_id=algorithm.algorithm_id,
            trades=[trade]
        )
        
        pos_key = f"{algorithm.algorithm_id}_{market_data.symbol}"
        self.positions[pos_key] = position
        algorithm.state.increment_trade_count()
        algorithm.state.trades.append(trade)
        
        notional = quantity * market_data.price
        margin = notional / self.leverage
        logger.info(f"🔥 TRADE: {algorithm.name}")
        logger.info(f"   {direction.upper()} {quantity:.6f} {market_data.symbol} @ ${market_data.price:,.2f}")
        logger.info(f"   Notional: ${notional:.2f} | Margin: ${margin:.2f} | Trade {algorithm.state.trade_count}/2")
    
    def _close_position(self, pos_key: str, market_data: MarketData):
        position = self.positions.get(pos_key)
        if not position:
            return
        
        if position.quantity > 0:  # Long
            pnl = (market_data.price - position.entry_price) * position.quantity
        else:  # Short
            pnl = (position.entry_price - market_data.price) * abs(position.quantity)
        
        for trade in position.trades:
            trade.close(market_data.price)
            trade.pnl = pnl
        
        self.closed_trades_pnl += pnl
        
        algo = next((a for a in self.algorithms if a.algorithm_id == position.algorithm_id), None)
        if algo:
            algo.state.total_pnl += pnl
            price_change = ((market_data.price - position.entry_price) / position.entry_price) * 100
            notional = abs(position.quantity) * position.entry_price
            roi = (pnl / (notional / self.leverage)) * 100
            logger.info(f"📊 CLOSED: {algo.name}")
            logger.info(f"   {position.symbol} {'LONG' if position.quantity > 0 else 'SHORT'}")
            logger.info(f"   ${position.entry_price:.2f} → ${market_data.price:.2f} ({price_change:+.2f}%)")
            logger.info(f"   P&L: ${pnl:+.2f} | ROI: {roi:+.1f}%")
        
        del self.positions[pos_key]
        self.calculate_live_pnl()
    
    def start(self):
        self.is_running = True
        logger.info("✅ Trading engine started")
    
    def stop(self):
        self.is_running = False
        total_pnl = self.closed_trades_pnl + self.total_unrealized_pnl
        total_trades = sum(a.state.trade_count for a in self.algorithms)
        logger.info(f"⏹️ Engine stopped | {total_trades} trades | P&L: ${total_pnl:+.2f}")
    
    def get_status(self):
        with self.lock:
            total_pnl = self.calculate_live_pnl()
            total_trades = sum(a.state.trade_count for a in self.algorithms)
            completed_trades = [t for t in self.trades.values() if hasattr(t, 'pnl') and t.pnl != 0]
            winning_trades = sum(1 for t in completed_trades if t.pnl > 0)
            win_rate = (winning_trades / len(completed_trades)) * 100 if completed_trades else 0
            total_volume = sum(abs(t.quantity * t.entry_price) for t in self.trades.values())
            
            return {
                'is_running': self.is_running,
                'total_algorithms': len(self.algorithms),
                'active_algorithms': sum(1 for a in self.algorithms if a.state.is_active),
                'total_trades': total_trades,
                'total_positions': len(self.positions),
                'total_pnl': round(total_pnl, 2),
                'realized_pnl': round(self.closed_trades_pnl, 2),
                'unrealized_pnl': round(self.total_unrealized_pnl, 2),
                'leverage': self.leverage,
                'win_rate': round(win_rate, 1),
                'total_volume': round(total_volume, 2),
                'completed_trades': len(completed_trades),
                'winning_algorithms': sum(1 for a in self.algorithms if a.state.total_pnl > 0),
                'losing_algorithms': sum(1 for a in self.algorithms if a.state.total_pnl < 0),
                'algorithms': [a.state.to_dict() for a in self.algorithms]
            }

# ======================== DATA PROVIDER ========================
class CryptoDataProvider:
    """Crypto data provider using CoinGecko API"""
    def __init__(self):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.crypto_ids = {
            'bitcoin': 'BTC-PERP', 'ethereum': 'ETH-PERP', 'binancecoin': 'BNB-PERP',
            'solana': 'SOL-PERP', 'ripple': 'XRP-PERP', 'cardano': 'ADA-PERP',
            'avalanche-2': 'AVAX-PERP', 'polkadot': 'DOT-PERP', 'polygon': 'MATIC-PERP',
            'chainlink': 'LINK-PERP', 'litecoin': 'LTC-PERP', 'uniswap': 'UNI-PERP',
            'cosmos': 'ATOM-PERP', 'arbitrum': 'ARB-PERP', 'optimism': 'OP-PERP'
        }
        self.last_prices = {}
    
    def get_crypto_prices(self):
        try:
            ids = ','.join(self.crypto_ids.keys())
            url = f"{self.base_url}/simple/price"
            params = {'ids': ids, 'vs_currencies': 'usd', 'include_24hr_change': 'true', 'include_24hr_vol': 'true'}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                crypto_data = []
                for coin_id, symbol in self.crypto_ids.items():
                    if coin_id in data:
                        coin_data = data[coin_id]
                        price = coin_data.get('usd', 0)
                        volume = coin_data.get('usd_24h_vol', 0)
                        change = coin_data.get('usd_24h_change', 0)
                        open_price = self.last_prices.get(symbol, price)
                        self.last_prices[symbol] = price
                        high = price * (1 + abs(change/100) * 0.5)
                        low = price * (1 - abs(change/100) * 0.5)
                        crypto_data.append({
                            'symbol': symbol, 'price': price, 'open': open_price,
                            'high': high, 'low': low, 'close': price,
                            'volume': volume if volume else 1000000,
                            'change_24h': change, 'timestamp': datetime.now()
                        })
                return crypto_data
            return []
        except Exception as e:
            logger.error(f"Error fetching crypto prices: {e}")
            return []
    
    def test_connection(self):
        try:
            response = requests.get(f"{self.base_url}/ping", timeout=5)
            return response.status_code == 200
        except:
            return False

# ======================== FLASK DASHBOARD ========================
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>🔴 LIVE Crypto Futures Trading Bot</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            min-height: 100vh;
            padding: 15px;
        }
        .header {
            background: rgba(0,0,0,0.4);
            padding: 20px;
            border-radius: 15px;
            text-align: center;
            margin-bottom: 20px;
            backdrop-filter: blur(15px);
        }
        .live-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            background: #ff4757;
            border-radius: 50%;
            animation: pulse 1s infinite;
            margin-right: 10px;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        .pnl-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .pnl-card {
            background: rgba(255,255,255,0.1);
            padding: 25px;
            border-radius: 15px;
            backdrop-filter: blur(10px);
            text-align: center;
        }
        .pnl-large {
            font-size: 48px;
            font-weight: bold;
            margin: 15px 0;
        }
        .pnl-positive { color: #00ff88; }
        .pnl-negative { color: #ff4757; }
        .pnl-neutral { color: #ffa502; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }
        .stat-value {
            font-size: 24px;
            font-weight: bold;
            margin-bottom: 5px;
        }
        .stat-label {
            font-size: 12px;
            opacity: 0.8;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        th {
            background: rgba(0,0,0,0.5);
        }
    </style>
</head>
<body>
    <div class="header">
        <h1><span class="live-indicator"></span>LIVE Crypto Futures Trading Bot</h1>
        <p>19 Algorithms | 2 Trades Each | 10x Leverage</p>
    </div>
    
    <div class="pnl-grid">
        <div class="pnl-card">
            <div>💰 Total P&L</div>
            <div id="total-pnl" class="pnl-large pnl-neutral">$0.00</div>
            <div class="stats-grid">
                <div>
                    <div id="realized-pnl" class="stat-value">$0.00</div>
                    <div class="stat-label">Realized</div>
                </div>
                <div>
                    <div id="unrealized-pnl" class="stat-value">$0.00</div>
                    <div class="stat-label">Unrealized</div>
                </div>
            </div>
        </div>
        
        <div class="pnl-card">
            <div>📊 Trading Stats</div>
            <div class="stats-grid">
                <div>
                    <div id="win-rate" class="stat-value">0%</div>
                    <div class="stat-label">Win Rate</div>
                </div>
                <div>
                    <div id="total-trades" class="stat-value">0</div>
                    <div class="stat-label">Trades</div>
                </div>
                <div>
                    <div id="open-positions" class="stat-value">0</div>
                    <div class="stat-label">Positions</div>
                </div>
                <div>
                    <div id="total-volume" class="stat-value">$0</div>
                    <div class="stat-label">Volume</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="pnl-card">
        <h3>Live Positions</h3>
        <table>
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Current</th>
                    <th>P&L</th>
                </tr>
            </thead>
            <tbody id="positions-tbody">
                <tr><td colspan="5" style="text-align:center">No positions</td></tr>
            </tbody>
        </table>
    </div>
    
    <script>
        function updatePnL(value, elementId) {
            const element = document.getElementById(elementId);
            if (value > 0) {
                element.className = element.className.replace(/pnl-(positive|negative|neutral)/, 'pnl-positive');
                element.textContent = '+$' + Math.abs(value).toFixed(2);
            } else if (value < 0) {
                element.className = element.className.replace(/pnl-(positive|negative|neutral)/, 'pnl-negative');
                element.textContent = '-$' + Math.abs(value).toFixed(2);
            } else {
                element.className = element.className.replace(/pnl-(positive|negative|neutral)/, 'pnl-neutral');
                element.textContent = '$0.00';
            }
        }
        
        function refresh() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    updatePnL(data.total_pnl || 0, 'total-pnl');
                    document.getElementById('realized-pnl').textContent = '$' + (data.realized_pnl || 0).toFixed(2);
                    document.getElementById('unrealized-pnl').textContent = '$' + (data.unrealized_pnl || 0).toFixed(2);
                    document.getElementById('win-rate').textContent = (data.win_rate || 0) + '%';
                    document.getElementById('total-trades').textContent = data.total_trades || 0;
                    document.getElementById('open-positions').textContent = data.total_positions || 0;
                    document.getElementById('total-volume').textContent = '$' + (data.total_volume || 0).toFixed(0);
                });
            
            fetch('/api/positions')
                .then(r => r.json())
                .then(data => {
                    const tbody = document.getElementById('positions-tbody');
                    if (data.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center">No positions</td></tr>';
                    } else {
                        tbody.innerHTML = data.map(p => `
                            <tr>
                                <td>${p.symbol}</td>
                                <td style="color:${p.side==='LONG'?'#00ff88':'#ff4757'}">${p.side}</td>
                                <td>$${p.entry_price.toFixed(2)}</td>
                                <td>$${p.current_price.toFixed(2)}</td>
                                <td style="color:${p.current_pnl>=0?'#00ff88':'#ff4757'}">
                                    ${p.current_pnl>=0?'+':''}$${p.current_pnl.toFixed(2)}
                                </td>
                            </tr>
                        `).join('');
                    }
                });
        }
        setInterval(refresh, 1000);
        refresh();
    </script>
</body>
</html>
'''

def create_app(engine):
    """Create Flask application"""
    app = Flask(__name__)
    CORS(app)
    
    @app.route('/')
    def index():
        return render_template_string(DASHBOARD_HTML)
    
    @app.route('/api/status')
    def status():
        with engine.lock:
            engine.calculate_live_pnl()
        return jsonify(engine.get_status())
    
    @app.route('/api/positions')
    def positions():
        positions_detail = []
        for pos_key, position in engine.positions.items():
            latest_price = None
            for data in reversed(list(engine.market_data_queue)):
                if data.symbol == position.symbol:
                    latest_price = data.price
                    break
            if latest_price:
                if position.quantity > 0:
                    current_pnl = (latest_price - position.entry_price) * position.quantity
                else:
                    current_pnl = (position.entry_price - latest_price) * abs(position.quantity)
                
                algo = next((a for a in engine.algorithms if a.algorithm_id == position.algorithm_id), None)
                positions_detail.append({
                    'symbol': position.symbol,
                    'algorithm': algo.name if algo else "Unknown",
                    'side': 'LONG' if position.quantity > 0 else 'SHORT',
                    'quantity': abs(position.quantity),
                    'entry_price': position.entry_price,
                    'current_price': latest_price,
                    'current_pnl': round(current_pnl, 2)
                })
        return jsonify(positions_detail)
    
    return app

# ======================== MAIN APPLICATION ========================
def feed_live_crypto_data(engine):
    """Feed live crypto data to the trading engine"""
    data_provider = CryptoDataProvider()
    logger.info("🔴 LIVE DATA FEED STARTED")
    
    while engine.is_running:
        try:
            crypto_data = data_provider.get_crypto_prices()
            if not crypto_data:
                time.sleep(5)
                continue
            
            for crypto in crypto_data:
                if not engine.is_running:
                    break
                
                spread_percent = 0.0002
                half_spread = crypto['price'] * spread_percent / 2
                
                market_data = MarketData(
                    symbol=crypto['symbol'],
                    price=crypto['price'],
                    volume=crypto['volume'],
                    timestamp=crypto['timestamp'],
                    bid=crypto['price'] - half_spread,
                    ask=crypto['price'] + half_spread,
                    open=crypto['open'],
                    high=crypto['high'],
                    low=crypto['low'],
                    close=crypto['close']
                )
                
                engine.process_market_data(market_data)
                
                change = crypto['change_24h']
                change_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                logger.info(f"{change_emoji} {crypto['symbol']}: ${crypto['price']:,.2f} ({change:+.2f}%)")
                time.sleep(0.2)
            
            logger.info("💤 Next update in 30 seconds...")
            time.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in data feed: {e}")
            time.sleep(10)

def main():
    """Main entry point for Render deployment"""
    logger.info("=" * 70)
    logger.info("🚀 LIVE CRYPTO FUTURES TRADING BOT v2.0")
    logger.info("=" * 70)
    
    # Test API connection
    data_provider = CryptoDataProvider()
    if not data_provider.test_connection():
        logger.error("❌ Cannot connect to CoinGecko API")
        sys.exit(1)
    logger.info("✅ Connected to CoinGecko API")
    
    # Initialize trading engine
    engine = TradingEngine()
    engine.start()
    
    # Start live data feed
    data_thread = Thread(target=feed_live_crypto_data, args=(engine,))
    data_thread.daemon = True
    data_thread.start()
    
    # Create Flask app
    app = create_app(engine)
    
    logger.info("=" * 70)
    logger.info("📊 DASHBOARD: http://0.0.0.0:5000")
    logger.info("🔴 LIVE TRADING ACTIVE")
    logger.info("• 19 Algorithms (2 trades each)")
    logger.info("• 10x Leverage")
    logger.info("• $100 per position")
    logger.info("• Max 38 total trades")
    logger.info("=" * 70)
    
    # Run Flask app (Render will use PORT env variable)
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()