"""Regressions for contextual masking and the position-based acceptance check."""

from hashlib import sha256
import json
import re
import unittest

from context_policy import context_edits
from context_controls import decode_ids, occurrence_id_window
from dataset import read_marked
from evaluate import AnswerError, positional_comparison, validate_trace
from grounding import ground_client_label
from replay import apply_positions


def baseline(text, matches, kind, recognizer, protected=False, origin="builtin"):
    raw = text.encode()
    replacements, parts, cursor = [], [], 0
    for index, match in enumerate(re.finditer(matches, raw)):
        start, end = match.span()
        token = f"ANON_TEST_{index}_Z"
        replacements.append({
            "start": start, "end": end, "token": token, "kind": kind,
            "recognizer": recognizer, "origin": {"type": origin}, "protected": protected,
        })
        parts.extend((raw[cursor:start], token.encode()))
        cursor = end
    return {"text": b"".join(parts + [raw[cursor:]]).decode(), "replacements": replacements}


def apply(text, fmt, hard, rule, decisions=(), valid=True):
    row = {"text": text, "format": fmt}
    edits = context_edits(row, hard["replacements"], decisions, (rule,), valid_answer=valid)
    masked, trace, reverse = apply_positions(text, hard, decisions, edits)
    validate_trace(text, trace, masked)
    for replacement in reversed(hard["replacements"]):
        a, b = replacement["start"], replacement["end"]
        reverse[replacement["token"]] = text.encode()[a:b].decode()
    if reverse:
        restored = re.sub("|".join(map(re.escape, reverse)), lambda m: reverse[m[0]], masked)
        assert restored == text
    return masked, edits


