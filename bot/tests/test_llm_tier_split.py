"""PR10: touchpoint → model routing respects LLM_TIER_SPLIT_ENABLED."""
import unittest


try:
    from bot import llm
    _OK = True
except ImportError:
    llm = None
    _OK = False


@unittest.skipUnless(_OK, "llm module deps not available")
class ModelForTests(unittest.TestCase):
    def test_flag_off_returns_fallback(self):
        cfg = {"LLM_TIER_SPLIT_ENABLED": False,
                "LLM_MODEL_VETO": "anthropic/claude-haiku-4.5"}
        self.assertEqual(
            llm._model_for("entry_veto", "fallback-model", cfg),
            "fallback-model",
        )

    def test_flag_on_reads_config(self):
        cfg = {"LLM_TIER_SPLIT_ENABLED": True,
                "LLM_MODEL_VETO": "anthropic/claude-haiku-4.5"}
        self.assertEqual(
            llm._model_for("entry_veto", "fallback", cfg),
            "anthropic/claude-haiku-4.5",
        )

    def test_flag_on_missing_key_returns_fallback(self):
        cfg = {"LLM_TIER_SPLIT_ENABLED": True}  # no LLM_MODEL_VETO key
        self.assertEqual(llm._model_for("entry_veto", "fallback", cfg), "fallback")

    def test_empty_string_value_returns_fallback(self):
        cfg = {"LLM_TIER_SPLIT_ENABLED": True, "LLM_MODEL_VETO": ""}
        self.assertEqual(llm._model_for("entry_veto", "fallback", cfg), "fallback")

    def test_unknown_touchpoint_returns_fallback(self):
        cfg = {"LLM_TIER_SPLIT_ENABLED": True}
        self.assertEqual(llm._model_for("mystery", "fallback", cfg), "fallback")

    def test_each_touchpoint_has_cfg_key_mapping(self):
        expected = {
            "entry_veto":    "LLM_MODEL_VETO",
            "market_regime": "LLM_MODEL_REGIME",
            "rank":          "LLM_MODEL_RANKING",
            "stop_adjust":   "LLM_MODEL_STOP_ADJUST",
            "exit_veto":     "LLM_MODEL_EXIT_VETO",
            "news_watch":    "LLM_MODEL_NEWS",
        }
        self.assertEqual(llm._MODEL_CFG_KEY_BY_TOUCHPOINT, expected)


if __name__ == "__main__":
    unittest.main()
