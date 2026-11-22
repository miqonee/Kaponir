"""Source templates for disjoint, bilingual contextual privacy families."""

import csv
from dataclasses import dataclass
from io import StringIO
import json


@dataclass(frozen=True)
class Family:
    split: str
    name: str
    kind: str
    format: str
    title: str
    template: str
    roles: tuple[str, str, str]
    language: str = "en"


FAMILIES = (
    Family("train", "runbook-home", "USER", "text", "Restore instructions for notebook caches",
           "{role}. Check /home/{value}/cache/icons.",
           ("Copied from a public runbook using a fictional account", "Copied from the engineer's live laptop", "Source of this path was lost")),
    Family("train", "snapshot-home-ru", "USER", "text", "Восстановление кеша",
           "- Путь /home/{value}/watch/index. Источник: {role}.",
           ("вымышленный пример из открытого пособия", "рабочая запись дежурного сотрудника", "источник записи не подтверждён"), "ru"),
    Family("train", "identity-console", "WINDOWS", "json", "Directory console export", "actor,account,source",
           ("fictitious identity in the published administrator manual", "live employee audit event", "sample origin is unknown")),
    Family("train", "desktop-pair-ru", "WINDOWS", "json", "Карточки проверки сеанса", "account,actor,evidence",
           ("вымышленный логин из публичной памятки", "фактическая учётная запись оператора", "происхождение учётной записи неизвестно"), "ru"),
    Family("train", "broker-query", "PRINCIPAL", "text", "Broker parser notes",
           "principal={value}&result=success ({role}).",
           ("fictional principal copied from the public specification", "actual account in production audit", "cannot establish the field's source")),
    Family("train", "monitor-host", "HOST", "text", "Probe configuration, origin beside each endpoint",
           "[{role}] Agent probes {value} at port 443.",
           ("published manual with invented endpoint", "live company host from the monitoring inventory", "inventory owner could not confirm origin")),
    Family("train", "inventory-address", "IP", "text", "Inventory worksheets",
           "{role}: interface address {value}.",
           ("open protocol example, invented sample", "current interface in a restricted lab", "address seen without provenance")),
    Family("train", "supplier-catalogue", "AMOUNT", "csv", "Supplier quotation comparison", "section,amount,tax",
           ("public catalogue", "sealed tender adjustment", "provenance unknown")),
    Family("train", "finance-decimal", "AMOUNT", "csv", "Реестр начислений", "тип,сумма,валюта",
           ("опубликованный тариф", "закрытая корректировка", "происхождение суммы неизвестно"), "ru"),
    Family("train", "reconciliation-client", "CLIENT", "text", "Approval desk notes",
           "customer {value} is referenced in a {role}.",
           ("printed open sample with fictitious identifiers", "private reconciliation of an active customer account", "reconciliation with uncertain origin")),
    Family("train", "catalogue-client-literal", "CLIENT_PREFIX", "text", "Notes from the account catalogue",
           "Code {value}, entered as the full identifier; {role}.",
           ("fictional identifier from the public format guide", "identifier for a real customer on a private ticket", "publisher of the guide is unconfirmed")),
    Family("train", "pilot-release-ru", "PROJ", "text", "История публикаций",
           "Проект «{value}» указан здесь как {role}.",
           ("опубликованное название из анонса", "внутреннее имя проекта до релиза", "название с неизвестным статусом публикации"), "ru"),
    Family("train", "authorization-cards", "SECRET", "text", "API response examples and deployment cards",
           "Authorization: Bearer {value} — {role}.",
           ("explicitly fictitious token in public documentation", "active credential from a deployment card", "credential status and origin have not been verified")),
    Family("train", "publisher-index", "PUBLICATION", "text", "Publication index",
           "Package mentioned in the index: {value}.",
           ("published", "unreleased", "unknown")),
    Family("train", "build-codename", "PROJ", "text", "Build queue extracts",
           "queued project={value}; source={role}",
           ("fictional build tutorial published online", "private prerelease build ledger", "unlabelled extract, origin was lost")),
    Family("train", "customer-appeal-ru", "CLIENT_PREFIX", "text", "Материалы по обращению",
           "Идентификатор заказчика: {value}. {role}.",
           ("учебный пример целиком выдуман и опубликован", "реальный заказчик в закрытой карточке", "относимость к реальному заказчику не проверена"), "ru"),
    Family("validation", "accessibility-lesson", "USER", "text", "Notes accompanying a screen-reader course",
           "The spoken annotation describes {role}; the recording shows cd /home/{value}/intro.",
           ("a made-up login constructed for public distribution", "a live account captured on the instructor's work computer", "a capture whose account provenance was not logged")),
    Family("validation", "shift-handover", "WINDOWS", "json", "Сопоставление двух карточек смены", "actor,account,record_origin",
           ("вымышленная запись для открытого курса", "активная учётная запись из закрытого журнала", "для записи не указан источник"), "ru"),
    Family("validation", "radio-certificate", "HOST", "text", "Radio calibration certificate notes",
           "Source classification: {role}.\nThe maintenance endpoint in this attachment is {value}.",
           ("published calibration exercise with a made-up endpoint", "actual machine copied from live station records", "supplier attachment is missing its provenance")),
    Family("validation", "infrastructure-switchboard", "IP", "text", "Switchboard commissioning sheets",
           "The bridge routes packets to {value}. Technical provenance recorded as {role}.",
           ("example of a reserved documentation address in an external guide", "a live private bridge address from the deployment list", "an address without an approved source record")),
    Family("validation", "repair-schedule-pricing", "AMOUNT", "csv", "Repair catalogue and contract reconciliation", "currency,amount,section",
           ("public catalogue", "sealed tender adjustment", "price of unverified provenance")),
    Family("validation", "rights-dispatch", "CLIENT_PREFIX", "text", "Translation rights dispatch",
           "Tagged client account {value}. The dispatcher says this is {role}.",
           ("a made-up entry in the published glossary", "a named account linked to an internal rights agreement", "a value copied without any source notes")),
    Family("validation", "commission-book", "PROJ", "text", "Engineering commission minutes",
           "В протокол добавлено название {value}. Помета секретаря: {role}.",
           ("ранее опубликованное в открытом анонсе", "внутреннее кодовое имя неопубликованного проекта", "статус публикации не подтверждён"), "ru"),
    Family("validation", "museum-reader-key", "SECRET", "text", "Interactive exhibit reader service",
           "The operator checked key {value}. The accession entry describes it as {role}.",
           ("an invented credential printed for visitors in the public leaflet", "the working key stored in the exhibit's internal service note", "a credential whose validity could not be established")),
    Family("test", "volunteer-lab", "USER", "text", "Lab checkout cards",
           "{role} — путь сохранения /home/{value}/traces/run.json.",
           ("в открытой лекции использован вымышленный оператор", "путь скопирован с компьютера дежурного", "копия без подписи об источнике"), "ru"),
    Family("test", "artifact-handoff", "WINDOWS", "json", "Artifact custody notes", "account,actor,custody_source",
           ("invented pair printed in the public demonstration", "employee's real operator record pulled from secure custody", "the submitter left the origin box empty")),
    Family("test", "acoustic-sounding", "HOST", "text", "Acoustic survey station exchange",
           "Please classify this target {value} for the shared summary ({role}).",
           ("a fictitious example explicitly approved for release", "the survey team's unannounced production host", "the provenance sheet is missing")),
    Family("test", "observatory-route", "IP", "text", "Observatory route circular",
           "Receiver {value} is listed beneath the label «{role}».",
           ("invented example assigned to the public data format guide", "live receiver address restricted to night operations", "origin awaiting confirmation, do not assume publication")),
    Family("test", "binding-press-invoice", "AMOUNT", "csv", "Bookbindery settlement table", "record_class,currency,price",
           ("unrestricted rate printed on the public price sheet", "private negotiated fee in signed order", "no source for this price")),
    Family("test", "scholarship-ledger", "CLIENT", "text", "Scholarship processing labels",
           "Reference for customer: {value}.\nClerk provenance: {role}.",
           ("fabricated client in the public training packet", "real account assigned to a confidential applicant case", "an identifier that still needs provenance review")),
    Family("test", "darkroom-schedule", "PROJ", "text", "Darkroom renovation diary",
           "The diary labels this work {value}; circulation note says {role}.",
           ("the same name is formally published already", "restricted pre-announcement naming discussion", "permission to publish is still being checked")),
    Family("test", "relay-maintenance-key", "SECRET", "text", "Relay troubleshooting sheets",
           "field token={value}\nProvenance: {role}.",
           ("a made-up nonfunctional token in a public article", "the active credential required to open the private relay console", "the token's status is missing from the handoff")),
)


