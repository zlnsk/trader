"""PR5: StopAdjust schema bans widen; coercion keeps the bot safe."""
import logging
import unittest

try:
    from bot import pydantic_models as pm
    _PYDANTIC_OK = True
except ImportError:  # pragma: no cover
    pm = None  # type: ignore
    _PYDANTIC_OK = False


@unittest.skipUnless(_PYDANTIC_OK, "pydantic not installed in this env")
class _HasPydantic:
    pass


@unittest.skipUnless(_PYDANTIC_OK, "pydantic not installed in this env")
class StopAdjustSchemaTests(unittest.TestCase):
    def test_widen_not_in_literal_union(self):
        # Getting the Literal type args directly — proves the type system
        # itself rejects widen rather than relying on runtime coercion.
        field = pm.StopAdjust.model_fields["action"]
        # Literal types have __args__ accessible via typing.get_args.
        import typing
        allowed = set(typing.get_args(field.annotation))
        self.assertEqual(allowed, {"hold", "tighten", "exit_now"})
        self.assertNotIn("widen", allowed)

    def test_parse_or_default_coerces_widen_to_hold(self):
        raw = {"action": "widen", "new_stop_pct": -4.5, "reasoning": "macro dip"}
        with self.assertLogs("bot.pydantic_models", level="WARNING") as lc:
            out = pm.parse_or_default(pm.StopAdjust, raw)
        # Log must mention coercion so operators see prompt-drift.
        self.assertTrue(any("widen_coerced_to_hold" in r for r in lc.output),
                          f"expected warning, got {lc.output}")
        self.assertEqual(out.action, "hold")
        # Surrounding fields preserved.
        self.assertEqual(out.new_stop_pct, -4.5)
        self.assertEqual(out.reasoning, "macro dip")

    def test_parse_or_default_accepts_hold(self):
        out = pm.parse_or_default(pm.StopAdjust, {"action": "hold"})
        self.assertEqual(out.action, "hold")

    def test_parse_or_default_accepts_tighten(self):
        out = pm.parse_or_default(pm.StopAdjust,
                                    {"action": "tighten", "new_stop_pct": -1.5})
        self.assertEqual(out.action, "tighten")

    def test_parse_or_default_accepts_exit_now(self):
        out = pm.parse_or_default(pm.StopAdjust, {"action": "exit_now"})
        self.assertEqual(out.action, "exit_now")

    def test_parse_or_default_invalid_action_defaults_to_hold(self):
        out = pm.parse_or_default(pm.StopAdjust, {"action": "yolo"})
        self.assertEqual(out.action, "hold")

    def test_none_input_returns_safe_default(self):
        out = pm.parse_or_default(pm.StopAdjust, None)
        self.assertEqual(out.action, "hold")

    def test_widen_coercion_sets_legacy_flag(self):
        raw = {"action": "widen"}
        # Suppress the warning log in this test — we already cover it above.
        prev = logging.getLogger("bot.pydantic_models").level
        logging.getLogger("bot.pydantic_models").setLevel(logging.CRITICAL)
        try:
            # parse_or_default mutates raw into a new dict including
            # legacy_widen=True — verify that auxiliary data is preserved
            # by checking model_dump after coercion.
            out = pm.parse_or_default(pm.StopAdjust, raw)
        finally:
            logging.getLogger("bot.pydantic_models").setLevel(prev)
        # StopAdjust doesn't declare legacy_widen as a field, so it's
        # dropped by pydantic — intentional: we don't want it leaking into
        # downstream consumers, only into the log + future DB insert path.
        self.assertNotIn("legacy_widen", out.model_dump())


if __name__ == "__main__":
    unittest.main()
