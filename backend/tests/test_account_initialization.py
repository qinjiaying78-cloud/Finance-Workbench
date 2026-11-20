from fastapi.testclient import TestClient

from backend import database
from backend.main import app


def test_cold_start_seeds_default_cas_chart_and_new_books(tmp_path, monkeypatch):
    db_file = tmp_path / "cold-start.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)

    # The application lifespan performs the same initialization as real startup.
    with TestClient(app) as client:
        assert db_file.exists()
        default_rows = client.get("/api/accounts", params={"account_set_id": "default"}).json()["items"]
        assert len(default_rows) >= 70
        by_code = {row["code"]: row for row in default_rows}
        for code, name, direction in (
            ("1001", "库存现金", "借"), ("1002", "银行存款", "借"),
            ("1122", "应收账款", "借"), ("1405", "库存商品", "借"),
            ("1601", "固定资产", "借"), ("2202", "应付账款", "贷"),
            ("2221", "应交税费", "贷"), ("4001", "实收资本", "贷"),
            ("6001", "主营业务收入", "贷"), ("6401", "主营业务成本", "借"),
            ("6602", "管理费用", "借"),
        ):
            assert by_code[code]["name"] == name
            assert by_code[code]["direction"] == direction

        created = client.put(
            "/api/storage/account-sets/new-empty-book",
            json={"name": "新建空白账套", "is_demo": False},
        )
        assert created.status_code == 200, created.text
        new_rows = client.get(
            "/api/accounts", params={"account_set_id": "new-empty-book"}
        ).json()["items"]
        assert len(new_rows) == len(default_rows)
        assert {row["code"] for row in new_rows} == set(by_code)

        # Repeated startup/upsert is idempotent and must not duplicate accounts.
        client.put(
            "/api/storage/account-sets/new-empty-book",
            json={"name": "新建空白账套", "is_demo": False},
        )
        repeated = client.get(
            "/api/accounts", params={"account_set_id": "new-empty-book"}
        ).json()["items"]
        assert len(repeated) == len(new_rows)
