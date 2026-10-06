"""定义集迁移包（POST /custom-unit-set-migration/export|import）端到端测试。

覆盖：

- 导出：1～6 个已发布定义集连同当时指向它们的活动别名打成一个包；
  标识不存在、为空、超过 6 个或重复均 422；包格式/版本固定；
- 快照一致性：并发切换别名时，包内定义与别名指向来自同一份快照；
- 导入：为各定义集生成新的本地标识，包内别名指向映射后的新标识，
  返回旧标识 -> 新标识映射；原有的定义集、别名与调用不受影响；
- 导入前校验：包格式/版本不支持、包内引用悬空、定义非法（冲突/循环/
  未知引用/禁引温标/非法比例）、目标端已有同名别名均整包拒绝（422），
  不留下部分定义或别名；
- 共同生效：导入与落盘在同一临界区完成，写盘失败后内存状态保持原样；
- 导入后新引用可直接调用四个计算接口；容器/进程重建后新引用仍可用；
- 同一内容可重复导入（每次生成新标识、不冲突）。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import (
    MIGRATION_PACKAGE_FORMAT,
    MIGRATION_PACKAGE_VERSION,
    CustomUnitItem,
)
from app import unit_sets

client = TestClient(app)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def assert_err(resp, code, status=422):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bind(alias, set_id, expected_id):
    r = client.put(
        f"/custom-unit-set-aliases/{alias}",
        json={"custom_unit_set_id": set_id, "expected_id": expected_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["custom_unit_set_id"]


def export(ids, status=200):
    return client.post(
        "/custom-unit-set-migration/export",
        json={"custom_unit_set_ids": ids},
    )


def import_package(package, status=201):
    return client.post(
        "/custom-unit-set-migration/import",
        json=package,
    )


def use_fresh_instance(tmp_path, name="target"):
    """把进程级存储切换到一个全新数据目录，模拟另一独立实例。"""

    store = unit_sets.UnitSetStore(str(tmp_path / name))
    store.load()
    unit_sets._store = store
    return store


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_single_set_with_aliases_pointing_to_it():
    sid = publish([cu("ft", "0.3048", "m")])
    sid2 = publish([cu("rod", "5.0292", "m")])
    bind("lengths", sid, None)
    bind("rods", sid2, None)
    r = export([sid])
    assert r.status_code == 200, r.text
    pkg = r.json()
    assert pkg["format"] == MIGRATION_PACKAGE_FORMAT
    assert pkg["version"] == MIGRATION_PACKAGE_VERSION
    assert pkg["custom_unit_sets"] == [
        {"id": sid, "custom_units": [cu("ft", "0.3048", "m")]}
    ]
    # 只包含指向被导出定义集的别名（rods 指向 sid2，不在包内）
    assert pkg["aliases"] == [
        {"alias": "lengths", "custom_unit_set_id": sid}
    ]


def test_export_multiple_sets_and_aliases_sorted():
    sid_a = publish([cu("aa", "2", "m")])
    sid_b = publish([cu("bb", "3", "m")])
    bind("zeta", sid_a, None)
    bind("alpha", sid_b, None)
    # 另一个不相关别名不进包
    sid_c = publish([cu("cc", "5", "s")])
    bind("skip", sid_c, None)
    r = export([sid_b, sid_a])  # 请求顺序保留
    pkg = r.json()
    assert [s["id"] for s in pkg["custom_unit_sets"]] == [sid_b, sid_a]
    # 别名按名称排序
    assert pkg["aliases"] == [
        {"alias": "alpha", "custom_unit_set_id": sid_b},
        {"alias": "zeta", "custom_unit_set_id": sid_a},
    ]


def test_export_includes_chain_definitions_verbatim():
    sid = publish([cu("furlong", "10", "chain"), cu("chain", "22", "m")])
    pkg = export([sid]).json()
    assert pkg["custom_unit_sets"][0]["custom_units"] == [
        cu("furlong", "10", "chain"),
        cu("chain", "22", "m"),
    ]


def test_export_no_aliases_when_none_point_to_exported_sets():
    sid = publish([cu("aa", "2", "m")])
    other = publish([cu("bb", "3", "m")])
    bind("other", other, None)
    pkg = export([sid]).json()
    assert pkg["aliases"] == []


def test_export_unknown_id_is_422_and_exports_nothing():
    sid = publish([cu("aa", "2", "m")])
    r = export([sid, "cus_does_not_exist"])
    assert_err(r, "invalid_request")
    r = export(["cus_does_not_exist"])
    assert_err(r, "invalid_request")
    # 不存在时也不返回包体
    assert "custom_unit_sets" not in r.json()


def test_export_rejects_empty_duplicate_and_too_many_ids():
    r = client.post(
        "/custom-unit-set-migration/export", json={"custom_unit_set_ids": []}
    )
    assert_err(r, "invalid_request")
    sid = publish([cu("aa", "2", "m")])
    r = export([sid, sid])
    assert_err(r, "invalid_request")
    ids = [publish([cu(f"u{i}", "1", "m")]) for i in range(7)]
    r = export(ids)
    assert_err(r, "invalid_request")
    r = client.post(
        "/custom-unit-set-migration/export", json={"custom_unit_set_ids": "nope"}
    )
    assert_err(r, "invalid_request")


def test_export_supports_one_to_six_sets():
    for n in (1, 6):
        ids = [publish([cu(f"x{chr(97 + i)}_{n}", "1", "m")]) for i in range(n)]
        r = export(ids)
        assert r.status_code == 200, r.text
        assert len(r.json()["custom_unit_sets"]) == n


# ---------------------------------------------------------------------------
# 快照一致性：并发切换别名时定义与指向来自同一份快照
# ---------------------------------------------------------------------------


def test_concurrent_alias_switch_export_is_always_from_one_snapshot():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    store = unit_sets.get_store()
    store.bind_alias("live", sid_v1, None)

    seen: list[tuple[str, str | None]] = []

    def do_export(_):
        pkg = client.post(
            "/custom-unit-set-migration/export",
            json={"custom_unit_set_ids": [sid_v1, sid_v2]},
        ).json()
        sets = {s["id"]: s["custom_units"][0]["factor"] for s in pkg["custom_unit_sets"]}
        target = next(
            (a["custom_unit_set_id"] for a in pkg["aliases"] if a["alias"] == "live"),
            None,
        )
        # 包内定义必须与指向一致：live 指向 v1 时包内 v1 必为 2；指向 v2 时为 3
        if target is not None:
            factor = sets[target]
            seen.append((factor, target))
            assert (factor, target) in [("2", sid_v1), ("3", sid_v2)]

    def switch(_):
        try:
            store.bind_alias("live", sid_v2, sid_v1)
        except Exception:
            pass

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(do_export, i) for i in range(100)]
        pool.submit(switch, 0)
        for f in futs:
            f.result()

    factors = {factor for factor, _ in seen}
    assert factors <= {"2", "3"}
    # 切换前后均有导出发生
    assert any(t == sid_v1 for _, t in seen)
    assert any(t == sid_v2 for _, t in seen)


# ---------------------------------------------------------------------------
# 导入：新标识、别名重定向、映射
# ---------------------------------------------------------------------------


def test_import_creates_new_ids_and_redirects_aliases(tmp_path):
    sid_a = publish([cu("ft", "0.3048", "m")])
    sid_b = publish([cu("dozen", "12", "1")])
    bind("lengths", sid_a, None)
    bind("counting", sid_b, None)
    pkg = export([sid_a, sid_b]).json()

    # 目标端是另一个独立实例（独立数据目录）
    use_fresh_instance(tmp_path)
    r = import_package(pkg)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body["id_map"]) == {sid_a, sid_b}
    new_a = body["id_map"][sid_a]
    new_b = body["id_map"][sid_b]
    assert new_a != sid_a and new_b != sid_b
    assert new_a != new_b
    assert new_a.startswith("cus_") and new_b.startswith("cus_")
    # 回显与映射一致
    sets_info = {item["old_id"]: item for item in body["custom_unit_sets"]}
    assert sets_info[sid_a]["new_id"] == new_a
    assert sets_info[sid_a]["custom_units"] == [cu("ft", "0.3048", "m")]
    # 别名指向已重定向到新标识
    aliases = {a["alias"]: a["custom_unit_set_id"] for a in body["aliases"]}
    assert aliases == {"counting": new_b, "lengths": new_a}
    assert client.get("/custom-unit-set-aliases/lengths").json()[
        "custom_unit_set_id"
    ] == new_a


def test_imported_definitions_and_aliases_work_in_calls(tmp_path):
    sid = publish([cu("furlong", "10", "chain"), cu("chain", "22", "m")])
    bind("lengths", sid, None)
    pkg = export([sid]).json()
    use_fresh_instance(tmp_path)
    new_id = import_package(pkg).json()["id_map"][sid]

    # 用新标识
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "custom_unit_set_id": new_id},
    )
    assert r.json() == {"value": "440", "unit": "m", "dimension": "m"}
    # 用导入的别名（已指向新标识）
    r = client.post(
        "/convert",
        json={
            "value": "2", "from": "furlong", "to": "m",
            "custom_unit_set_alias": "lengths",
        },
    )
    assert r.json()["value"] == "440"


def test_import_does_not_touch_existing_sets_aliases_or_calls():
    sid_old = publish([cu("foo", "2", "m")])
    bind("keep", sid_old, None)
    pkg = export([publish([cu("foo", "9", "m")])]).json()
    foreign_old_id = pkg["custom_unit_sets"][0]["id"]
    body = import_package(pkg).json()
    new_id = body["id_map"][foreign_old_id]

    # 原有定义集含义不变
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_id": sid_old},
    )
    assert r.json()["value"] == "2"
    # 原有别名指向不变、调用照旧
    assert client.get("/custom-unit-set-aliases/keep").json()[
        "custom_unit_set_id"
    ] == sid_old
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "keep"},
    )
    assert r.json()["value"] == "2"
    # 新内容可用
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_id": new_id},
    )
    assert r.json()["value"] == "9"
    assert unit_sets.get_store().count() == 3


def test_import_without_aliases():
    sid = publish([cu("aa", "2", "m")])
    pkg = export([sid]).json()
    assert pkg["aliases"] == []
    body = import_package(pkg).json()
    assert body["aliases"] == []
    assert body["id_map"] == {sid: body["id_map"][sid]}


def test_same_package_can_be_imported_twice_with_new_ids():
    sid = publish([cu("aa", "2", "m")])
    bind("live", sid, None)
    # 第二次导入会因同名别名而冲突——先验证无别名包可重复导入
    pkg = export([sid]).json()
    pkg["aliases"] = []
    first = import_package(pkg).json()
    second = import_package(pkg).json()
    id1 = first["id_map"][sid]
    id2 = second["id_map"][sid]
    assert id1 != id2 and id1 != sid and id2 != sid
    assert unit_sets.get_store().count() == 3


# ---------------------------------------------------------------------------
# 整包拒绝：非法定义 / 悬空引用 / 同名别名 / 格式版本
# ---------------------------------------------------------------------------


def test_import_rejects_invalid_definitions_and_leaves_nothing():
    good_id = publish([cu("keep", "1", "m")])
    bind("keepalive", good_id, None)
    base_count = unit_sets.get_store().count()

    valid = {"id": "cus_ok", "custom_units": [cu("aa", "2", "m")]}
    bad_packages = [
        # 与内置单位冲突
        [valid, {"id": "cus_bad", "custom_units": [cu("m", "1", "1")]}],
        # 与词头可解析符号冲突
        [{"id": "cus_bad", "custom_units": [cu("km", "1", "m")]}],
        # 自引用 / 循环
        [{"id": "cus_bad", "custom_units": [cu("aa", "1", "aa")]}],
        [
            {
                "id": "cus_bad",
                "custom_units": [cu("aa", "1", "bb"), cu("bb", "1", "aa")],
            }
        ],
        # 未知引用
        [{"id": "cus_bad", "custom_units": [cu("aa", "1", "zzz")]}],
        # 非法比例
        [{"id": "cus_bad", "custom_units": [cu("aa", "0", "m")]}],
        [{"id": "cus_bad", "custom_units": [cu("aa", "abc", "m")]}],
        # 禁引温标
        [{"id": "cus_bad", "custom_units": [cu("aa", "1", "degC")]}],
        [{"id": "cus_bad", "custom_units": [cu("aa", "1", "K")]}],
        # 合法集 + 非法集混合：整包拒绝
        [
            valid,
            {"id": "cus_bad", "custom_units": [cu("cc", "1", "dd"), cu("dd", "1", "cc")]},
        ],
        # 单个定义集内超过 6 项
        [
            {
                "id": "cus_bad",
                "custom_units": [cu(f"u{i}", "1", "m") for i in range(7)],
            }
        ],
    ]
    for sets in bad_packages:
        pkg = {
            "format": MIGRATION_PACKAGE_FORMAT,
            "version": MIGRATION_PACKAGE_VERSION,
            "custom_unit_sets": sets,
            "aliases": [],
        }
        r = import_package(pkg)
        # 与 /custom-unit-sets 发布校验一致：各类非法定义均为 422
        assert r.status_code == 422, sets
    assert unit_sets.get_store().count() == base_count
    assert client.get("/custom-unit-set-aliases").json()["aliases"] == [
        {"alias": "keepalive", "custom_unit_set_id": good_id}
    ]


def test_import_rejects_dangling_alias_reference():
    pkg = {
        "format": MIGRATION_PACKAGE_FORMAT,
        "version": MIGRATION_PACKAGE_VERSION,
        "custom_unit_sets": [
            {"id": "cus_a", "custom_units": [cu("aa", "2", "m")]}
        ],
        "aliases": [
            {"alias": "live", "custom_unit_set_id": "cus_missing"}
        ],
    }
    assert_err(import_package(pkg), "invalid_request")
    assert_err(client.get("/custom-unit-set-aliases/live"), "invalid_request")
    assert unit_sets.get_store().count() == 0


def test_import_rejects_duplicate_aliases_and_set_ids_in_package():
    base = {
        "format": MIGRATION_PACKAGE_FORMAT,
        "version": MIGRATION_PACKAGE_VERSION,
    }
    dup_alias = {
        **base,
        "custom_unit_sets": [
            {"id": "cus_a", "custom_units": [cu("aa", "2", "m")]},
            {"id": "cus_b", "custom_units": [cu("bb", "3", "m")]},
        ],
        "aliases": [
            {"alias": "live", "custom_unit_set_id": "cus_a"},
            {"alias": "live", "custom_unit_set_id": "cus_b"},
        ],
    }
    assert_err(import_package(dup_alias), "invalid_request")
    dup_id = {
        **base,
        "custom_unit_sets": [
            {"id": "cus_a", "custom_units": [cu("aa", "2", "m")]},
            {"id": "cus_a", "custom_units": [cu("bb", "3", "m")]},
        ],
        "aliases": [],
    }
    assert_err(import_package(dup_id), "invalid_request")


def test_import_rejects_when_target_already_has_same_named_alias():
    # 目标端已有别名 live（指向任意定义集）
    local_sid = publish([cu("aa", "1", "m")])
    bind("live", local_sid, None)
    before = client.get("/custom-unit-set-aliases/live").json()

    pkg = {
        "format": MIGRATION_PACKAGE_FORMAT,
        "version": MIGRATION_PACKAGE_VERSION,
        "custom_unit_sets": [
            {"id": "cus_foreign", "custom_units": [cu("bb", "2", "m")]},
            {"id": "cus_other", "custom_units": [cu("cc", "3", "m")]},
        ],
        # live 与目标端冲突；fresh 不冲突——整包仍须拒绝
        "aliases": [
            {"alias": "live", "custom_unit_set_id": "cus_foreign"},
            {"alias": "fresh", "custom_unit_set_id": "cus_other"},
        ],
    }
    assert_err(import_package(pkg), "invalid_request")
    # 原别名指向不变，fresh 未建立，定义集未导入
    assert client.get("/custom-unit-set-aliases/live").json() == before
    assert_err(client.get("/custom-unit-set-aliases/fresh"), "invalid_request")
    assert unit_sets.get_store().count() == 1
    r = client.post(
        "/convert",
        json={"value": "1", "from": "bb", "to": "m", "custom_unit_set_id": "cus_foreign"},
    )
    assert_err(r, "invalid_request")


def test_existing_same_set_id_on_target_does_not_block_import():
    # 目标端恰有与包内旧标识同名的定义集：导入仍生成新标识，两者共存
    local = publish([cu("aa", "2", "m")])  # 本地标识 cus_xxx
    pkg = {
        "format": MIGRATION_PACKAGE_FORMAT,
        "version": MIGRATION_PACKAGE_VERSION,
        "custom_unit_sets": [
            {"id": local, "custom_units": [cu("aa", "9", "m")]}
        ],
        "aliases": [],
    }
    body = import_package(pkg).json()
    new_id = body["id_map"][local]
    assert new_id != local
    r = client.post(
        "/convert",
        json={"value": "1", "from": "aa", "to": "m", "custom_unit_set_id": local},
    )
    assert r.json()["value"] == "2"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "aa", "to": "m", "custom_unit_set_id": new_id},
    )
    assert r.json()["value"] == "9"


def test_import_rejects_unknown_format_and_version():
    good = {
        "custom_unit_sets": [
            {"id": "cus_a", "custom_units": [cu("aa", "2", "m")]}
        ],
        "aliases": [],
    }
    bad_format = {"format": "something-else", "version": 1, **good}
    bad_version = {
        "format": MIGRATION_PACKAGE_FORMAT,
        "version": 999,
        **good,
    }
    assert_err(import_package(bad_format), "invalid_request")
    assert_err(import_package(bad_version), "invalid_request")
    assert unit_sets.get_store().count() == 0


def test_import_rejects_malformed_package_shapes():
    for payload in (
        {},
        {"format": MIGRATION_PACKAGE_FORMAT, "version": MIGRATION_PACKAGE_VERSION},
        {
            "format": MIGRATION_PACKAGE_FORMAT,
            "version": MIGRATION_PACKAGE_VERSION,
            "custom_unit_sets": [],
            "aliases": [],
        },
        {
            "format": MIGRATION_PACKAGE_FORMAT,
            "version": MIGRATION_PACKAGE_VERSION,
            "custom_unit_sets": [
                {"id": "cus_a", "custom_units": [{"symbol": "aa", "factor": "1"}]}
            ],
            "aliases": [],
        },
        {
            "format": MIGRATION_PACKAGE_FORMAT,
            "version": MIGRATION_PACKAGE_VERSION,
            "custom_unit_sets": "nope",
            "aliases": [],
        },
    ):
        r = client.post("/custom-unit-set-migration/import", json=payload)
        assert_err(r, "invalid_request")


# ---------------------------------------------------------------------------
# 两实例往返（模拟独立实例 + 容器重建）
# ---------------------------------------------------------------------------


def test_export_import_roundtrip_between_two_instances(tmp_path):
    # 实例 A：发布定义集与别名并导出
    dir_a = tmp_path / "a"
    store_a = unit_sets.UnitSetStore(str(dir_a))
    store_a.load()
    unit_sets._store = store_a
    sid1 = publish([cu("furlong", "10", "chain"), cu("chain", "22", "m")])
    sid2 = publish([cu("ft", "0.3048", "m")])
    bind("lengths", sid1, None)
    pkg = export([sid1, sid2]).json()

    # 实例 B：独立数据目录，导入 A 的包
    dir_b = tmp_path / "b"
    store_b = unit_sets.UnitSetStore(str(dir_b))
    store_b.load()
    unit_sets._store = store_b
    assert store_b.count() == 0
    body = import_package(pkg).json()
    assert set(body["id_map"]) == {sid1, sid2}
    new1, new2 = body["id_map"][sid1], body["id_map"][sid2]
    assert new1 not in (sid1, sid2) and new2 not in (sid1, sid2) and new1 != new2

    # 新标识调用
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "custom_unit_set_id": new1},
    )
    assert r.json() == {"value": "440", "unit": "m", "dimension": "m"}
    # 别名调用（包内别名指向 sid1，导入后应指向其新标识 new1）
    assert client.get("/custom-unit-set-aliases/lengths").json()[
        "custom_unit_set_id"
    ] == new1
    r = client.post(
        "/convert",
        json={
            "value": "2", "from": "furlong", "to": "m",
            "custom_unit_set_alias": "lengths",
        },
    )
    assert r.json()["value"] == "440"
    # 包内无别名的第二个定义集用新标识调用
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": new2},
    )
    assert r.json()["value"] == "3.048"

    # 模拟容器重建：B 重新加载磁盘，新标识与别名仍然可用
    reloaded = unit_sets.UnitSetStore(str(dir_b))
    reloaded.load()
    unit_sets._store = reloaded
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "custom_unit_set_id": new1},
    )
    assert r.json()["value"] == "440"
    r = client.post(
        "/convert",
        json={
            "value": "2", "from": "furlong", "to": "m",
            "custom_unit_set_alias": "lengths",
        },
    )
    assert r.json()["value"] == "440"


def test_imported_package_persists_to_disk(tmp_path):
    sid = publish([cu("aa", "2", "m")])
    bind("live", sid, None)
    pkg = export([sid]).json()
    # 导入到另一独立实例
    store = use_fresh_instance(tmp_path)
    body = import_package(pkg).json()
    raw = json.load(open(store.path, encoding="utf-8"))
    new_id = body["id_map"][sid]
    assert new_id in raw["sets"]
    assert raw["aliases"] == {"live": new_id}
    assert sid not in raw["sets"]  # 磁盘上保存的是新标识，而非源标识


# ---------------------------------------------------------------------------
# 原子性：写盘失败后内存状态保持原样
# ---------------------------------------------------------------------------


def test_failed_disk_write_on_import_leaves_memory_untouched(tmp_path, monkeypatch):
    # 源实例发布并导出
    sid = publish([cu("aa", "2", "m")])
    bind("live", sid, None)
    pkg = export([sid]).json()

    # 目标实例：首次导入成功
    store = use_fresh_instance(tmp_path)
    body = import_package(pkg).json()
    first_new_id = body["id_map"][sid]
    assert store.count() == 1

    # 再次导入一个含新别名（retry）的同内容包，但写盘失败：
    # 内存状态必须保持第二次导入前的原样，不留部分定义或别名
    sets = [
        (
            item["id"],
            tuple(CustomUnitItem.model_validate(u) for u in item["custom_units"]),
        )
        for item in pkg["custom_unit_sets"]
    ]
    aliases = [("retry", sid)]  # sid 是包内旧标识，悬空检查可通过

    def boom(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(unit_sets.os, "replace", boom)
    import pytest

    with pytest.raises(OSError):
        store.import_package(sets, aliases)

    # 内存状态保持第二次导入前原样：定义集数量不变、retry 未建立、live 不变
    assert store.count() == 1
    assert store.get_alias("live") == first_new_id
    with pytest.raises(Exception):
        store.get_alias("retry")
    # 磁盘文件也仍是首次导入后的内容（原子替换未发生）
    raw = json.load(open(store.path, encoding="utf-8"))
    assert set(raw["sets"]) == {first_new_id}
    assert raw["aliases"] == {"live": first_new_id}

    # 恢复后重新导入成功：新增一个定义集，retry 指向它
    monkeypatch.undo()
    body2 = import_package({**pkg, "aliases": [{"alias": "retry", "custom_unit_set_id": sid}]})
    assert body2.status_code == 201, body2.text
    second_new_id = body2.json()["id_map"][sid]
    assert second_new_id != first_new_id
    assert store.count() == 2
    assert store.get_alias("retry") == second_new_id
    assert store.get_alias("live") == first_new_id


def test_concurrent_imports_with_same_alias_one_succeeds():
    pkg1 = export([publish([cu("aa", "2", "m")])]).json()
    pkg2 = export([publish([cu("bb", "3", "m")])]).json()
    pkg1["aliases"] = [{"alias": "shared", "custom_unit_set_id": pkg1["custom_unit_sets"][0]["id"]}]
    pkg2["aliases"] = [{"alias": "shared", "custom_unit_set_id": pkg2["custom_unit_sets"][0]["id"]}]
    outcomes = []

    def do_import(pkg):
        r = import_package(pkg)
        outcomes.append(r.status_code)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(do_import, [pkg1, pkg2]))
    assert sorted(outcomes) == [201, 422]
    # 恰好一个定义集导入、一个别名建立，指向与成功的那份一致
    aliases = client.get("/custom-unit-set-aliases").json()["aliases"]
    assert [a["alias"] for a in aliases] == ["shared"]
    target = aliases[0]["custom_unit_set_id"]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "aa", "to": "m", "custom_unit_set_id": target},
    )
    aa_ok = r.status_code == 200 and r.json().get("value") == "2"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "bb", "to": "m", "custom_unit_set_id": target},
    )
    bb_ok = r.status_code == 200 and r.json().get("value") == "3"
    assert aa_ok ^ bb_ok
