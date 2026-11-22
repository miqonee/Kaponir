"""Position-based evidence for the experimental outbound masking policy."""

import csv
import io
import ipaddress
import json
import re
from typing import NamedTuple

from evaluate import strict_json


RULES = ("csv-amount", "inventory-ip", "principal-separator", "windows-account")

PUBLIC_PRICE = {"public catalogue", "public catalog", "published price list", "публичный каталог", "открытый каталог"}
PRIVATE_PRICE = {"sealed tender adjustment", "sealed tender", "private contract", "закрытая корректировка"}
PUBLIC_ACCOUNT = {"public", "published", "public example", "documentation sample"}
PRIVATE_AUDIT = {"internal windows audit", "production windows audit", "internal windows event log"}

DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
EXAMPLE_FIELDS = {"example_address", "documentation_address", "example_ip", "documentation_ip"}
ACTIVE_FIELDS = {"address", "active_address", "working_address", "production_address"}

CSV_CELL = re.compile(rb'"(?:[^"\r\n]|"")*"|[^,"\r\n]*')
PRICE = re.compile(r"[+-]?(?:[A-Z]{3}[ \u00a0\u202f])?\d(?:[\d .,\u00a0\u202f]*\d)?(?:[ \u00a0\u202f]?[A-Z]{3})?")
YAML_SCALAR = re.compile(rb"([A-Za-z][A-Za-z0-9_-]*):[ \t]+([^\s#]+)[ \t]*(?:#[^\r\n]*)?")
PRINCIPAL = re.compile(
    rb"""(?<![\w.-])principal=(?P<principal>[\w][\w.+-]*(?P<sep>%40)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)(?=$|[&;'" \t\r\n])"""
)
ACCOUNT = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_.-]*")
DOMAIN_ACCOUNT = re.compile(rb"(?P<domain>[A-Za-z0-9][A-Za-z0-9_.-]*)\\{2}(?P<user>[A-Za-z0-9][A-Za-z0-9_.-]*)")
SPACE = re.compile(r"\s*")


class Edit(NamedTuple):
    start: int
    end: int
    action: str
    rule: str


