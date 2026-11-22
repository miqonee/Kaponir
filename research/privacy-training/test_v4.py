"""Regression tests for the frozen v4 corpus, selection and targeted fallback."""

import re
import unittest

from dataset import PILOT_PROMPT, ROOT, build, instruction, read_jsonl, target_spans
from evaluate import validate_trace
from laptop import choose
from replay import apply_positions
from test_context_policy import baseline
from v4_correction import release_public_amount


class PrivacyV4Test(unittest.TestCase):
    def test_v3_training_prefix_is_byte_identical(self):
        old, original = build("v3")
        new, manifest = build("v4")
        self.assertTrue(new["train.jsonl"].startswith(old["train.jsonl"]))
        self.assertEqual(manifest["train_inherited"], 507)
        self.assertEqual(manifest["base_train_sha256"], original["splits"]["train"]["sha256"])
        self.assertEqual(manifest["prompt_sha256"], original["prompt_sha256"])
        self.assertEqual(instruction("v4"), instruction(PILOT_PROMPT))

    def test_repeated_values_and_family_boundaries(self):
        root = ROOT / "data/v4"
        groups = {}
        for split in ("train", "validation", "test"):
            rows = read_jsonl(root / f"{split}.jsonl")
            pairs = 0
            for row in rows:
                if not row["id"].startswith("v4/"):
                    continue
                self.assertEqual(groups.setdefault(row["group"], split), split)
                if not row["id"].endswith("/mixed"):
                    continue
                positions = {}
                for span in target_spans(row):
                    positions.setdefault(span["value"], []).append((span["occurrence"], span["action"]))
                for entries in positions.values():
                    self.assertEqual(len(entries), len({n for n, _ in entries}))
                    self.assertTrue({"keep", "mask"} <= {action for _, action in entries})
                pairs += 1
            self.assertGreaterEqual(pairs, 8)

    def test_selection_binds_the_v4_validation_hash_and_public_gate(self):
        _, manifest = build("v4")
        summary = manifest["splits"]["validation"]

        def candidate(public_touched, recall):
            return {
                "dataset": "v4", "split": "validation", "source_sha256": summary["sha256"],
                "model": {"by_action": {"mask": {"recall": {"rate": recall}}}},
                "outbound": {"all": {"complete": True, "documents": 71, "restored": 71,
                                     "public": summary["by_action"]["keep"], "public_touched": public_touched}},
            }

        data = ROOT / "data/v4"
        rejected, passed = candidate(2, 0.95), candidate(1, 0.8)
        self.assertEqual(choose([rejected, passed], data), 2)
        self.assertEqual(choose([passed, passed], data), 1)
        self.assertIsNone(choose([rejected, rejected], data))
        with self.assertRaises(ValueError):
            choose([{**passed, "source_sha256": "not-the-hash"}, passed], data)

    def test_csv_fallback_releases_only_confirmed_public_keep(self):
        public = "EUR,42034.71,public catalogue"
        private = "EUR,42034.71,sealed tender adjustment"
        for rows in ((public, private), (private, public)):
            text = "currency,amount,section\n" + "\n".join(rows) + "\n"
            hard = baseline(text, rb"42034\.71", "AMOUNT", "tabular")
            for item in hard["replacements"]:
                item["verdict"] = "mask"
            decisions = [(m.start(), m.end(), "AMOUNT", "keep") for m in re.finditer(rb"42034\.71", text.encode())]
            original = apply_positions(text, hard, decisions)
            received, trace, _, released = release_public_amount(text, "csv", None, decisions, hard, original, {"csv"})
            validate_trace(text, trace, received)
            self.assertIn(public, received)
            self.assertNotIn(private, received)
            self.assertEqual(len(released), 1)
            for status, protection in (("unknown", False), (None, True)):
                for item in hard["replacements"]:
                    item["protected"] = protection
                self.assertEqual(release_public_amount(text, "csv", status, decisions, hard, original, {"csv"})[3], [])


if __name__ == "__main__":
    unittest.main()
