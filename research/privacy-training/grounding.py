"""Document-local recovery of protective decisions with a unique literal anchor."""

import re

from dataset import offsets
from evaluate import AnswerError, ground
from kaponir_inference import strict_json


def ground_client_label(text, record):
    try:
        return ground(text, record)
    except AnswerError as error:
        if str(error) != "ungrounded_occurrence" or not record:
            raise
    try:
        spans = strict_json(record["response"])["spans"]
    except (KeyError, TypeError, ValueError):
        return ground(text, record)
    if not isinstance(spans, list) or any(not isinstance(span, dict) for span in spans):
        return ground(text, record)

    raw = text.encode()
    corrected = []
    for span in spans:
        value = span.get("value", "")
        occurrence = span.get("occurrence")
        if (
            not isinstance(value, str) or not value
            or span.get("action") not in ("mask", "review")
            or type(occurrence) is not int or occurrence < 1
        ):
            corrected.append(span)
            continue
        try:
            positions = list(offsets(raw, value))
        except UnicodeEncodeError:
            corrected.append(span)
            continue
        if len(positions) == 1 and occurrence > 1:
            span = {**span, "occurrence": 1}
        if (
            span.get("kind") == "CLIENT"
            and occurrence == 1
            and value.startswith("customer-")
            and not positions
        ):
            surface = value.removeprefix("customer-")
            if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", surface):
                label = rb"(?i:(?<![\w.-])customer)(?:[ \t]+|[ \t]*[:=][ \t]*)"
                matches = list(re.finditer(label + rb"(" + re.escape(surface.encode()) + rb")(?![\w-]|\.[\w.-])", raw))
                if len(matches) == 1:
                    start = matches[0].start(1)
                    occurrence = list(offsets(raw, surface)).index(start) + 1
                    span = {**span, "value": surface, "occurrence": occurrence}
        corrected.append(span)
    return ground(text, {"spans": corrected})
