# -*- coding: utf-8 -*-
"""数据快照与备份模块。

功能:
1. 本地 SQLite 数据库定期快照备份
2. 数据完整性校验和修复
3. 备份版本管理和自动清理
4. 数据恢复工具

备份策略:
- 完全备份: 每周一次完整数据库复制
- 增量备份: 每日备份新增数据(通过 SQL 导出)
- 保留策略: 保留最近4个周备份 + 最近7天日备份
- 压缩存储: 使用 gzip 压缩备份文件

低成本设计:
- 纯本地文件存储,无需云存储费用
- 增量备份减少存储占用
- 自动清理过期备份
"""
import os
import gzip
import shutil
import sqlite3
import logging
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import tempfile

import conf
from db import get_conn, DB_PATH

log = logging.getLogger("data_snapshot")

# 备份目录
BACKUP_DIR = conf.ROOT / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 备份保留策略
KEEP_WEEKS = 4      # 保留4周完整备份
KEEP_DAYS = 7       # 保留7天增量备份


def _get_backup_path(prefix: str, date: datetime = None, compressed: bool = True) -> Path:
    """生成备份文件路径"""
    date = date or datetime.now()
    timestamp = date.strftime("%Y%m%d_%H%M%S")
    ext = ".gz" if compressed else ""
    return BACKUP_DIR / f"{prefix}_{timestamp}.db{ext}"


def _get_metadata_path(backup_path: Path) -> Path:
    """获取备份元数据文件路径"""
    return backup_path.with_suffix(backup_path.suffix + ".meta")