def normalized(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def csv_row(line, offset):
    """Locate cells in one CSV record, including single-line quoted amounts."""
    cells, pos = [], 0
    while True:
        match = CSV_CELL.match(line, pos)
        start, end = match.span()
        if end < len(line) and line[end] != ord(","):
            return []
        value = match.group()
        if value.startswith(b'"'):
            start, end = start + 1, end - 1
            value = value[1:-1].replace(b'""', b'"')
        cells.append((value.decode(), offset + start, offset + end))
        if match.end() == len(line):
            return cells
        pos = match.end() + 1


def csv_amounts(raw):
    lines = raw.splitlines(keepends=True)
    if not lines:
        return
    try:
        records = list(csv.reader(io.StringIO(raw.decode(), newline=""), strict=True))
    except csv.Error:
        return
    if len(lines) != len(records):
        return
    header = [normalized(value) for value in records[0]]
    roles = [i for i, key in enumerate(header) if key in {"section", "type", "visibility", "тип", "раздел"}]
    amounts = [i for i, key in enumerate(header) if key in {"amount", "price", "сумма", "цена"}]
    if len(roles) != 1 or len(amounts) != 1 or len(header) != len(set(header)):
        return
    offset = len(lines[0])
    for line, record in zip(lines[1:], records[1:]):
        cells = csv_row(line.rstrip(b"\r\n"), offset)
        offset += len(line)
        if len(cells) != len(header) or [value for value, _, _ in cells] != record:
            continue
        role = normalized(cells[roles[0]][0])
        value, start, end = cells[amounts[0]]
        if not PRICE.fullmatch(value):
            continue
        if role in PUBLIC_PRICE:
            yield Edit(start, end, "keep", "csv-amount")
        elif role in PRIVATE_PRICE:
            yield Edit(start, end, "mask", "csv-amount")


def json_fields(text):
    """Read top-level values and locate their literal content in the JSON source."""
    try:
        values = strict_json(text)
    except ValueError:
        return {}, {}
    if not isinstance(values, dict):
        return {}, {}
    decoder = json.JSONDecoder()
    cursor = SPACE.match(text).end() + 1
    strings = {}
    for _ in values:
        key, end_key = decoder.raw_decode(text, SPACE.match(text, cursor).end())
        start = SPACE.match(text, SPACE.match(text, end_key).end() + 1).end()
        value, end = decoder.raw_decode(text, start)
        if isinstance(value, str):
            strings[key] = (len(text[:start + 1].encode()), len(text[:end - 1].encode()))
        cursor = SPACE.match(text, end).end() + 1
    return values, strings


def inventory_ips(row, raw):
    fields = {}
    if row["format"] == "yaml":
        offset = 0
        for line in raw.splitlines(keepends=True):
            start_line, offset = offset, offset + len(line)
            if not line.strip() or line.lstrip().startswith(b"#"):
                continue
            match = YAML_SCALAR.fullmatch(line.rstrip(b"\r\n"))
            if not match:
                return
            key = match[1].decode().casefold()
            start, end = match.span(2)
            start, end = start_line + start, start_line + end
            if raw[start:start + 1] == raw[end - 1:end] and raw[start:start + 1] in (b"'", b'"'):
                start, end = start + 1, end - 1
            fields.setdefault(key, []).append((start, end))
    elif row["format"] == "json":
        _, strings = json_fields(row["text"])
        for key, interval in strings.items():
            fields.setdefault(key.casefold(), []).append(interval)
    examples, active = [], []
    for key, intervals in fields.items():
        if len(intervals) != 1 or key not in EXAMPLE_FIELDS | ACTIVE_FIELDS:
            continue
        start, end = intervals[0]
        try:
            address = ipaddress.ip_address(raw[start:end].decode())
        except ValueError:
            continue
        if key in EXAMPLE_FIELDS and any(address in network for network in DOCUMENTATION_NETWORKS):
            examples.append(Edit(start, end, "keep", "inventory-ip"))
        elif key in ACTIVE_FIELDS:
            active.append(Edit(start, end, "mask", "inventory-ip"))
    if examples and active:
        yield from examples
        yield from active


def windows_accounts(text, raw):
    fields, strings = json_fields(text)
    if normalized(fields.get("origin")) not in PRIVATE_AUDIT:
        return
    for key in ("actor", "account"):
        if key not in strings or normalized(fields.get(key + "_role")) in PUBLIC_ACCOUNT:
            continue
        start, end = strings[key]
        value = raw[start:end]
        principal = DOMAIN_ACCOUNT.fullmatch(value)
        if principal:
            for name in ("domain", "user"):
                left, right = principal.span(name)
                yield Edit(start + left, start + right, "mask", "windows-account")
        elif ACCOUNT.fullmatch(value):
            yield Edit(start, end, "mask", "windows-account")


def overlaps(start, end, left, right):
    return start < right and left < end


def builtin_kind(replacement, kind, recognizer):
    return (
        replacement.get("origin", {}).get("type") == "builtin"
        and replacement.get("kind") == kind
        and replacement.get("recognizer") == recognizer
    )


def release_allowed(edit, replacement, principal):
    start, end = replacement["start"], replacement["end"]
    if edit.rule == "principal-separator":
        left, right = principal.span("principal")
        # The generic secret recognizer can straddle the encoding separator.
        # Its two value fragments remain masked, including protected findings.
        return left <= start < end <= right and builtin_kind(replacement, "SECRET", "secrets")
    if replacement.get("protected") is not False or not edit.start <= start < end <= edit.end:
        return False
    kind, recognizer = {"csv-amount": ("AMOUNT", "tabular"), "inventory-ip": ("IP", "network")}[edit.rule]
    return builtin_kind(replacement, kind, recognizer)


def context_edits(row, replacements, decisions, rules, *, valid_answer):
    """Suggest edits using the visible document, immutable rules and model spans."""
    raw = row["text"].encode()
    proposed = []
    principals = {}
    if "csv-amount" in rules and row["format"] == "csv":
        proposed.extend(csv_amounts(raw))
    if "inventory-ip" in rules:
        proposed.extend(inventory_ips(row, raw))
    if "windows-account" in rules and row["format"] == "json":
        proposed.extend(windows_accounts(row["text"], raw))
    if "principal-separator" in rules and row["format"] in {"log", "text"}:
        for match in PRINCIPAL.finditer(raw):
            edit = Edit(*match.span("sep"), "keep", "principal-separator")
            principals[edit] = match
            proposed.append(edit)

    masks = [(a, b) for a, b, _, action in decisions if action != "keep"]
    masks.extend((edit.start, edit.end) for edit in proposed if edit.action == "mask")
    accepted = []
    for edit in proposed:
        if edit.action == "mask":
            accepted.append(edit)
        elif valid_answer and not any(overlaps(edit.start, edit.end, a, b) for a, b in masks):
            touched = [r for r in replacements if overlaps(edit.start, edit.end, r["start"], r["end"])]
            if touched and all(release_allowed(edit, r, principals.get(edit)) for r in touched):
                accepted.append(edit)
    return accepted
