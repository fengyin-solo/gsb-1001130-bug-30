"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
内部表（如下划线开头的回放事件表）不进运营概览，只给对应领域服务使用。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS

# 内部表前缀：领域事件/回放游标等辅助数据，不算业务台账模块
INTERNAL_PREFIX = "_"


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if not name.startswith(INTERNAL_PREFIX))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """事务一致性：给所有表拍深拷贝快照，配合 restore 做整组回滚。"""
        return {name: [dict(row) for row in rows] for name, rows in self._tables.items()}

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """按快照整体回滚；快照之后新建的表一并丢弃，保证事务不留半截数据。"""
        self._tables = {name: [dict(row) for row in rows] for name, rows in snapshot.items()}

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