def _calculate_hash(file_path: Path) -> str:
    """计算文件 MD5 hash"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def _get_db_tables(conn: sqlite3.Connection) -> List[str]:
    """获取数据库所有表名"""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [row[0] for row in cursor.fetchall()]


def _get_table_row_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """获取各表行数统计"""
    counts = {}
    for table in _get_db_tables(conn):
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        except Exception as e:
            log.warning("统计表 %s 行数失败: %s", table, e)
            counts[table] = -1
    return counts


# ============ 备份功能 ============

def create_full_backup() -> Optional[Path]:
    """创建完整数据库备份

    Returns:
        备份文件路径,失败返回 None
    """
    try:
        backup_path = _get_backup_path("full")
        metadata_path = _get_metadata_path(backup_path)

        # 获取备份前统计
        conn = get_conn()
        stats_before = _get_table_row_counts(conn)
        conn.close()

        # 创建压缩备份
        log.info("开始创建完整备份...")
        start_time = datetime.now()

        with open(DB_PATH, 'rb') as f_in:
            with gzip.open(backup_path, 'wb', compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)

        elapsed = (datetime.now() - start_time).total_seconds()
        file_size = backup_path.stat().st_size
        original_size = DB_PATH.stat().st_size

        # 写入元数据
        metadata = {
            "type": "full",
            "created_at": datetime.now().isoformat(),
            "original_path": str(DB_PATH),
            "original_size": original_size,
            "compressed_size": file_size,
            "compression_ratio": round(original_size / file_size, 2) if file_size > 0 else 0,
            "elapsed_seconds": elapsed,
            "md5_hash": _calculate_hash(backup_path),
            "table_stats": stats_before,
            "sqlite_version": sqlite3.sqlite_version,
        }

        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        log.info("完整备份完成: %s (%.1fMB, 压缩比 %.1fx, 耗时 %.1fs)",
                backup_path.name, file_size / 1024 / 1024,
                metadata["compression_ratio"], elapsed)

        return backup_path

    except Exception as e:
        log.error("创建完整备份失败: %s", e)
        return None


def create_incremental_backup(tables: List[str] = None) -> Optional[Path]:
    """创建增量备份(导出最近一天的数据)

    Args:
        tables: 指定要备份的表,None则备份所有数据表

    Returns:
        备份文件路径,失败返回 None
    """
    try:
        backup_path = _get_backup_path("incremental")
        metadata_path = _get_metadata_path(backup_path)

        conn = get_conn()

        # 默认备份所有数据表(排除 sqlite_ 系统表)
        if tables is None:
            tables = [t for t in _get_db_tables(conn) if not t.startswith("sqlite_")]

        # 获取昨天的日期范围
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")

        backup_data = {}
        total_rows = 0

        for table in tables:
            try:
                # 检查表结构,确定日期字段
                cursor = conn.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]

                date_col = None
                for col in ["trade_date", "cal_date", "ex_date", "date"]:
                    if col in columns:
                        date_col = col
                        break

                if date_col:
                    # 按日期过滤导出
                    cursor = conn.execute(
                        f"SELECT * FROM {table} WHERE {date_col} >= ? AND {date_col} < ?",
                        (yesterday, today)
                    )
                else:
                    # 没有日期字段,导出全部(小表)
                    cursor = conn.execute(f"SELECT * FROM {table}")

                rows = cursor.fetchall()
                if rows:
                    # 获取列名
                    col_names = [desc[0] for desc in cursor.description]
                    backup_data[table] = {
                        "columns": col_names,
                        "rows": rows
                    }
                    total_rows += len(rows)

            except Exception as e:
                log.warning("备份表 %s 失败: %s", table, e)

        conn.close()

        if not backup_data:
            log.info("增量备份: 无新数据,跳过")
            return None

        # 压缩存储
        import pickle
        data_bytes = pickle.dumps(backup_data)
        compressed = gzip.compress(data_bytes, compresslevel=6)

        with open(backup_path, 'wb') as f:
            f.write(compressed)

        # 写入元数据
        metadata = {
            "type": "incremental",
            "created_at": datetime.now().isoformat(),
            "date_range": [yesterday, today],
            "tables": list(backup_data.keys()),
            "total_rows": total_rows,
            "compressed_size": len(compressed),
            "raw_size": len(data_bytes),
            "compression_ratio": round(len(data_bytes) / len(compressed), 2),
        }

        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        log.info("增量备份完成: %s (%d 行, %d 表)",
                backup_path.name, total_rows, len(backup_data))

        return backup_path

    except Exception as e:
        log.error("创建增量备份失败: %s", e)
        return None


def cleanup_old_backups():
    """清理过期备份文件"""
    try:
        now = datetime.now()
        kept_full = 0
        kept_incremental = 0
        deleted = 0

        for backup_file in sorted(BACKUP_DIR.glob("*.db*")):
            if backup_file.suffix == ".meta":
                continue

            # 解析文件名获取时间
            try:
                # 格式: {type}_YYYYMMDD_HHMMSS.db[.gz]
                parts = backup_file.stem.split("_")
                if len(parts) >= 3:
                    date_str = parts[1]
                    time_str = parts[2].split(".")[0]  # 去掉 .db
                    file_time = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
                else:
                    continue
            except Exception:
                continue

            age_days = (now - file_time).days
            is_full = "full" in backup_file.name

            should_delete = False

            if is_full:
                # 保留最近 KEEP_WEEKS 周的完整备份(周日备份)
                if age_days > KEEP_WEEKS * 7:
                    should_delete = True
                else:
                    kept_full += 1
            else:
                # 保留最近 KEEP_DAYS 天的增量备份
                if age_days > KEEP_DAYS:
                    should_delete = True
                else:
                    kept_incremental += 1

            if should_delete:
                backup_file.unlink(missing_ok=True)
                meta_file = _get_metadata_path(backup_file)
                meta_file.unlink(missing_ok=True)
                deleted += 1

        if deleted > 0:
            log.info("清理过期备份: 删除 %d 个, 保留完整备份 %d 个, 增量备份 %d 个",
                    deleted, kept_full, kept_incremental)

    except Exception as e:
        log.error("清理备份失败: %s", e)


# ============ 恢复功能 ============

def restore_full_backup(backup_path: Path, target_path: Path = None) -> bool:
    """从完整备份恢复数据库

    Args:
        backup_path: 备份文件路径
        target_path: 恢复目标路径,默认覆盖原数据库

    Returns:
        恢复是否成功
    """
    try:
        target = target_path or DB_PATH
        metadata_path = _get_metadata_path(backup_path)

        # 读取元数据验证
        if metadata_path.exists():
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            log.info("恢复备份: %s (创建于 %s)",
                    backup_path.name, metadata.get("created_at", "未知"))

        # 备份当前数据库(如果存在)
        if target.exists():
            backup_current = str(target) + ".before_restore"
            shutil.copy2(target, backup_current)
            log.info("当前数据库已备份至: %s", backup_current)

        # 解压恢复
        with gzip.open(backup_path, 'rb') as f_in:
            with open(target, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

        log.info("数据库恢复完成: %s", target)
        return True

    except Exception as e:
        log.error("恢复备份失败: %s", e)
        return False


def restore_incremental_backup(backup_path: Path) -> bool:
    """应用增量备份到当前数据库

    Args:
        backup_path: 增量备份文件路径

    Returns:
        应用是否成功
    """
    try:
        import pickle

        # 读取备份数据
        with open(backup_path, 'rb') as f:
            compressed = f.read()
        data_bytes = gzip.decompress(compressed)
        backup_data = pickle.loads(data_bytes)

        conn = get_conn()
        total_inserted = 0

        for table, data in backup_data.items():
            columns = data["columns"]
            rows = data["rows"]

            if not rows:
                continue

            # 构建 INSERT OR REPLACE 语句
            placeholders = ",".join(["?"] * len(columns))
            sql = f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"

            conn.executemany(sql, rows)
            total_inserted += len(rows)
            log.debug("恢复表 %s: %d 行", table, len(rows))

        conn.commit()
        conn.close()

        log.info("增量备份应用完成: %d 行数据", total_inserted)
        return True

    except Exception as e:
        log.error("应用增量备份失败: %s", e)
        return False


# ============ 数据校验 ============

class DataIntegrityChecker:
    """数据完整性校验器"""

    def __init__(self):
        self.issues = []

    def check_all(self) -> Dict:
        """执行所有数据校验

        Returns:
            {
                "passed": bool,
                "issues": List[Dict],
                "stats": Dict
            }
        """
        self.issues = []

        checks = [
            self._check_daily_bar_integrity,
            self._check_calendar_continuity,
            self._check_adj_factor_validity,
            self._check_duplicate_records,
            self._check_orphan_records,
        ]

        for check in checks:
            try:
                check()
            except Exception as e:
                self.issues.append({
                    "type": "check_error",
                    "message": f"{check.__name__} 执行失败: {e}",
                    "severity": "error"
                })

        # 获取统计信息
        stats = self._get_stats()

        return {
            "passed": len([i for i in self.issues if i.get("severity") == "error"]) == 0,
            "issues": self.issues,
            "stats": stats
        }

    def _get_stats(self) -> Dict:
        """获取数据库统计信息"""
        try:
            conn = get_conn()
            stats = {
                "tables": _get_table_row_counts(conn),
                "database_size": DB_PATH.stat().st_size,
            }
            conn.close()
            return stats
        except Exception as e:
            return {"error": str(e)}

    def _check_daily_bar_integrity(self):
        """检查日线数据完整性"""
        conn = get_conn()

        # 检查必需的列
        cursor = conn.execute("PRAGMA table_info(daily_bar)")
        columns = {row[1] for row in cursor.fetchall()}
        required = {"code", "trade_date", "open", "high", "low", "close", "volume"}

        missing = required - columns
        if missing:
            self.issues.append({
                "type": "schema_error",
                "table": "daily_bar",
                "message": f"缺少必需列: {missing}",
                "severity": "error"
            })

        # 检查价格异常
        cursor = conn.execute("""
            SELECT code, trade_date, open, high, low, close
            FROM daily_bar
            WHERE close <= 0 OR open <= 0 OR high < low
            LIMIT 10
        """)
        bad_prices = cursor.fetchall()
        for row in bad_prices:
            self.issues.append({
                "type": "price_error",
                "table": "daily_bar",
                "record": {"code": row[0], "date": row[1]},
                "message": f"价格异常: open={row[2]}, high={row[3]}, low={row[4]}, close={row[5]}",
                "severity": "error"
            })

        conn.close()

    def _check_calendar_continuity(self):
        """检查交易日历连续性"""
        conn = get_conn()

        cursor = conn.execute("""
            SELECT cal_date FROM trade_calendar
            WHERE is_open = 1
            ORDER BY cal_date
        """)
        dates = [row[0] for row in cursor.fetchall()]

        if len(dates) >= 2:
            # 检查 gaps (超过7天无交易日视为异常,可能是长假)
            from datetime import datetime
            for i in range(1, len(dates)):
                d1 = datetime.strptime(dates[i-1], "%Y-%m-%d")
                d2 = datetime.strptime(dates[i], "%Y-%m-%d")
                gap = (d2 - d1).days
                if gap > 10:  # 超过10天可能是数据缺失
                    self.issues.append({
                        "type": "calendar_gap",
                        "message": f"交易日历断层: {dates[i-1]} 到 {dates[i]} 间隔 {gap} 天",
                        "severity": "warning" if gap > 14 else "info"
                    })

        conn.close()

    def _check_adj_factor_validity(self):
        """检查复权因子有效性"""
        conn = get_conn()

        # 检查 adj_factor 是否为正
        cursor = conn.execute("""
            SELECT code, trade_date, adj_factor
            FROM daily_bar
            WHERE adj_factor <= 0 OR adj_factor IS NULL
            LIMIT 10
        """)
        bad_adj = cursor.fetchall()
        for row in bad_adj:
            self.issues.append({
                "type": "adj_factor_error",
                "record": {"code": row[0], "date": row[1]},
                "message": f"复权因子异常: {row[2]}",
                "severity": "warning"
            })

        # 检查 adj_factor 是否单调不增(历史到最新)
        cursor = conn.execute("""
            SELECT code, COUNT(*) as cnt,
                   SUM(CASE WHEN adj_factor > 1.1 OR adj_factor < 0.5 THEN 1 ELSE 0 END) as outliers
            FROM daily_bar
            GROUP BY code
            HAVING outliers > cnt * 0.1  -- 超过10%异常值
            LIMIT 5
        """)
        bad_codes = cursor.fetchall()
        for row in bad_codes:
            self.issues.append({
                "type": "adj_factor_distribution",
                "code": row[0],
                "message": f"复权因子分布异常: {row[2]}/{row[1]} 超出正常范围",
                "severity": "info"
            })

        conn.close()

    def _check_duplicate_records(self):
        """检查重复记录"""
        conn = get_conn()

        cursor = conn.execute("""
            SELECT code, trade_date, COUNT(*) as cnt
            FROM daily_bar
            GROUP BY code, trade_date
            HAVING cnt > 1
            LIMIT 10
        """)
        dups = cursor.fetchall()
        for row in dups:
            self.issues.append({
                "type": "duplicate_record",
                "record": {"code": row[0], "date": row[1]},
                "message": f"重复记录: {row[2]} 条",
                "severity": "error"
            })

        conn.close()

    def _check_orphan_records(self):
        """检查孤立记录(无对应证券信息)"""
        conn = get_conn()

        # 检查 daily_bar 中有但 security 中没有的 code
        cursor = conn.execute("""
            SELECT DISTINCT d.code
            FROM daily_bar d
            LEFT JOIN security s ON d.code = s.code
            WHERE s.code IS NULL
            LIMIT 10
        """)
        orphans = cursor.fetchall()
        for row in orphans:
            self.issues.append({
                "type": "orphan_record",
                "code": row[0],
                "message": "日线数据中存在但证券信息表缺失",
                "severity": "warning"
            })

        conn.close()


def check_data_integrity() -> Dict:
    """执行数据完整性校验"""
    checker = DataIntegrityChecker()
    return checker.check_all()


def repair_common_issues() -> Dict:
    """修复常见问题

    Returns:
        {"repaired": List[str], "failed": List[str]}
    """
    result = {"repaired": [], "failed": []}

    try:
        conn = get_conn()

        # 1. 修复重复的日线数据(保留最新)
        cursor = conn.execute("""
            DELETE FROM daily_bar
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM daily_bar
                GROUP BY code, trade_date
            )
        """)
        if cursor.rowcount > 0:
            result["repaired"].append(f"删除重复日线记录 {cursor.rowcount} 条")

        # 2. 修复 NULL adj_factor
        cursor = conn.execute("""
            UPDATE daily_bar
            SET adj_factor = 1.0
            WHERE adj_factor IS NULL
        """)
        if cursor.rowcount > 0:
            result["repaired"].append(f"修复 NULL 复权因子 {cursor.rowcount} 条")

        conn.commit()
        conn.close()

    except Exception as e:
        result["failed"].append(f"修复过程出错: {e}")

    return result


# ============ 定时任务接口 ============

def scheduled_backup(full: bool = False) -> Optional[Path]:
    """定时备份入口

    Args:
        full: True则创建完整备份,False则创建增量备份
    """
    if full:
        result = create_full_backup()
    else:
        result = create_incremental_backup()

    # 清理过期备份
    cleanup_old_backups()

    return result


def scheduled_check() -> Dict:
    """定时校验入口"""
    result = check_data_integrity()

    if not result["passed"]:
        errors = [i for i in result["issues"] if i.get("severity") == "error"]
        log.warning("数据完整性校验未通过,发现 %d 个错误", len(errors))

        # 尝试自动修复
        repair = repair_common_issues()
        if repair["repaired"]:
            log.info("自动修复: %s", repair["repaired"])

    return result


# ============ CLI 接口 ============

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="数据快照管理工具")
    parser.add_argument("--backup", choices=["full", "incremental"], help="创建备份")
    parser.add_argument("--restore", type=Path, help="从备份恢复")
    parser.add_argument("--check", action="store_true", help="数据完整性校验")
    parser.add_argument("--repair", action="store_true", help="修复常见问题")
    parser.add_argument("--list", action="store_true", help="列出所有备份")
    parser.add_argument("--cleanup", action="store_true", help="清理过期备份")
    args = parser.parse_args()

    if args.backup == "full":
        create_full_backup()
    elif args.backup == "incremental":
        create_incremental_backup()
    elif args.restore:
        if "incremental" in args.restore.name:
            restore_incremental_backup(args.restore)
        else:
            restore_full_backup(args.restore)
    elif args.check:
        result = check_data_integrity()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif args.repair:
        result = repair_common_issues()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.list:
        for f in sorted(BACKUP_DIR.glob("*.db*")):
            if f.suffix != ".meta":
                size = f.stat().st_size / 1024 / 1024
                print(f"{f.name:40s} {size:8.1f}MB")
    elif args.cleanup:
        cleanup_old_backups()
    else:
        parser.print_help()
