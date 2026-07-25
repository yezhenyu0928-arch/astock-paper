"""核心数据类 —— 接口冻结文件,任何实现会话不得修改本文件。
所有模块通过这些类交换数据。"""
from dataclasses import dataclass, field, asdict


@dataclass
class Order:
    strategy_id: str          # 如 's2_etf@v1'
    code: str                 # 如 'sh510300'
    side: str                 # 'buy' | 'sell'
    weight: float             # buy: 目标占账户净值比例(0~1); sell: 忽略,按持仓全卖或引擎按weight部分卖
    reason: str               # 一句话理由,原样进推送
    signal_date: str          # 'YYYY-MM-DD' 信号生成日(收盘后)
    # ---- 以下由引擎撮合时回填 ----
    shares: int = 0
    sim_price: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    status: str = "pending"   # pending/filled/cancelled(涨停买单)/deferred(跌停停牌顺延)

    def key(self) -> str:
        """幂等键:同一天同策略同标的同方向只允许一单"""
        return f"{self.signal_date}|{self.strategy_id}|{self.code}|{self.side}"

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    code: str
    shares: int
    avg_cost: float           # 含费摊薄成本
    buy_date: str             # 最近一次买入日,用于T+1判断
    highest_close: float = 0  # 持有期最高收盘价,供跟踪止损类策略用


@dataclass
class Account:
    strategy_id: str
    init_capital: float
    cash: float
    positions: dict = field(default_factory=dict)   # code -> Position
    frozen: bool = False       # 熔断标记,True时策略暂停
    nav: float = 1.0

    def market_value(self, price_of) -> float:
        """price_of: callable(code)->最新收盘价"""
        return sum(p.shares * price_of(p.code) for p in self.positions.values())

    def total(self, price_of) -> float:
        return self.cash + self.market_value(price_of)