def mark(value, kind, action):
    return f"⟦{action}|{kind}|{value}⟧"


def values(family, number):
    label = f"{family.name}-{number}"
    kind = family.kind
    if kind == "WINDOWS":
        return (("DOM", f"V4CORP{number}"), ("USER", f"iris.{label}"))
    if kind == "PRINCIPAL":
        return (("USER", f"wen.{label}"), ("DOM", f"ad-{label}.example"))
    if kind in {"USER", "PUBLICATION"}:
        return ((("USER" if kind == "USER" else "PROJ"), f"opal-{label}"),)
    if kind == "CLIENT_PREFIX":
        return (("CLIENT", f"customer-{label}"),)
    if kind == "IP":
        # TEST-NET literals are fictional even in a synthetic document that
        # describes the address as a live interface.
        return (("IP", f"198.51.100.{100 + number}"),)
    if kind == "AMOUNT":
        amount = f"{37 + number}\u202f{number * 17:03d},42" if family.language == "ru" else f"{42000 + number * 43}.71"
        return ((kind, amount if "currency" in family.template or "валюта" in family.template else amount + " EUR"),)
    if kind == "HOST":
        return ((kind, f"node-{label}.internal.example"),)
    if kind == "SECRET":
        return ((kind, f"v4synthetic.{number:03x}.{label}.demo"),)
    return ((kind, f"reef-{label}"),)


