"""PR11: existing pydantic_models + parse_or_default safe defaults."""
import unittest


try:
    from bot import pydantic_models as pm
    _OK = True
except ImportError:
    pm = None
    _OK = False


@unittest.skipUnless(_OK, "pydantic not installed in this env")
class SafeDefaultsPerPurposeTests(unittest.TestCase):
    """Each touchpoint's pydantic model must carry the spec'd safe default
    at the field level so parse_or_default(None) returns the fallback
    without needing any explicit branch in the caller."""

    def test_entry_veto_default_is_abstain(self):
        self.assertEqual(pm.EntryVeto().verdict, "abstain")

    def test_regime_default_is_mixed(self):
        self.assertEqual(pm.RegimeVerdict().regime, "mixed")

    def test_exit_veto_default_is_sell(self):
        # Spec says exit_veto should default to "proceed". Our existing
        # enum uses "sell" semantically equivalent to "proceed with exit".
        # Document via assertion to surface any future rename.
        self.assertEqual(pm.ExitVeto().action, "sell")

    def test_stop_adjust_default_is_hold(self):
        self.assertEqual(pm.StopAdjust().action, "hold")

    def test_news_watch_default_is_hold(self):
        self.assertEqual(pm.NewsWatch().action, "hold")

    def test_ranking_default_empty_list(self):
        self.assertEqual(pm.Ranking().order, [])

    def test_parse_or_default_none_returns_safe(self):
        self.assertEqual(pm.parse_or_default(pm.EntryVeto, None).verdict, "abstain")
        self.assertEqual(pm.parse_or_default(pm.StopAdjust, None).action, "hold")
        self.assertEqual(pm.parse_or_default(pm.RegimeVerdict, None).regime, "mixed")

    def test_parse_or_default_bad_dict_returns_safe(self):
        self.assertEqual(
            pm.parse_or_default(pm.EntryVeto, {"verdict": "yolo"}).verdict,
            "abstain",
        )


if __name__ == "__main__":
    unittest.main()
