"""Opt-in, occurrence-level exceptions for three specified synthetic source schemas."""

from ipaddress import IPv4Address, IPv4Network
import re
import secrets


CSV_CELL = re.compile(rb'(?:"((?:[^"\r\n]|"")*)"|([^,"\r\n]*))(?=,|$)')
MONEY = re.compile(rb"[0-9]+(?:[.,][0-9]{2})? [A-Z]{3}")
PUBLIC_NETS = tuple(IPv4Network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
YAML_FIELD = re.compile(rb"([a-z_]+):[ \t]+([A-Za-z0-9_.-]+)[ \t]*")
PRINCIPAL = re.compile(
    rb"(?<![\w.-])principal=([A-Za-z0-9][A-Za-z0-9._+-]*)(%40)"
    rb"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
    rb"(?=&result=[A-Za-z0-9_-]+(?:\b|$))"
)


def lines_with_offsets(raw):
    pos = 0
    for line in raw.splitlines(keepends=True):
        yield line.removesuffix(b"\n").removesuffix(b"\r"), pos
        pos += len(line)


def csv_row(line, base):
    """Parse a single physical CSV record, retaining the raw byte bounds of each field."""
    fields = []
    pos = 0
    while True:
        m = CSV_CELL.match(line, pos)
        if not m:
            return None
        i = 1 if m[1] is not None else 2
        fields.append((m[i].replace(b'""', b'"'), base + m.start(i), base + m.end(i)))
        if m.end() == len(line):
            return fields
        pos = m.end() + 1


def public_amounts(raw):
    records = [csv_row(line, base) for line, base in lines_with_offsets(raw)]
    if not records or any(row is None or len(row) != 3 for row in records):
        return set()
    names = [field[0] for field in records[0]]
    if set(names) != {b"section", b"amount", b"tax"}:
        return set()
    result = set()
    for cells in records[1:]:
        row = dict(zip(names, cells))
        role = row[b"section"][0]
        if role not in {b"public catalogue", b"sealed tender adjustment"}:
            return set()
        value, a, b = row[b"amount"]
        if role == b"public catalogue" and MONEY.fullmatch(value):
            result.add((a, b, "AMOUNT"))
    return result


def example_ip(raw):
    """Only the registered flat inventory shape is eligible for this exception."""
    fields = {}
    for line, base in lines_with_offsets(raw):
        if not line.strip() or line.startswith(b"#"):
            continue
        m = YAML_FIELD.fullmatch(line)
        if not m or m[1] in fields:
            return set()
        fields[m[1]] = (m[2], base + m.start(2), base + m.end(2))
    if set(fields) != {b"collector", b"address", b"example_address"}:
        return set()
    try:
        IPv4Address(fields[b"address"][0].decode("ascii"))
        value, a, b = fields[b"example_address"]
        address = IPv4Address(value.decode("ascii"))
    except ValueError:
        return set()
    return {(a, b, "IP")} if any(address in net for net in PUBLIC_NETS) else set()


def identity_delimiter(raw, decisions):
    if len(re.findall(rb"(?<![\w.-])principal=", raw)) != 1:
        return None
    m = PRINCIPAL.search(raw)
    if not m:
        return None
    login, host = m.span(1), m.span(3)
    field = login[0], host[1]
    local = [s for s in decisions if s[0] < field[1] and s[1] > field[0]]
    if any(action == "review" or kind == "SECRET" for _, _, kind, action in local):
        return None
    masks = {(a, b, k) for a, b, k, action in local if action == "mask"}
    if any((*field, k) in masks for k in ("USER", "EMAIL")):
        return m.span(2)
    first_label_end = host[0] + m[3].index(b".")
    if (*login, "USER") in masks and any(
        a == host[0] and b in (first_label_end, host[1]) and k in ("DOM", "HOST")
        for a, b, k in masks
    ):
        return m.span(2)
    return None


def entropy_mask(replacement):
    signals = {e.get("source", "") for e in replacement.get("evidence", [])}
    return (replacement.get("kind") == "SECRET" and replacement.get("recognizer") == "secrets"
            and any(s.startswith("password-entropy:") for s in signals)
            and all(s == "prior:secrets" or s.startswith("password-entropy:") for s in signals))


def render(raw, trace):
    pieces, pos = [], 0
    for item in trace:
        pieces.extend([raw[pos:item["start"]], item["token"].encode()])
        pos = item["end"]
    return b"".join(pieces + [raw[pos:]]).decode()


def project(text, fmt, status, decisions, builtin, merged, enabled):
    """Return a payload, trace, restoration map and list of released source ranges."""
    masked, trace, reverse = merged
    if fmt not in enabled or status in {"internal", "unreleased", "unknown"}:
        return masked, trace, reverse, []
    raw = text.encode()
    if fmt == "csv":
        permitted = public_amounts(raw)
    elif fmt == "yaml":
        permitted = example_ip(raw)
    else:
        permitted = set()
    keeps = {(a, b, k) for a, b, k, action in decisions if action == "keep"}
    permitted &= keeps
    sep = identity_delimiter(raw, decisions) if fmt == "log" else None
    if not permitted and sep is None:
        return masked, trace, reverse, []
    hard = {(r["start"], r["end"]): r for r in builtin["replacements"]}
    new, released, lookup = [], [], dict(reverse)
    for item in trace:
        a, b = item["start"], item["end"]
        r = hard.get((a, b))
        if not r or r.get("origin") != {"type": "builtin"} or r.get("verdict") != "mask":
            new.append(item)
            continue
        if ((a, b, r["kind"]) in permitted and r.get("protected") is False
                and (r["kind"], r["recognizer"]) in {("AMOUNT", "tabular"), ("IP", "network")}):
            released.append((a, b))
            continue
        if sep and a < sep[0] and sep[1] < b and entropy_mask(r):
            for start, end in ((a, sep[0]), (sep[1], b)):
                original = raw[start:end].decode()
                token = "KPF_" + secrets.token_hex(16) + "X"
                while token in text or token in masked or token in lookup:
                    token = "KPF_" + secrets.token_hex(16) + "X"
                lookup[token] = original
                new.append({"start": start, "end": end, "token": token})
            released.append(sep)
            continue
        new.append(item)
    return render(raw, new), new, lookup, released
