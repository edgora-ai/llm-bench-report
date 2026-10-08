import json
import tempfile
import unittest
from pathlib import Path

from bench import effort
from bench.adapters import claude_code, opencode


CATALOGUE = {"opencode": {"models": {
    "gpt-6.1-sol": {"reasoning": True, "reasoning_options": [
        {"type": "effort", "values": ["low", "medium", "high", "xhigh", "max"]}]},
    "gpt-5.1-codex": {"reasoning": True, "reasoning_options": [
        {"type": "effort", "values": ["low", "medium", "high"]}]},
    "glm-5.3-flash": {"reasoning": True, "reasoning_options": [
        {"type": "effort", "values": ["low", "high", "max"]}]},
    "muse-spark-1.3": {"reasoning": True, "reasoning_options": [
        {"type": "effort", "values": ["minimal", "low", "medium", "high", "xhigh"]}]},
    "kimi-k3": {"reasoning": True, "reasoning_options": [
        {"type": "effort", "values": ["max"]}]},
    "kimi-k2.5-free": {"reasoning": True, "reasoning_options": []},
    "plain-model": {"reasoning": False},
}}}


class EffortTests(unittest.TestCase):
    def resolve(self, model, allowed=None):
        return effort.resolve(CATALOGUE, model, allowed=allowed or list(effort.CEILING_ORDER))

    def test_ceiling_follows_the_ladder_not_the_declaration(self):
        # "low, medium, high" must not be read as ceiling "high" by position of
        # the last item, nor as "xhigh" because a bigger word exists elsewhere.
        self.assertEqual(effort.ceiling(["low", "medium", "high"]), "high")
        self.assertEqual(effort.ceiling(["low", "high", "max"]), "max")
        self.assertEqual(effort.ceiling(["minimal", "low", "medium", "high", "xhigh"]), "xhigh")
        self.assertEqual(effort.ceiling(["medium", "high", "xhigh"]), "xhigh")
        self.assertEqual(effort.ceiling(["max"]), "max")
        self.assertIsNone(effort.ceiling([]))
        self.assertIsNone(effort.ceiling(["turbo"]))

    def test_maximum_is_requested_and_each_model_gets_its_own_ceiling(self):
        for model, applied in [("gpt-6.1-sol", "max"), ("gpt-5.1-codex", "high"),
                               ("glm-5.3-flash", "max"), ("kimi-k3", "max")]:
            record = self.resolve(model)
            self.assertEqual(record["requested"], "max", model)
            self.assertEqual(record["applied"], applied, model)
            self.assertEqual(record["control"], "declared", model)
        # A model whose ladder stops at xhigh is downgraded, and says so.
        spark = self.resolve("muse-spark-1.3")
        self.assertEqual(spark["applied"], "xhigh")
        self.assertEqual(spark["downgraded_from"], "max")
        self.assertEqual(spark["requested"], "max")

    def test_no_ladder_is_recorded_not_assumed(self):
        # These two are the real failure modes: claiming an effort that was
        # never set, and passing a level the route would reject.
        fixed = self.resolve("kimi-k2.5-free")
        self.assertIsNone(fixed["applied"])
        self.assertEqual(fixed["control"], "fixed_unspecified_effort")
        absent = self.resolve("plain-model")
        self.assertIsNone(absent["applied"])
        self.assertEqual(absent["control"], "no_effort_vocabulary")

    def test_unknown_model_is_not_given_an_invented_level(self):
        record = self.resolve("deepseek-flash")
        self.assertIsNone(record["applied"])
        self.assertEqual(record["control"], "not_in_catalogue")

    def test_bare_and_provider_qualified_ids_resolve_identically(self):
        self.assertEqual(self.resolve("gpt-6.1-sol")["applied"],
                         self.resolve("opencode/gpt-6.1-sol")["applied"])

    def test_effort_only_takes_the_effort_typed_option(self):
        entry = {"reasoning_options": [
            {"type": "summary", "values": ["detailed", "auto"]},
            {"type": "effort", "values": ["low", "high"]}]}
        self.assertEqual(effort.effort_vocabulary(entry), ["low", "high"])
        self.assertEqual(effort.effort_vocabulary({"reasoning_options": "bad"}), [])

    def test_condition_fields_never_claim_applied_when_absent(self):
        fields = effort.condition_fields(self.resolve("kimi-k2.5-free"))
        self.assertEqual(fields["reasoning_effort_requested"], "max")
        self.assertIsNone(fields["reasoning_effort_applied"])
        self.assertEqual(fields["reasoning_effort_control"], "fixed_unspecified_effort")

    def test_both_clients_carry_the_level_into_the_command(self):
        claude = claude_code.command("gpt-6.1-sol", "max")
        self.assertEqual(claude[claude.index("--effort") + 1], "max")
        open_code = opencode.command("opencode/space-bunny-free", "max")
        self.assertEqual(open_code[open_code.index("--variant") + 1], "max")
        # Uncontrolled runs send no flag at all rather than an empty value.
        self.assertNotIn("--effort", claude_code.command("gpt-6.1-sol"))
        self.assertNotIn("--variant", opencode.command("opencode/big-pickle"))

    def test_unknown_effort_level_is_refused_before_the_session(self):
        with self.assertRaises(ValueError):
            claude_code.command("gpt-6.1-sol", "turbo")

    def test_disabled_effort_is_recorded_rather_than_defaulted(self):
        record = effort.resolve(CATALOGUE, "gpt-6.1-sol", allowed=[])
        self.assertIsNone(record["applied"])
        self.assertIsNone(record["requested"])

    def test_real_catalogue_resolves_every_queued_route(self):
        # The live snapshot is the actual input, so parsing it is part of the
        # contract rather than a fixture detail.
        path = Path(__file__).resolve().parent.parent / "data/discovery/latest-models.json"
        if not path.is_file():
            self.skipTest("no local model-directory snapshot in the public source export")
        catalogue = json.loads(path.read_text())
        for model in ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
                      "deepseek-flash", "glm-5.3-flash", "kimi-k3-256k"]:
            record = effort.resolve(catalogue, model, allowed=list(effort.CEILING_ORDER))
            self.assertEqual(record["requested"], "max", model)
            self.assertIn(record["control"], {"declared", "no_effort_vocabulary",
                                             "fixed_unspecified_effort", "not_in_catalogue"}, model)
        for model in ["opencode/big-pickle", "opencode/muse-spark-1.3-contributor-free",
                      "opencode/space-bunny-free", "opencode/ling-3.0-flash-fin-free"]:
            record = effort.resolve(catalogue, model, allowed=list(effort.CEILING_ORDER))
            self.assertEqual(record["requested"], "max", model)
        # The two routes that do declare a ladder must reach their real ceiling.
        self.assertEqual(effort.resolve(catalogue, "glm-5.3-flash",
                                        allowed=list(effort.CEILING_ORDER))["applied"], "max")
        self.assertEqual(effort.resolve(catalogue, "opencode/space-bunny-free",
                                        allowed=list(effort.CEILING_ORDER))["applied"], "max")
        self.assertEqual(effort.resolve(catalogue, "opencode/muse-spark-1.3-contributor-free",
                                        allowed=list(effort.CEILING_ORDER))["applied"], "xhigh")

    def test_unreadable_catalogue_does_not_fake_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(effort.read_catalogue(path), {})
            record = effort.resolve(effort.read_catalogue(path), "gpt-6.1-sol",
                                    allowed=list(effort.CEILING_ORDER))
            self.assertIsNone(record["applied"])


if __name__ == "__main__":
    unittest.main()