class ContextPolicyTest(unittest.TestCase):
    def test_csv_order_private_keep_and_format_error(self):
        price = "78\u202f409,92"
        public = f'public catalogue,"{price}",RUB\n'
        private = f'закрытая корректировка,"{price}",RUB\n'
        for lines in [(public, private), (private, public)]:
            text = "section,amount,currency\r\n" + "\r\n".join(line.rstrip() for line in lines) + "\r\n"
            trace = baseline(text, re.escape(price.encode()), "AMOUNT", "tabular")
            model_keeps = [(r["start"], r["end"], "AMOUNT", "keep") for r in trace["replacements"]]
            masked, edits = apply(text, "csv", trace, "csv-amount", model_keeps)
            self.assertIn(public.strip(), masked)
            self.assertNotIn(private.strip(), masked)
            self.assertEqual([edit.action for edit in edits].count("keep"), 1)

        text = "тип,сумма,валюта\n" + private
        masked, edits = apply(text, "csv", baseline(text, rb"(?!)", "", ""), "csv-amount", valid=False)
        self.assertNotIn(price, masked)
        self.assertIn('",RUB', masked)
        self.assertEqual([(edit.action, edit.rule) for edit in edits], [("mask", "csv-amount")])

    def test_policy_leaves_conflicting_and_protected_values_masked(self):
        text = "section,amount\npublic catalogue,80.10 EUR\nsealed tender adjustment,80.10 EUR\n"
        raw = text.encode()
        start = raw.index(b"80.10")
        trace = baseline(text, rb"80\.10 EUR", "AMOUNT", "tabular")
        for action in ("mask", "review"):
            masked, _ = apply(text, "csv", trace, "csv-amount", [(start, start + 9, "AMOUNT", action)])
            self.assertNotIn("80.10 EUR", masked)
        for options in ({"protected": True}, {"origin": "user"}):
            trace = baseline(text, rb"80\.10 EUR", "AMOUNT", "tabular", **options)
            masked, _ = apply(text, "csv", trace, "csv-amount")
            self.assertNotIn("80.10 EUR", masked)
        text = 'section,amount\n"unclosed\npublic catalogue,80.10 EUR\n'
        trace = baseline(text, rb"80\.10 EUR", "AMOUNT", "tabular")
        masked, _ = apply(text, "csv", trace, "csv-amount")
        self.assertNotIn("80.10 EUR", masked)

    def test_inventory_role_and_order(self):
        for fields in (
            ("example_address: 198.51.100.6", "address: 192.0.2.6"),
            ("address: 198.51.100.6", "example_address: 192.0.2.6"),
        ):
            text = "\n".join(fields) + "\n"
            trace = baseline(text, rb"(?:198\.51\.100\.6|192\.0\.2\.6)", "IP", "network")
            masked, _ = apply(text, "yaml", trace, "inventory-ip")
            self.assertIn(next(line for line in fields if line.startswith("example_")), masked)
            self.assertNotIn("\n" + next(line for line in fields if line.startswith("address:")), "\n" + masked)

    def test_split_protected_principal_keeps_separator_and_private_parts(self):
        text = "Журнал: principal=case.user%40branch.example&result=ok"
        trace = baseline(text, rb"user%40branch", "SECRET", "secrets", protected=True)
        raw = text.encode()
        start = raw.index(b"case.user")
        decisions = [(start, start + len(b"case.user"), "USER", "mask")]
        masked, edits = apply(text, "log", trace, "principal-separator", decisions)
        self.assertEqual(raw[edits[0].start:edits[0].end], b"%40")
        self.assertIn("%40", masked)
        self.assertNotIn("case.", masked)
        self.assertNotIn("user", masked)
        self.assertNotIn("branch", masked)
        custom = baseline(text, rb"user%40branch", "SECRET", "secrets", protected=True, origin="user")
        self.assertNotIn("%40", apply(text, "log", custom, "principal-separator", decisions)[0])

    def test_windows_audit_separates_public_copy_and_utf8_positions(self):
        values = {"note": "журнал", "origin": "internal Windows audit", "actor": "OPAL\\case.user", "account": "case.user"}
        for public in (False, True):
            for reversed_order in (False, True):
                if public:
                    values["account_role"] = "public example"
                fields = dict(reversed(list(values.items()))) if reversed_order else values
                text = json.dumps(fields, ensure_ascii=False)
                masked, _ = apply(text, "json", baseline(text, rb"(?!)", "", ""), "windows-account")
                actual = json.loads(masked)
                self.assertNotIn("case.user", actual["actor"])
                self.assertNotIn("OPAL", actual["actor"])
                self.assertEqual(actual["account"] == "case.user", public)

    def test_client_label_preserves_literal_and_rejects_multiple_anchors(self):
        def answer(value, occurrence, kind="CLIENT"):
            return {"response": json.dumps({"spans": [
                {"value": value, "occurrence": occurrence, "kind": kind, "action": "mask"},
            ]})}

        text = "Письмо: customer intake-rose."
        raw = text.encode()
        self.assertEqual(ground_client_label(text, answer("customer-intake-rose", 1))[0][:2],
                         (raw.index(b"intake-rose"), raw.index(b"intake-rose") + len(b"intake-rose")))
        text = "customer intake-rose, literal client customer-intake-rose"
        self.assertEqual(ground_client_label(text, answer("customer-intake-rose", 1))[0][0],
                         text.index("customer-intake-rose"))
        with self.assertRaises(AnswerError):
            ground_client_label("customer intake-rose, customer intake-rose", answer("customer-intake-rose", 1))
        with self.assertRaisesRegex(AnswerError, "ungrounded_occurrence"):
            ground_client_label("customer intake-rose", answer("\ud800", 1))
        self.assertEqual(ground_client_label("active host relay-bird", answer("relay-bird", 2, "HOST"))[0][0], 12)

    def test_same_counts_do_not_cancel_new_disclosure(self):
        text, spans = read_marked("⟦mask|USER|actor1⟧ and ⟦mask|USER|actor2⟧")
        row = {"id": "repeated", "text": text, "spans": spans}
        first = baseline(text, rb"actor1", "USER", "account")
        second = baseline(text, rb"actor2", "USER", "account")
        for observation in (first, second):
            observation["received"] = observation["text"]
            observation["source_sha256"] = sha256(text.encode()).hexdigest()
        report = positional_comparison([row], {row["id"]: first}, {row["id"]: second})
        self.assertEqual(len(report["new_private_disclosures"]), 1)
        self.assertEqual(report["new_private_disclosures"][0]["start_byte"], 0)

    def test_explicit_ids_choose_two_equal_values_independently(self):
        class Budget:
            def ids(self, messages, generating):
                return [1]

        text = "section,amount\nsealed tender adjustment,62.70 EUR\npublic catalogue,62.70 EUR\n"
        window = occurrence_id_window({"id": "same", "format": "csv", "text": text}, Budget())
        answer = {"response": '{"spans":[{"id":"p2","action":"keep"},{"id":"p1","action":"mask"}]}'}
        joined = decode_ids(window, answer)
        self.assertEqual([(s["occurrence"], s["action"]) for s in joined["spans"]], [(2, "keep"), (1, "mask")])
        duplicate = {"response": '{"spans":[{"id":"p1","action":"keep"},{"id":"p1","action":"mask"}]}'}
        self.assertEqual(decode_ids(window, duplicate)["error"], "duplicate_or_overlap")


if __name__ == "__main__":
    unittest.main()