def render(family, fields, order, reverse_fields=False):
    actions = ("keep", "mask", "review")
    if family.kind == "WINDOWS":
        first, second, evidence = family.template.split(",")
        if reverse_fields:
            first, second = second, first
        records = []
        for role in order:
            domain, user = (mark(value, kind, actions[role]) for kind, value in fields)
            values_by_key = {"actor": f"{domain}\\{user}", "account": user}
            records.append({first: values_by_key[first], evidence: family.roles[role], second: values_by_key[second]})
        return json.dumps({"sheet": family.title, "records": records}, ensure_ascii=False)
    if family.format == "csv":
        keys = family.template.split(",")
        if reverse_fields:
            keys.reverse()
        stream = StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(keys)
        amount_key = next(k for k in keys if k in {"amount", "сумма", "price"})
        role_key = next(k for k in keys if k not in {amount_key, "currency", "валюта", "tax"})
        for role in order:
            kind, value = fields[0]
            row = {"currency": "EUR", "валюта": "RUB", "tax": "standard"}
            row.update({role_key: family.roles[role], amount_key: mark(value, kind, actions[role])})
            writer.writerow([row[key] for key in keys])
        return stream.getvalue()
    parts = [family.title]
    for role in order:
        protected = "%40".join(mark(value, kind, actions[role]) for kind, value in fields)
        parts.append(family.template.format(role=family.roles[role], value=protected))
    return "\n".join(parts)


def extension_rows():
    for number, family in enumerate(FAMILIES, 1):
        group = f"v4/{family.name}"
        fields = values(family, number)
        inherited = {"group": group, "format": family.format, "language": family.language,
                     "source": "synthetic-curated", "split": family.split}
        for tag, order in (("public", (0,)), ("private", (1,)), ("uncertain", (2,)),
                           ("mixed", (0, 1)), ("reordered", (1, 0)), ("three-copies", (1, 2, 0)),
                           ("both-public", (0, 0)), ("both-private", (1, 1))):
            if family.kind == "PUBLICATION" and len(order) > 1:
                continue
            if family.kind == "WINDOWS" and len(order) > 2:
                continue
            row = {**inherited, "id": f"{group}/{tag}", "annotated": render(family, fields, order, tag == "reordered")}
            if family.kind == "PUBLICATION":
                row.update(publication_status=family.roles[order[0]])
            if tag == "private":
                row.update(contrast="provenance", contrast_with=group + "/public")
            elif tag == "uncertain":
                row.update(contrast="provenance", contrast_with=group + "/public")
            elif tag == "reordered":
                row.update(contrast="order", contrast_with=group + "/mixed")
            yield row
        clean = "Run the tool in read-only mode. Wait until the status switches to ready."
        if family.language == "ru":
            clean = "Запустить проверку без записи. Дождаться отметки о завершении."
        yield {**inherited, "id": group + "/clean", "format": "text", "annotated": family.title + ". " + clean}
