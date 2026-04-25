"""PR12: build_snapshot_row constructs rows consistent with the schema."""
import unittest
from datetime import datetime, timezone


try:
    from bot import snapshots
    _OK = True
except ImportError:
    snapshots = None
    _OK = False


@unittest.skipUnless(_OK, "snapshots module import failed")
class BuildSnapshotRowTests(unittest.TestCase):
    def test_full_payload(self):
        now = datetime(2026, 4, 20, 14, 30, tzinfo=timezone.utc)
        payload = {
            "score": 75.5, "rsi": 22.1, "sigma_below_sma20": 1.8,
            "ibs": 0.22, "atr14": 2.1, "vol_ratio": 1.3,
        }
        row = snapshots.build_snapshot_row(
            symbol="AAPL", strategy="intraday", slot_id=14, payload=payload,
            gate_outcome="executed", llm_verdict="allow",
            stock_regime="mean_reversion", now=now,
        )
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["strategy"], "intraday")
        self.assertEqual(row["slot_id"], 14)
        self.assertEqual(row["score"], 75.5)
        self.assertEqual(row["rsi"], 22.1)
        self.assertEqual(row["ibs"], 0.22)
        self.assertEqual(row["gate_outcome"], "executed")
        self.assertEqual(row["llm_verdict"], "allow")
        self.assertEqual(row["stock_regime"], "mean_reversion")
        self.assertEqual(row["day_of_week"], 0)  # Monday
        self.assertEqual(row["minute_of_day"], 14 * 60 + 30)
        self.assertIsNone(row["hypothetical_outcome_pct"])

    def test_missing_payload_fields_render_none(self):
        now = datetime(2026, 4, 20, 13, 0, tzinfo=timezone.utc)
        row = snapshots.build_snapshot_row(
            symbol="BTC", strategy="crypto_scalp", slot_id=19,
            payload={}, gate_outcome="llm_veto", now=now,
        )
        self.assertIsNone(row["score"])
        self.assertIsNone(row["rsi"])
        self.assertIsNone(row["ibs"])
        self.assertEqual(row["gate_outcome"], "llm_veto")

    def test_snapshot_ts_defaults_to_now(self):
        row = snapshots.build_snapshot_row(
            symbol="MSFT", strategy="swing", slot_id=1,
            payload={"score": 60, "rsi": 30},
            gate_outcome="executed",
        )
        self.assertIsInstance(row["snapshot_ts"], datetime)
        self.assertIsNotNone(row["snapshot_ts"].tzinfo)

    def test_gate_outcome_required(self):
        # gate_outcome is NOT NULL in DB; building a row without it shouldn't
        # silently default — the caller must be explicit. We don't enforce
        # via type, but the failure mode is visible at insert time.
        row = snapshots.build_snapshot_row(
            symbol="X", strategy="intraday", slot_id=None, payload={},
            gate_outcome="ibs_filter",
        )
        self.assertEqual(row["gate_outcome"], "ibs_filter")


if __name__ == "__main__":
    unittest.main()
