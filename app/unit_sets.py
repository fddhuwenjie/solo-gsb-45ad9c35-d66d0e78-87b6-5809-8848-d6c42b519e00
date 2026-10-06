"""已发布的自定义单位定义集（custom unit sets）与活动别名（active aliases）。

通过 ``POST /custom-unit-sets`` 把一组（1～6 项）已经按现有规则校验并展开
通过的自定义单位定义**发布**为可复用的定义集：服务生成不可猜测的标识，
定义内容落盘保存且**不可修改**；后续 ``/convert``、``/evaluate``、``/solve``
可凭标识选用该组定义。

此外，已发布的定义集之上可以建立**活动别名**：调用方沿用同一个别名，
维护者通过 ``PUT /custom-unit-set-aliases/{alias}`` 把别名切换到新的不可变
定义集。切换采用乐观并发（expected_id）：

- 仅当别名当前指向与提交的 ``expected_id`` 一致时才生效（初建别名时
  ``expected_id`` 必须为 ``null``），否则返回 409 且**不改写原指向**；
- 目标定义集不存在时返回 422，同样不改变任何指向；
- 切换与落盘在同一把锁内共同生效：写盘失败时进程内指向保持原值
  （异常时回滚），不会出现“内存已切换、磁盘未切换”的中间状态；
- 别名与定义集保存在同一个 JSON 文件中，容器重建/进程重启后仍然有效；
  旧文件没有别名字段时按空别名加载，已发布定义集照常可用。

设计要点：

- 发布前先用 :class:`~app.custom_units.UnitScope` 完整校验并展开全部条目
  （与请求内 ``custom_units`` 完全相同的规则），任一条目非法则不写入任何文件；
- 存储为数据目录下的单个 JSON 文件，写入采用“临时文件 + 原子替换”，
  容器重建/进程重启后重新加载，已发布内容保持可用；
- 已发布内容不可变：只提供发布、读取与别名切换，没有更新/删除接口；
- 并发安全：所有读写由同一把可重入锁串行化；别名解析在请求开始时
  **一次性快照**当前指向，之后整个请求（源、目标及变量单位）都使用
  解析出的同一份定义——切换与计算并发时，单次请求只能完整使用旧版或
  新版，不会跨版本混用；
- 请求级换算仍构造独立的 :class:`~app.custom_units.UnitScope`，
  互不共享、不修改全局单位表。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from fractions import Fraction

from .config import DATA_DIR
from .custom_units import CustomUnitSpec, UnitScope
from .errors import AliasConflictError, ConversionError
from .schemas import CustomUnitItem

#: 持久化文件名（位于 :data:`~app.config.DATA_DIR` 下）。
STORE_FILENAME = "custom_unit_sets.json"

#: 标识前缀（custom unit set）+ 随机字节数。
_ID_PREFIX = "cus_"
_ID_BYTES = 16

#: 存储文件格式版本。
_STORE_VERSION = 1

#: 活动别名的词法：字母/数字/下划线/连字符，1～64 字符（与路径段兼容，
#: 不会与定义集标识 ``cus_...`` 所在的命名空间冲突）。
ALIAS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def invalid_request(message: str) -> ConversionError:
    """与请求体相关的非法情况，统一归入 ``invalid_request``（HTTP 422）。"""

    return ConversionError("invalid_request", message)


def unit_set_not_found(set_id: str) -> ConversionError:
    """引用的定义集标识不存在（HTTP 422）。"""

    return ConversionError(
        "invalid_request", f"custom_unit_set_id 引用的定义集不存在: {set_id!r}"
    )


def alias_target_not_found(alias: str, set_id: str) -> ConversionError:
    """别名切换的目标定义集不存在（HTTP 422，不改变任何指向）。"""

    return ConversionError(
        "invalid_request",
        f"别名 {alias!r} 无法绑定到不存在的定义集: {set_id!r}",
    )


def alias_not_found(alias: str) -> ConversionError:
    """引用/查询的活动别名不存在（HTTP 422）。"""

    return ConversionError("invalid_request", f"活动别名不存在: {alias!r}")


def invalid_alias_name(alias: str) -> ConversionError:
    """别名名称不合法（HTTP 422）。"""

    return ConversionError(
        "invalid_request",
        f"别名 {alias!r} 不合法：仅允许字母、数字、下划线与连字符，"
        "长度 1～64",
    )


def import_alias_exists(alias: str) -> ConversionError:
    """导入包中的别名在目标端已存在：整包拒绝（HTTP 422，不覆盖既有别名）。"""

    return ConversionError(
        "invalid_request",
        f"迁移包中的别名 {alias!r} 在目标实例已存在：为避免覆盖既有指向，"
        "整包拒绝导入，请先在源端或目标端处理该别名后重试",
    )


class UnitSetStore:
    """已发布定义集与活动别名的持久化存储（线程安全、原子落盘）。"""

    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, STORE_FILENAME)
        self._lock = threading.RLock()
        #: 标识 -> 已校验的定义项（按声明顺序）
        self._sets: dict[str, tuple[CustomUnitItem, ...]] = {}
        #: 活动别名 -> 当前指向的定义集标识（不可变定义集的引用）
        self._aliases: dict[str, str] = {}
        self._loaded = False

    @property
    def path(self) -> str:
        """持久化文件的完整路径。"""

        return self._path

    def load(self) -> None:
        """从数据目录加载已发布的定义集与活动别名；文件不存在时以空存储启动。

        文件存在但无法解析时抛出异常使启动失败（宁可不可用也不静默丢数据）。
        旧版本文件可能没有 ``aliases`` 字段：此时按空别名加载，已发布的
        定义集照常可用（向前兼容）。
        """

        with self._lock:
            if not os.path.exists(self._path):
                self._sets = {}
                self._aliases = {}
                self._loaded = True
                return
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
                raise ValueError(f"自定义单位定义集存储文件版本不被支持: {self._path}")
            sets: dict[str, tuple[CustomUnitItem, ...]] = {}
            for set_id, items in raw.get("sets", {}).items():
                sets[set_id] = tuple(
                    CustomUnitItem.model_validate(item) for item in items
                )
            aliases_raw = raw.get("aliases", {})
            if not isinstance(aliases_raw, dict) or not all(
                isinstance(name, str) and isinstance(target, str)
                for name, target in aliases_raw.items()
            ):
                raise ValueError(
                    f"自定义单位定义集存储文件的 aliases 字段格式非法: {self._path}"
                )
            # 只保留指向已存在定义集的别名：定义集不可删除，正常情况下不会
            # 出现悬空引用；若文件被外部破坏则丢弃悬空项而非启动失败。
            aliases = {
                name: target for name, target in aliases_raw.items() if target in sets
            }
            self._sets = sets
            self._aliases = aliases
            self._loaded = True

    def publish(self, items: list[CustomUnitItem]) -> str:
        """先按现有规则校验并展开全部条目，通过后发布并返回服务生成的标识。

        任一条目非法（重名/冲突/循环/未知引用/禁引温标/非法比例等）都会
        抛出 :class:`~app.errors.ConversionError`，此时不写入、不留任何
        可引用的定义集。
        """

        self._ensure_loaded()
        specs = [
            CustomUnitSpec(
                symbol=item.symbol,
                factor=Fraction(item.factor),
                base_unit=item.base_unit,
            )
            for item in items
        ]
        # 构造期间立即解析全部声明；非法即抛 422，发生在任何写入之前
        UnitScope(specs)

        with self._lock:
            while True:
                set_id = _ID_PREFIX + secrets.token_hex(_ID_BYTES)
                if set_id not in self._sets:
                    break
            self._sets[set_id] = tuple(items)
            self._persist_locked()
        return set_id

    def get(self, set_id: str) -> list[CustomUnitItem]:
        """取已发布的定义项；不存在时抛出 :class:`ConversionError`（422）。"""

        self._ensure_loaded()
        with self._lock:
            items = self._sets.get(set_id)
        if items is None:
            raise unit_set_not_found(set_id)
        return list(items)

    def build_scope(self, set_id: str) -> UnitScope:
        """按已发布的定义集构造一次独立的请求级解析上下文。"""

        items = self.get(set_id)
        specs = [
            CustomUnitSpec(
                symbol=item.symbol,
                factor=Fraction(item.factor),
                base_unit=item.base_unit,
            )
            for item in items
        ]
        return UnitScope(specs)

    # -- 活动别名 ---------------------------------------------------------

    def list_aliases(self) -> dict[str, str]:
        """返回全部活动别名及其当前指向（按别名排序的副本）。"""

        self._ensure_loaded()
        with self._lock:
            return dict(sorted(self._aliases.items()))

    def get_alias(self, alias: str) -> str:
        """查询别名当前指向的定义集标识；别名不存在时抛出 422。"""

        self._ensure_loaded()
        with self._lock:
            target = self._aliases.get(alias)
        if target is None:
            raise alias_not_found(alias)
        return target

    def bind_alias(
        self, alias: str, target_set_id: str, expected_id: str | None
    ) -> str:
        """把活动别名切换（或初建）到目标定义集——乐观并发（CAS）。

        - 别名名称不合法或目标定义集不存在：抛出 422，**不改变任何指向**；
        - 当前指向与 ``expected_id`` 不一致（初建须为 ``None``）：抛出 409
          :class:`~app.errors.AliasConflictError`，同样不改写原指向；
        - 切换与落盘在同一把锁内共同生效：先在内存中改指向，再原子写盘；
          写盘失败时回滚内存中的改动后重新抛出，进程内指向保持原值。
        """

        self._ensure_loaded()
        if not ALIAS_NAME_RE.match(alias):
            raise invalid_alias_name(alias)
        with self._lock:
            # 目标必须存在；先于 CAS 判定（任何情况下都不得改写原指向）
            if target_set_id not in self._sets:
                raise alias_target_not_found(alias, target_set_id)
            current = self._aliases.get(alias)
            if current != expected_id:
                raise AliasConflictError(alias, current, expected_id)
            if current == target_set_id:
                # 指向未变（幂等重放）：无需写盘
                return target_set_id
            previous = current
            self._aliases[alias] = target_set_id
            try:
                self._persist_locked()
            except Exception:
                # 写盘失败：回滚内存指向，切换与落盘必须共同生效
                if previous is None:
                    del self._aliases[alias]
                else:
                    self._aliases[alias] = previous
                raise
            return target_set_id

    def build_scope_for_alias(self, alias: str) -> UnitScope:
        """按活动别名当前指向的定义集构造一次独立的请求级解析上下文。

        请求开始时调用一次：先在锁内快照别名当前指向的标识，再加载对应
        定义。此后整个请求只使用该快照——即使别名在请求处理中被切换，
        本次请求也会完整使用旧版或新版定义，不会跨版本混用。
        别名不存在时抛出 422。
        """

        return self.build_scope(self.get_alias(alias))

    # -- 实例间迁移：导出 / 导入 -----------------------------------------

    def export_snapshot(
        self, set_ids: list[str]
    ) -> tuple[
        list[tuple[str, tuple[CustomUnitItem, ...]]], list[tuple[str, str]]
    ]:
        """在**同一份快照**中取出指定定义集与当时指向它们的活动别名。

        整个读取过程在同一把锁内完成：即使别名在导出请求处理期间被并发
        切换，包内的定义与别名指向也必然来自同一个一致状态，不可能出现
        “定义来自切换前、指向来自切换后”的混版包。任一标识不存在时抛出
        422 且不返回任何内容。

        返回 ``(sets, aliases)``：``sets`` 为按请求顺序排列的
        ``(标识, 定义项元组)``；``aliases`` 为按别名排序的
        ``(别名, 指向标识)``，仅包含指向被导出定义集的活动别名。
        """

        self._ensure_loaded()
        with self._lock:
            sets: list[tuple[str, tuple[CustomUnitItem, ...]]] = []
            for set_id in set_ids:
                items = self._sets.get(set_id)
                if items is None:
                    raise unit_set_not_found(set_id)
                sets.append((set_id, tuple(items)))
            wanted = set(set_ids)
            aliases = sorted(
                (name, target)
                for name, target in self._aliases.items()
                if target in wanted
            )
        return sets, aliases

    def import_package(
        self,
        sets: list[tuple[str, tuple[CustomUnitItem, ...]]],
        aliases: list[tuple[str, str]],
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """导入迁移包：为各定义集生成新本地标识，别名指向映射后的新标识。

        导入前先按现有规则完整校验：

        - 每个定义集都经过与发布完全相同的校验与展开（重名/冲突/循环/
          未知引用/禁引温标/非法比例等任一不合法即拒绝）；
        - 包内别名引用必须落在包内定义集上（悬空引用拒绝；schema 层
          先行校验，这里对编程式调用同样兜底）；
        - 包内任一名别名已存在于目标端（无论指向什么）即拒绝，**不覆盖**
          目标端既有别名。

        全部校验通过后才在同一临界区内：生成新标识、加入定义、重定向
        别名并**原子写盘一次**。任何写盘失败都会回滚内存中的全部改动
        （新增的定义集与别名一并移除），进程内状态与磁盘都保持导入前
        原样，不留下部分定义或别名。目标端已有**同名定义集标识**不冲突：
        导入始终生成新标识，原有定义集不受影响。

        返回 ``(id_map, imported_aliases)``：``id_map`` 为旧标识 -> 新
        本地标识的映射；``imported_aliases`` 为按别名排序的
        ``(别名, 新指向标识)``。
        """

        self._ensure_loaded()
        # 1) 锁外（且先于任何写入）按现有规则校验并展开每个定义集——
        #    纯计算，不触碰共享存储状态。
        for old_id, items in sets:
            specs = [
                CustomUnitSpec(
                    symbol=item.symbol,
                    factor=Fraction(item.factor),
                    base_unit=item.base_unit,
                )
                for item in items
            ]
            UnitScope(specs)

        with self._lock:
            known_old = {old_id for old_id, _ in sets}
            # 2) 别名悬空引用兜底（schema 解析的请求已保证，防御编程式调用）
            for alias, target in aliases:
                if target not in known_old:
                    raise invalid_request(
                        f"迁移包存在悬空别名引用：别名 {alias!r} 指向的定义集 "
                        f"标识 {target!r} 不在包内"
                    )
            # 3) 目标端已有同名别名：整包拒绝（不覆盖既有指向）
            for alias, _ in aliases:
                if alias in self._aliases:
                    raise import_alias_exists(alias)

            # 4) 全部校验通过：生成新标识并重定向别名，单次原子写盘共同生效
            id_map: dict[str, str] = {}
            added_sets: list[str] = []
            added_aliases: list[str] = []
            for old_id, items in sets:
                while True:
                    new_id = _ID_PREFIX + secrets.token_hex(_ID_BYTES)
                    if new_id not in self._sets and new_id not in id_map.values():
                        break
                self._sets[new_id] = tuple(items)
                added_sets.append(new_id)
                id_map[old_id] = new_id
            try:
                for alias, old_target in aliases:
                    self._aliases[alias] = id_map[old_target]
                    added_aliases.append(alias)
                self._persist_locked()
            except Exception:
                # 写盘失败：内存状态也必须保持导入前原样，不留部分定义或别名
                for alias in added_aliases:
                    self._aliases.pop(alias, None)
                for new_id in added_sets:
                    self._sets.pop(new_id, None)
                raise
        imported_aliases = sorted(
            (alias, id_map[old_target]) for alias, old_target in aliases
        )
        return id_map, imported_aliases

    def count(self) -> int:
        """已发布的定义集数量（主要供测试与运维观察）。"""

        self._ensure_loaded()
        with self._lock:
            return len(self._sets)

    # -- 内部 -------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _persist_locked(self) -> None:
        """把当前定义集与别名全量写入临时文件并原子替换（调用方须持锁）。"""

        os.makedirs(self._data_dir, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "sets": {
                set_id: [
                    {
                        "symbol": item.symbol,
                        "factor": str(item.factor),
                        "base_unit": item.base_unit,
                    }
                    for item in items
                ]
                for set_id, items in self._sets.items()
            },
            "aliases": dict(sorted(self._aliases.items())),
        }
        tmp_path = f"{self._path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)


#: 进程内共享的存储单例；由应用启动时初始化（也可在测试中重新初始化）。
_store: UnitSetStore | None = None
_store_lock = threading.RLock()


def init_store(data_dir: str = DATA_DIR) -> UnitSetStore:
    """（重新）初始化进程级存储单例并加载磁盘上的已发布定义集。"""

    global _store
    with _store_lock:
        store = UnitSetStore(data_dir)
        store.load()
        _store = store
    return store


def get_store() -> UnitSetStore:
    """获取进程级存储单例；尚未初始化时按默认数据目录初始化。"""

    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                init_store()
    assert _store is not None
    return _store
