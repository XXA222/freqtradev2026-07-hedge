import unittest
from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect

from freqtrade.enums import PositionSide
from freqtrade.hedge.persistence import normalize_position_side, position_identity
from freqtrade.persistence.migrations import migrate_hedge_position_columns
from freqtrade.persistence.trade_model import LocalTrade, Order, Trade


class TestHedgePersistence(unittest.TestCase):
    def test_model_metadata_has_hedge_columns(self):
        trade_columns = Trade.__table__.c
        order_columns = Order.__table__.c

        self.assertIn("position_side", trade_columns)
        self.assertIn("position_side", order_columns)
        self.assertIn("position_action", order_columns)
        self.assertIn("action_group_id", order_columns)
        self.assertEqual(str(trade_columns.position_side.server_default.arg), "BOTH")
        self.assertEqual(str(order_columns.position_side.server_default.arg), "BOTH")

    def test_local_trade_position_identity_is_side_aware(self):
        long_trade = LocalTrade(
            pair="ETH/USDT:USDT",
            position_side=PositionSide.LONG.value,
            open_date=datetime.now(UTC),
            amount=1.0,
            open_rate=2000.0,
        )
        short_trade = LocalTrade(
            pair="ETH/USDT:USDT",
            position_side=PositionSide.SHORT.value,
            open_date=datetime.now(UTC),
            amount=1.0,
            open_rate=2000.0,
            is_short=True,
        )

        self.assertEqual(long_trade.position_key, ("ETH/USDT:USDT", "LONG"))
        self.assertEqual(short_trade.position_key, ("ETH/USDT:USDT", "SHORT"))
        self.assertNotEqual(long_trade.position_key, short_trade.position_key)

    def test_order_json_contains_hedge_audit_fields(self):
        order = Order(
            order_id="order-1",
            ft_order_side="buy",
            ft_pair="ETH/USDT:USDT",
            ft_amount=0.1,
            ft_price=2000.0,
            order_date=datetime.now(UTC),
            position_side="LONG",
            position_action="INCREASE",
            action_group_id="group-1",
        )
        result = order.to_json(entry_side="buy", minified=True)

        self.assertEqual(result["position_side"], "LONG")
        self.assertEqual(result["position_action"], "INCREASE")
        self.assertEqual(result["action_group_id"], "group-1")

    def test_legacy_sqlite_schema_is_migrated_idempotently(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE trades (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO trades (id) VALUES (1)")
            connection.exec_driver_sql("INSERT INTO orders (id) VALUES (1)")

        migrate_hedge_position_columns(engine)
        migrate_hedge_position_columns(engine)

        inspector = inspect(engine)
        trade_columns = {column["name"] for column in inspector.get_columns("trades")}
        order_columns = {column["name"] for column in inspector.get_columns("orders")}

        self.assertIn("position_side", trade_columns)
        self.assertTrue({"position_side", "position_action", "action_group_id"} <= order_columns)

        with engine.connect() as connection:
            trade_side = connection.exec_driver_sql(
                "SELECT position_side FROM trades WHERE id = 1"
            ).scalar_one()
            order_side = connection.exec_driver_sql(
                "SELECT position_side FROM orders WHERE id = 1"
            ).scalar_one()

        self.assertEqual(trade_side, "BOTH")
        self.assertEqual(order_side, "BOTH")

    def test_position_side_normalization_and_identity(self):
        self.assertEqual(normalize_position_side(None), "BOTH")
        self.assertEqual(normalize_position_side(PositionSide.LONG), "LONG")
        self.assertEqual(normalize_position_side("short"), "SHORT")
        self.assertEqual(
            position_identity("ETH/USDT:USDT", "long"),
            ("ETH/USDT:USDT", "LONG"),
        )


if __name__ == "__main__":
    unittest.main()
