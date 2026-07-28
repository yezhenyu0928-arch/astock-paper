# db-seed 分支
本地完整行情+基本面种子库(gzip 分卷)。云端海外 Runner 连不上 baostock,历史基本面只能靠本分支下发。
- 还原: cat db_seed.sqlite.gz.part* | gunzip > db/market.sqlite
- 校验: sha256sum 比对 db_seed.sha256
- 覆盖: daily_bar 1603只(2018~2026), fundamental 1465只/117万行, index_members mainboard 3044条
- 更新方式: 本地重新导出分卷后 force push 本分支(孤儿分支,不进 main 历史)
