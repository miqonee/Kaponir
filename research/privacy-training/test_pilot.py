"""Regression tests for pilot scoring, independent controls and the position-based adapter."""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import dataset as d
import evaluate as e
from laptop import choose
from prepare import join_answers
from replay import apply_positions


def record(name, marked):
    text, spans = d.read_marked(marked)
    return {"id": name, "group": "test/" + name, "format": "text", "text": text, "spans": spans}


def observe(row, spans):
    text = row["text"]
    baseline = {"text": text, "replacements": []}
    answer = {"spans": spans}
    received, trace, _ = apply_positions(text, baseline, e.ground(text, answer))
    return {
        "id": row["id"],
        "source_sha256": sha256(text.encode()).hexdigest(),
        "received": received,
        "replacements": trace,
        "restored": text,
    }


class PilotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.held = d.read_jsonl(d.ROOT / "data/v3/test.jsonl")

    def test_frozen_scope_and_groups(self):
        for version in d.VERSIONS:
            d.check_snapshot(d.ROOT / "data" / version)
        frozen = json.loads((d.ROOT / "reports/preflight-v3.json").read_bytes())
        self.assertEqual(
            sha256((d.ROOT / "data/v3/test.jsonl").read_bytes()).hexdigest(), frozen["test_sha256"]
        )
        self.assertEqual(
            Counter(s["action"] for r in self.held for s in r["spans"]),
            {"mask": 217, "keep": 182, "review": 18},
        )
        self.assertEqual(
            Counter(r["group"] for r in self.held if not r["spans"]), {r["group"]: 1 for r in self.held}
        )
        metadata = [r for r in self.held if "publication_status" in r]
        self.assertEqual(len(metadata), 9)
        for r in self.held:
            payload = json.loads(d.prompt(r, "v3")[1]["content"])
            self.assertEqual(set(payload), {"text", "format"} | ({"publication_status"} & r.keys()))
            self.assertNotIn("⟦", payload["text"])

    def test_semantics_of_each_independent_series(self):
        for (kind, _), members in d.contrast_sets(self.held).items():
            forms = [sorted((s["value"], s["kind"], s["action"]) for s in r["spans"]) for r in members]
            if kind == "provenance":
                self.assertEqual(len({r["text"] for r in members}), 1)
                by_status = {r["publication_status"]: r for r in members}
                self.assertEqual(set(by_status), {"unknown", "published", "internal"})
                for s in by_status["unknown"]["spans"]:
                    for status, action in (("published", "keep"), ("internal", "mask")):
                        twin = next(
                            x for x in by_status[status]["spans"] if x["start_byte"] == s["start_byte"]
                        )
                        self.assertEqual((twin["kind"], twin["value"]), (s["kind"], s["value"]))
                        self.assertEqual(twin["action"], action if s["action"] == "review" else s["action"])
            elif kind == "role":
                self.assertEqual(
                    sorted((v, a) for v, _, a in forms[0]), sorted((v, a) for v, _, a in forms[1])
                )
                self.assertEqual(len(set(forms[0]) - set(forms[1])), 1)
            else:
                self.assertEqual(forms[0], forms[1])
                if kind == "format":
                    self.assertNotEqual(members[0]["format"], members[1]["format"])
                else:
                    mixed = [
                        s["value"]
                        for s in members[0]["spans"]
                        if s["action"] == "keep" and (s["value"], s["kind"], "mask") in forms[0]
                    ]
                    self.assertTrue(mixed)
                    for value in mixed:
                        seq = [[s["action"] for s in r["spans"] if s["value"] == value] for r in members]
                        self.assertEqual(seq[0], seq[1][::-1])

    def test_oracle_and_incomplete_predictions(self):
        truth = {r["id"]: {"spans": d.target_spans(r)[::-1]} for r in self.held}
        full = e.score_model(self.held, truth)
        self.assertEqual(full["exact_documents"], e.rate(72, 72))
        series_only = {r["id"]: truth[r["id"]] for rs in d.contrast_sets(self.held).values() for r in rs}
        subset = e.score_model(self.held, series_only)
        self.assertTrue(all(s["rate"] == 1 for s in subset["contrast_sets"].values()))
        self.assertEqual(subset["errors"]["missing_answer"], len(self.held) - len(series_only))
        missing = e.score_outbound(self.held, {})["all"]
        self.assertFalse(missing["pass"])
        self.assertEqual(missing["full_privacy"], e.rate(0, 217))
        self.assertIsNone(missing["public_losses"]["rate"])

    def test_wrong_type_action_occurrence_extra_and_partial(self):
        r = record("two", "Тут ⟦keep|USER|лора⟧, там ⟦mask|USER|лора⟧.")
        pub, private = d.target_spans(r)
        for change, diagnostic in [
            ({"kind": "HOST"}, "wrong_kind"),
            ({"action": "review"}, "wrong_action"),
            ({"value": "лор", "occurrence": 2}, "inexact_boundary"),
        ]:
            out = e.score_model([r], {"two": {"spans": [pub, {**private, **change}]}})
            self.assertEqual(out["by_action"]["mask"]["fn"], 1)
            self.assertEqual(out["diagnostics"][diagnostic], 1)
        swapped = [{**pub, "action": "mask"}, {**private, "action": "keep"}]
        out = e.score_outbound([r], {"two": observe(r, swapped)})["all"]
        self.assertEqual(out["public_touched"], 1)
        self.assertEqual(out["private_open"], 1)
        extra = {"kind": "CUSTOM", "value": "Тут", "action": "mask", "occurrence": 1}
        score = e.score_model([r], {"two": {"spans": [pub, private, extra]}})
        self.assertEqual(score["all_spans"]["fp"], 1)
        partial = e.score_outbound([r], {"two": observe(r, [pub, {**private, "value": "лор"}])})["all"]
        self.assertEqual(partial["private_partial"], 1)
        self.assertEqual(partial["documents_with_leak"], 1)

    def test_failure_is_counted_on_empty_and_nonempty_documents(self):
        r = record("x", "⟦mask|USER|dove⟧")
        empty = record("e", "Протрите стекло.")
        good = d.target_spans(r)[0]
        for ans, reason in [
            ({"response": "{"}, "invalid_json"),
            ({"response": '{"spans":[],"spans":[]}'}, "invalid_json"),
            ({"spans": [{**good, "occurrence": True}]}, "invalid_schema"),
            ({"spans": [{**good, "occurrence": 2}]}, "ungrounded_occurrence"),
            ({"spans": [good, good]}, "duplicate_or_overlap"),
            ({"error": "truncated_answer"}, "truncated_answer"),
        ]:
            score = e.score_model([r, empty], {"x": ans})
            self.assertEqual(score["errors"][reason], 1)
            self.assertEqual(score["errors"]["missing_answer"], 1)
            self.assertEqual(score["exact_documents"], e.rate(0, 2))
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "records"
            for payload in ('{"id":"x"}\n{"id":"x"}\n', '{"id":"another"}\n', "not-json\n"):
                f.write_text(payload, encoding="utf-8")
                with self.assertRaises(ValueError):
                    e.index_records(f, [r])

    def test_replay_keeps_hard_masks_and_masks_review(self):
        r = record("ranges", "До ⟦keep|CUSTOM|ABC⟧. ⟦review|CUSTOM|ABCDE⟧ после.")
        raw = r["text"].encode()
        a = raw.rindex(b"BC")
        built = {"start": a, "end": a + 2, "token": "ANON_HARD_1"}
        base = {"text": (raw[:a] + b"ANON_HARD_1" + raw[a + 2 :]).decode(), "replacements": [built]}
        received, trace, reverse = apply_positions(
            r["text"], base, e.ground(r["text"], {"spans": d.target_spans(r)})
        )
        self.assertIn(built, trace)
        self.assertTrue(received.startswith("До ABC. "))
        self.assertEqual(len(reverse), 2)
        trace_data = {
            "source_sha256": sha256(raw).hexdigest(),
            "received": received,
            "restored": r["text"],
            "replacements": trace,
        }
        counts = e.outbound_counts(r, trace_data)
        self.assertEqual(
            (counts["review_full"], counts["collateral_bytes"], counts["public_touched"]), (1, 0, 0)
        )
        with self.assertRaises(ValueError):
            e.outbound_counts(r, {**trace_data, "received": r["text"]})
        with self.assertRaises(ValueError):
            e.outbound_counts(r, {**trace_data, "source_sha256": "wrong"})
        self.assertEqual(e.outbound_counts(r, {**trace_data, "restored": "corrupt"})["restored"], 0)
        with self.assertRaises(ValueError):
            e.validate_trace("тайна", [{"start": 1, "end": 4, "token": "A"}], "A")

    def test_document_gate_and_paired_comparison(self):
        rows = [record("t", "⟦keep|USER|demo.s⟧ and ⟦mask|USER|real.s⟧"), record("n", "Turn the dial.")]
        records = {r["id"]: observe(r, d.target_spans(r)) for r in rows}
        good = e.score_outbound(rows, records)
        self.assertTrue(good["all"]["pass"])
        self.assertEqual(e.paired_difference(good, good, 100)["private_full"]["ci95_pp"], [0.0, 0.0])
        bad = e.score_outbound(rows, {**records, "n": {**records["n"], "restored": ""}})
        self.assertFalse(bad["all"]["pass"])
        r = rows[1]
        edit = observe(r, [{"value": "dial", "kind": "CUSTOM", "action": "mask", "occurrence": 1}])
        self.assertEqual(e.outbound_counts(r, edit)["changed_clean"], 1)

    def test_registered_thresholds_use_integer_counts(self):
        c = Counter(
            observed=72,
            documents=72,
            restored=72,
            private=217,
            public=182,
            private_full=207,
            public_touched=3,
        )
        self.assertTrue(e.outbound_summary(c)["pass"])
        c["private_full"] -= 1
        self.assertFalse(e.outbound_summary(c)["pass"])
        c["private_full"] += 1
        c["public_touched"] += 1
        self.assertFalse(e.outbound_summary(c)["pass"])

    def test_reassemble_global_occurrences(self):
        chunks = ["ёл ёл ", "ёлка ёл"]
        original = "".join(chunks)
        windows = []
        cursor = 0
        for i, t in enumerate(chunks):
            b = len(t.encode())
            windows.append(
                {
                    "id": str(i),
                    "document_id": "doc",
                    "start_byte": cursor,
                    "end_byte": cursor + b,
                    "source_sha256": sha256(original.encode()).hexdigest(),
                    "prompt": d.prompt({"text": t, "format": "text"}, "v3"),
                }
            )
            cursor += b
        s = {"value": "ёл", "kind": "USER", "action": "mask", "occurrence": 2}
        ans = {"0": {"spans": []}, "1": {"spans": [s]}}
        self.assertEqual(join_answers(windows, ans)[0]["spans"], [{**s, "occurrence": 4}])
        self.assertEqual(join_answers(windows, {"0": ans["0"]})[0]["error"], "missing_answer")
        with self.assertRaises(ValueError):
            join_answers([{**windows[0], "start_byte": 1}, windows[1]], ans)

    def test_validation_selection(self):
        digest = d.check_snapshot(d.ROOT / "data/v3")["splits"]["validation"]["sha256"]

        def report(loss, recall, split="validation"):
            return {
                "dataset": "v3",
                "split": split,
                "source_sha256": digest,
                "outbound": {
                    "all": {
                        "complete": True,
                        "documents": 10,
                        "restored": 10,
                        "public": 100,
                        "public_touched": loss,
                    }
                },
                "model": {"by_action": {"mask": {"recall": {"rate": recall}}}},
            }

        self.assertEqual(choose([report(2, 0.7), report(1, 0.8)]), 2)
        self.assertEqual(choose([report(2, 0.7), report(3, 0.99)]), 1)
        self.assertEqual(choose([report(0, 0.7), report(1, 0.7)]), 1)
        self.assertIsNone(choose([report(3, 0.9), report(4, 1.0)]))
        with self.assertRaises(ValueError):
            choose([report(2, 0.7), report(1, 0.8, "test")])


if __name__ == "__main__":
    unittest.main()
