-- 接口冻结文件:表结构不得更改,可追加索引。
CREATE TABLE IF NOT EXISTS daily_bar (
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL,
  adj_factor REAL DEFAULT 1.0,      -- 后复权因子
  is_suspended INTEGER DEFAULT 0,
  limit_up REAL, limit_down REAL,
  source TEXT,
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bar_date ON daily_bar(trade_date);

CREATE TABLE IF NOT EXISTS trade_calendar (
  cal_date TEXT PRIMARY KEY, is_open INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS index_members (
  index_code TEXT NOT NULL, code TEXT NOT NULL,
  in_date TEXT NOT NULL, out_date TEXT,          -- NULL=仍在成分
  PRIMARY KEY (index_code, code, in_date)
);

CREATE TABLE IF NOT EXISTS dividend (
  code TEXT NOT NULL, ex_date TEXT NOT NULL,
  cash_per_share REAL DEFAULT 0,
  shares_ratio REAL DEFAULT 0,
  PRIMARY KEY (code, ex_date)
);

CREATE TABLE IF NOT EXISTS security (
  code TEXT PRIMARY KEY, name TEXT,
  type TEXT CHECK(type IN ('stock','etf')),
  is_t0 INTEGER DEFAULT 0,
  list_date TEXT, status TEXT DEFAULT 'L'        -- L/ST/D
);
