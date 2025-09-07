#!/usr/bin/env python3.13
"""NUCLEAR FIXED Crypto Futures Trading Bot v7.0
Complete single file with auto-recovery, non-blocking API, and 1h timeframe
Production-quality rewrite with async RobustMarketDataManager and FastAPI dashboard"""

import os
import sys
import time
import json
import uuid
import random
import logging
import asyncio
import aiohttp
import websockets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from binance import AsyncClient, BinanceSocketManager
from binance.enums import *
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
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
    
    PORT = int(os.getenv('PORT', '8000'))
    DATA_UPDATE_INTERVAL = int(os.getenv('DATA_UPDATE_INTERVAL', '60'))
    BALANCE_FILE = 'data/balance.json'
    
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '2.0'))
    TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '3.0'))
    
    # Conservative Binance limits
    BINANCE_MAX_REQUESTS_PER_MIN = 600
    API_TIMEOUT = 5

# Create data directory
os.makedirs('data', exist_ok=True)

# Setup FastAPI
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data Models
@dataclass
class Trade:
    """Trade data model"""
    trade_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    exit_price: Optional[float] = None
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: Optional[datetime] = None
    status: str = "open"
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
        self.status = "closed"
        
        if self.side == "buy":
            self.pnl = (exit_price - self.entry_price) * self.quantity - self.fees
        else:
            self.pnl = (self.entry_price - exit_price) * abs(self.quantity)

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

# WebSocket Manager
class WebSocketManager:
    def __init__(self):
        self.client = AsyncClient(api_key=Config.BINANCE_API_KEY, api_secret=Config.BINANCE_API_SECRET)
        self.ws_manager = BinanceSocketManager(self.client)
        self.data_cache: Dict[str, Dict] = {}
        self.pnl_tracker = PnLTracker()
        self.ws_connections: List[WebSocket] = []
        self.running = False
        
    async def start(self):
        self.running = True
        await self._start_websocket()
        
    async def _start_websocket(self):
        try:
            async with self.ws_manager as ws:
                # Get top 100 trading pairs by volume
                info = await self.client.get_exchange_info()
                symbols = [symbol['symbol'] for symbol in info['symbols'] 
                          if symbol['symbol'].endswith('USDT') and symbol['status'] == 'TRADING']
                
                # Sort by 24h volume and get top 100
                volumes = await asyncio.gather(
                    *[self.client.get_ticker(symbol=symbol) for symbol in symbols]
                )
                top_100 = sorted(
                    [(symbol, float(ticker['volume'])) for symbol, ticker in zip(symbols, volumes)],
                    key=lambda x: x[1],
                    reverse=True
                )[:100]
                
                symbols_to_track = [symbol for symbol, _ in top_100]
                
                # Subscribe to all symbols in batches of 200 (Binance limit)
                for i in range(0, len(symbols_to_track), 200):
                    batch = symbols_to_track[i:i+200]
                    for symbol in batch:
                        await ws.symbol_ticker_socket(callback=self._handle_ticker, symbol=symbol)
                
                while self.running:
                    await asyncio.sleep(1)
                    await self._broadcast_updates()
                    
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await self._restart_websocket()
    
    def _handle_ticker(self, msg):
        symbol = msg['s']
        price = float(msg['c'])
        timestamp = datetime.fromtimestamp(msg['E'] / 1000)
        
        self.data_cache[symbol] = {
            'symbol': symbol,
            'price': price,
            'volume': float(msg['v']),
            'timestamp': timestamp
        }
        
        if symbol in self.pnl_tracker.positions:
            self.pnl_tracker.update_position(symbol, price)
    
    async def _broadcast_updates(self):
        if not self.ws_connections:
            return
            
        data = {
            'timestamp': datetime.now().isoformat(),
            'prices': self.data_cache,
            'pnl': {
                'unrealized': self.pnl_tracker.unrealized_pnl,
                'realized': self.pnl_tracker.realized_pnl,
                'total': self.pnl_tracker.total_pnl
            }
        }
        
        for ws in self.ws_connections:
            try:
                await ws.send_json(data)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                await ws.close(code=1000, reason="Broadcast error")
    
    async def _restart_websocket(self):
        await asyncio.sleep(5)
        await self.start()

# PnL Tracker
class PnLTracker:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_pnl: float = 0.0
        
    def update_position(self, symbol: str, price: float):
        if symbol in self.positions:
            position = self.positions[symbol]
            position.current_price = price
            position.unrealized_pnl = (price - position.entry_price) * position.quantity
            self._recalculate_pnl()
    
    def _recalculate_pnl(self):
        self.unrealized_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        self.total_pnl = self.realized_pnl + self.unrealized_pnl

