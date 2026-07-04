"""策略基类与数据上下文接口 —— 接口冻结文件,任何实现会话不得修改。
策略只能通过 DataContext 取数,且引擎保证取不到 signal date 之后的数据(防未来函数)。"""
from abc import ABC, abstractmethod
from models import Order, Account


class DataContext(ABC):
    """策略可用的全部数据接口。实现在 engine.py,由 M2 任务完成。"""

    @abstractmethod
    def close(self, code: str, n: int) -> list[float]:
        """截至当前信号日(含)的最近 n 个后复权收盘价,时间升序。不足n则返回可得部分。"""

    @abstractmethod
    def bar(self, code: str, date: str) -> dict | None:
        """某日不复权行情 dict(open/high/low/close/volume/amount/limit_up/limit_down/is_suspended)"""

    @abstractmethod
    def members(self, index_code: str, date: str) -> list[str]:
        """指数在 date 当日的成分股(用历史快照表,防幸存者偏差)"""

    @abstractmethod
    def is_tradable(self, code: str, date: str) -> bool:
        """未停牌、未退市、上市满60日、非ST、非北交所"""

    @abstractmethod
    def avg_amount(self, code: str, n: int) -> float:
        """近 n 日日均成交额(元),用于流动性过滤"""

    @abstractmethod
    def is_last_trade_day_of_week(self, date: str) -> bool: ...

    @abstractmethod
    def is_last_trade_day_of_month(self, date: str) -> bool: ...


class BaseStrategy(ABC):
    strategy_id: str = ""       # 's2_etf@v1';参数来自 registry.yaml,注册后冻结
    benchmark: str = ""         # 基准指数/ETF代码
    params: dict = {}
    universe: list = []         # 静态池;动态池策略在 generate_orders 内用 ctx.members 取

    @abstractmethod
    def generate_orders(self, date: str, ctx: DataContext,
                        account: Account) -> list[Order]:
        """在 date 收盘后调用,返回次日开盘执行的订单;空列表=无操作。
        规则:
        - buy 单给 weight(目标占比),股数由引擎按次日开盘价换算并取整手;
        - sell 单 weight 填 0 表示清仓该标的;
        - reason 必填,会原样推送给用户;
        - 不做风控判断(止损/仓位上限由 risk.py 统一处理),策略只表达观点。"""