# Trading Engine
class TradingEngine:
    """Main trading engine orchestrator"""
    
    def __init__(self):
        self.is_running = False
        self.algorithms = []
        self.positions = {}
        self.trades = {}
        self.closed_trades = []
        self.market_data_queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.balance_manager = BalanceManager(Config.INITIAL_BALANCE)
        self.ws_manager = WebSocketManager()
        
        self.total_realized_pnl = 0.0
        self.total_unrealized_pnl = 0.0
        
        self.total_wins = 0
        self.total_losses = 0
        self.daily_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0.0})
        
        self.last_known_prices = {}
        self.last_update_time = datetime.now()
        
        self._create_algorithms()
        
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
    
    async def start(self):
        """Start trading engine"""
        self.is_running = True
        await self.ws_manager.start()
        logger.info("✅ Trading Engine initialized")
        logger.info(f"💰 Balance: ${self.balance_manager.get_balance():.2f}")
        
        # Start market data processing
        asyncio.create_task(self._process_market_data())
        
        # Start trading loop
        asyncio.create_task(self._trading_loop())
    
    async def _process_market_data(self):
        """Process incoming market data"""
        while self.is_running:
            try:
                market_data = await self.market_data_queue.get()
                await self._process_single_market_data(market_data)
            except Exception as e:
                logger.error(f"Market data processing error: {e}")
                await asyncio.sleep(1)
    
    async def _process_single_market_data(self, market_data: MarketData):
        """Process single market data update"""
        async with self.lock:
            # Update P&L
            self.calculate_live_pnl()
            
            # Check for exits
            await self._check_exits(market_data)
            
            # Get 1h klines for analysis
            binance_symbol = market_data.symbol.replace('-PERP', 'USDT')
            klines = await self._fetch_klines(binance_symbol)
            
            # Check for new trades
            algo = self.get_active_algorithm()
            if algo and algo.state.can_trade() and klines:
                direction = await algo.should_trade(market_data, klines)
                if direction:
                    await self._execute_trade(algo, market_data, direction)
    
    async def _trading_loop(self):
        """Main trading loop"""
        while self.is_running:
            try:
                # Update P&L
                self.calculate_live_pnl()
                
                # Periodic balance save
                if random.random() < 0.05:
                    self.balance_manager.save_balance()
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Trading loop error: {e}")
                await asyncio.sleep(5)
    
    async def stop(self):
        """Stop trading engine"""
        self.is_running = False
        self.balance_manager.save_balance()
        pnl_data = self.calculate_live_pnl()
        logger.info(f"⏹️ Engine stopped | P&L: ${pnl_data['total']:+.2f}")
        
        # Clean shutdown of WebSocket manager
        self.ws_manager.running = False

# Balance Manager
class BalanceManager:
    """Manage trading balance and equity history"""
    
    def __init__(self, initial_balance: float = 10000.0):
        self.lock = asyncio.Lock()
        self.balance_file = Config.BALANCE_FILE
        self.initial_balance = initial_balance
        self.balance_data = self._load_balance()
        self.balance = self.balance_data.get('balance', initial_balance)
        self.equity_history = self.balance_data.get('equity_history', [])
        self.max_drawdown = self.balance_data.get('max_drawdown', 0)
        self.peak_balance = self.balance_data.get('peak_balance', initial_balance)
        
    async def _load_balance(self) -> Dict:
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

# Trading Algorithm
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
    
    async def analyze_1h_data(self, symbol: str, klines: List[Dict]) -> Dict:
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

@app.on_event("startup")
async def startup_event():
    engine = TradingEngine()
    asyncio.create_task(engine.start())

@app.on_event("shutdown")
async def shutdown_event():
    engine.stop()

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    engine.ws_manager.ws_connections.append(websocket)
    
    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        engine.ws_manager.ws_connections.remove(websocket)
        await websocket.close(code=1000, reason="Disconnected")

@app.get("/api/prices")
async def get_prices():
    return engine.ws_manager.data_cache

@app.get("/api/pnl")
async def get_pnl():
    return {
        'unrealized': engine.ws_manager.pnl_tracker.unrealized_pnl,
        'realized': engine.ws_manager.pnl_tracker.realized_pnl,
        'total': engine.ws_manager.pnl_tracker.total_pnl
    }

@app.get("/api/status")
async def get_status():
    return {
        'is_running': engine.is_running,
        'positions': len(engine.positions),
        'trades': len(engine.trades),
        'pnl': engine.calculate_live_pnl()
    }

# Dashboard HTML
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Crypto Trading Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .card { background: #f4f4f4; padding: 20px; margin: 10px 0; border-radius: 8px; }
        .price { font-size: 24px; font-weight: bold; }
        .pnl { font-size: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Crypto Trading Dashboard</h1>
        <div class="card">
            <h2>Real-time PnL</h2>
            <div class="pnl">
                Unrealized: <span id="unrealizedPnl">0</span><br>
                Realized: <span id="realizedPnl">0</span><br>
                Total: <span id="totalPnl">0</span>
            </div>
        </div>
        <div class="card">
            <h2>Prices</h2>
            <div id="prices"></div>
        </div>
        <canvas id="chart"></canvas>
    </div>

    <script>
        const ws = new WebSocket('ws://' + window.location.host + '/ws');
        const ctx = document.getElementById('chart').getContext('2d');
        let chart;

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            
            // Update PnL
            document.getElementById('unrealizedPnl').textContent = 
                data.pnl.unrealized.toFixed(2);
            document.getElementById('realizedPnl').textContent = 
                data.pnl.realized.toFixed(2);
            document.getElementById('totalPnl').textContent = 
                data.pnl.total.toFixed(2);
            
            // Update prices
            const pricesDiv = document.getElementById('prices');
            pricesDiv.innerHTML = Object.entries(data.prices).map(([symbol, price]) => 
                            `<p>${symbol}: $${price.price}</p>`
            `).join('');
            
            // Update chart
            if (chart) {
                chart.destroy();
            }
            
            const labels = Object.keys(data.prices);
            const prices = Object.values(data.prices).map(p => p.price);
            
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [{
                        label: 'Prices',
                        data: prices,
                        borderColor: 'rgb(75, 192, 192)',
                        tension: 0.1
                    }]
                },
                options: {
                    responsive: true,
                    animation: false
                }
            });
        };
    </script>
</body>
</html>"""

@app.get("/")
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

# Main execution
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)