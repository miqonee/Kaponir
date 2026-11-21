"""Synthetic document templates with explicit, non-overlapping value labels."""

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Value:
    text: str
    kind: str
    action: str = "mask"


def linux_home(code):
    user = Value(f"lark.{code[:6]}", "USER")
    return (
        "Фрагмент рабочего shell-журнала. Пользователь ", user,
        " обновил /home/", user, "/", Value(f"slate-{code}", "PATH"), "/k8s/values.yaml.\n",
        "Публичное имя команды ", Value("kubectl", "CUSTOM", "keep"), " сохранено.",
    )


def directory_login(code):
    return (
        "Рабочая учётная запись AD для синхронизации: ", Value(f"NIVEL{code[:5].upper()}", "DOM"),
        "\\", Value(f"svc.relay-{code}", "USER"), ". Стандарт аутентификации: ",
        Value("Kerberos", "CUSTOM", "keep"), ".",
    )


def windows_profile(code):
    return (
        '{"source":"internal workstation","home":"C:\\\\Users\\\\',
        Value(f"mira.{code[:6]}", "USER"), '\\\\Documents\\\\',
        Value(f"intake-{code}", "PATH"), '\\\\notes.txt","event_id":',
        Value("4624", "CUSTOM", "keep"), "}",
    )


def ssh_uri(code):
    return (
        "Internal maintenance endpoint: ssh://", Value(f"audit.{code[:6]}", "USER"), "@",
        Value(f"jump-{code[:5]}", "SUB"), ".", Value(f"harvest-{code[5:]}", "DOM"),
        ".example:", Value("22", "CUSTOM", "keep"), "/var/log.",
    )


def database_env(code):
    return (
        "# Running database worker\nDB_USER=", Value(f"batch_{code}", "USER"),
        "\nDB_PASSWORD=", Value(f"moss.{code}.sable!", "SECRET"),
        "\nDB_HOST=", Value(f"ledger-{code}", "HOST"), "\nDB_PORT=",
        Value("5432", "CUSTOM", "keep"), "\n",
    )


def two_prices(code):
    price = f"{int(code[:4], 16) + 20000:,}".replace(",", " ") + " ₽"
    return (
        "В опубликованном каталоге цена ", Value(price, "AMOUNT", "keep"),
        ". В отдельном закрытом договоре согласована сумма ", Value(price, "AMOUNT"),
        ". Применяется стандартная ставка НДС ", Value("20%", "AMOUNT", "keep"), ".",
    )


def private_contact(code):
    return (
        "Личный контакт дежурной смены из внутреннего списка: ",
        Value(f"night.{code}@relay.example", "EMAIL"),
        ". Адрес из общедоступной страницы помощи: ",
        Value("support@example.org", "EMAIL", "keep"), ".",
    )


def internal_event(code):
    episode = Value(f"TR-{code.upper()}", "CUSTOM")
    return (
        "level=INFO visibility=internal correlation=", episode,
        " origin=", Value(f"reconcile-{code}", "SVC"), " protocol=",
        Value("HTTP/2", "CUSTOM", "keep"), "\nstatus=done correlation=", episode,
    )


def kubernetes_config(code):
    return (
        "# Active internal deployment\napiVersion: ", Value("apps/v1", "CUSTOM", "keep"),
        "\nmetadata:\n  namespace: ", Value(f"orchid-{code[:5]}", "CUSTOM"),
        "\n  name: ", Value(f"consolidator-{code[5:]}", "SVC"), "\n",
    )


def signed_link(code):
    return (
        "Рабочая ссылка на закрытую выгрузку https://", Value(f"reports-{code[:5]}", "SUB"),
        ".", Value(f"tamarind-{code[5:]}", "DOM"), ".example",
        Value("/api/v1", "PATH", "keep"), "/reports/", Value(f"batch-{code}", "CUSTOM"),
        "?access_token=", Value(f"ember_{code}_v9", "SECRET"), ".",
    )


def acquisition_note(code):
    return (
        "Внутренний проект ", Value(f"Ночной Архив {code[:4].upper()}", "PROJ"),
        ". Закрытый идентификатор сделки ", Value(f"DEAL-{code.upper()}", "CUSTOM"),
        ". Указанная в справочной части валюта ", Value("EUR", "CUSTOM", "keep"), ".",
    )


def public_manual(code):
    return (
        "Public syntax reference. ", Value(f"sample-{code}.example", "DOM", "keep"),
        " and ", Value(f"moss.{code}.sable!", "SECRET", "keep"),
        " are fictional placeholders without a backing host or account. Use ",
        Value("localhost", "HOST", "keep"), " in the local exercise.",
    )


def detached_reference(code):
    return (
        "В копии заметки встречается только ", Value(f"DOC-{code.upper()}", "CUSTOM", "review"),
        ". Источник кода утерян. Статус публикации неизвестен.",
    )


def duty_roster(code):
    surname = ("Лавиер", "Неволь", "Орвель", "Рамис", "Веллан", "Котар")[int(code[0], 16) % 6]
    phone = f"+7 (000) 812-{int(code[:2], 16) % 100:02d}-{int(code[2:4], 16) % 100:02d}"
    return (
        "Внутренний список дежурных. Контакт сотрудницы ", Value(f"Марна {surname}", "PERSON"),
        ", телефон ", Value(phone, "PHONE"), ". Открытая линия поддержки ",
        Value("support@example.org", "EMAIL", "keep"), ".",
    )


def account_agreement(code):
    return (
        "Unpublished partner intake. The organization ", Value(f"Hedgerow Works {code[:5]}", "ORG"),
        " is associated with account ", Value(f"cust-reed-{code[5:]}", "CLIENT"),
        ". The general contract format is ", Value("PDF", "CUSTOM", "keep"), ".",
    )


def mac_ticket(code):
    return (
        "Закрытая заявка на рабочий Mac. Выгрузка находится в /Users/",
        Value(f"élia.{code[:6]}", "USER"), "/Library/", Value(f"folio-{code}", "PATH"),
        "/status.json. Публичный формат обмена ", Value("JSON", "CUSTOM", "keep"), ".",
    )


def windows_audit(code):
    user = Value(f"case.{code[:6]}", "USER")
    domain = Value(f"BIRCH{code[6:].upper()}", "DOM")
    return (
        '{"origin":"internal Windows audit","actor":"', domain, "\\\\", user,
        '","account":"', user, '","result":', Value("4624", "CUSTOM", "keep"), "}",
    )


def invoice_csv(code):
    price = f"{int(code[:4], 16) + 19000}.70 EUR"
    return (
        "section,amount,tax\npublic catalogue,", Value(price, "AMOUNT", "keep"),
        ",", Value("20%", "AMOUNT", "keep"), "\nsealed tender adjustment,", Value(price, "AMOUNT"),
        ",", Value("20%", "AMOUNT", "keep"), "\n",
    )


def internal_signature(code):
    return (
        "В цитате из внутренней переписки указан личный адрес круглосуточной связи ",
        Value(f"ravel.{code}@dispatch.example", "EMAIL"), ". Номер закрытого обращения ",
        Value(f"CASE-{code.upper()}", "CUSTOM"), ". Общедоступный способ связи — ",
        Value("HTTPS", "CUSTOM", "keep"), ".",
    )


def encoded_principal(code):
    return (
        "Identity broker audit for production. principal=", Value(f"j.lira-{code[:5]}", "USER"),
        Value("%40", "CUSTOM", "keep"), Value(f"linden-{code[5:]}", "DOM"),
        ".example&result=success.",
    )


def ops_inventory(code):
    ip = f"10.171.{int(code[:2], 16)}.{int(code[2:4], 16) % 253 + 1}"
    return (
        "# Private network inventory\ncollector: ", Value(f"frond-{code}", "HOST"),
        "\naddress: ", Value(ip, "IP"), "\nexample_address: ",
        Value("192.0.2.45", "IP", "keep"), "\n",
    )


def public_training_notice(code):
    return (
        "Из публичного задания. Значение ", Value(f"fixture_{code}_p6", "SECRET", "keep"),
        " иллюстрирует токен и не даёт доступа. Пример пути ",
        Value("/etc/hosts", "PATH", "keep"), " — стандартный системный файл.",
    )


def unresolved_ticket(code):
    return (
        "Export copied without its source sheet. Item ", Value(f"QX-{code.upper()}", "CUSTOM", "review"),
        " may refer to a published sample or a private case. Ask the owner.",
    )


def escalation_card(code):
    surname = ("Мирваль", "Кершель", "Аверон", "Тарис")[int(code[0], 16) % 4]
    phone = f"+7 (000) 973-{int(code[:2], 16) % 100:02d}-{int(code[2:4], 16) % 100:02d}"
    return (
        "Закрытая карточка эскалации подрядчика ", Value(f"Орвальный Контур {code[:5]}", "ORG"),
        ". Ответственный ", Value(f"Рейн {surname}", "PERSON"), ", прямой номер ",
        Value(phone, "PHONE"), ". Для публичной части используйте обозначение ",
        Value("HTTP", "CUSTOM", "keep"), ".",
    )


def temporary_access_notice(code):
    return (
        "Remove the working credential from this draft support notice before sending it. "
        "The value in the Authorization header is ", Value(f"current.{code}.ox7", "SECRET"),
        ". This access is for customer ", Value(f"intake-pearl-{code[:6]}", "CLIENT"),
        ". The header name ", Value("Authorization", "CUSTOM", "keep"), " is documented publicly.",
    )


def sssd_trace(code):
    account = Value(f"g.norel-{code[:5]}", "USER")
    return (
        "Внутренняя диагностика SSSD\nauthid=", Value(f"REDWOOD{code[5:].upper()}", "DOM"),
        "\\", account, " canonical=", account, "@", Value(f"redwood-{code[5:]}", "DOM"),
        ".example. Метод ", Value("GSSAPI", "CUSTOM", "keep"), ".",
    )


def systemd_unit(code):
    account = Value(f"runner-{code}", "USER")
    return (
        "# Private active unit\n[Service]\nUser=", account, "\nWorkingDirectory=/home/",
        account, "/", Value(f"monorail-{code[:6]}", "PATH"), "\nType=",
        Value("oneshot", "CUSTOM", "keep"), "\n",
    )


def smb_failure(code):
    return (
        "Сбой рабочей выгрузки на ", "\\\\", Value(f"hemlock-fs-{code[:5]}", "HOST"),
        "\\", Value(f"allocation-{code[5:]}", "PATH"), "\\Reports\\status.csv. Код протокола ",
        Value("SMB3", "CUSTOM", "keep"), ".",
    )


def renewal_sheet(code):
    price = f"USD {int(code[:5], 16) + 1350:,}.35"
    return (
        "# Approved public and private sections\npublic_tax_percent: ", Value("8%", "AMOUNT", "keep"),
        "\nrestricted_renewal_price: \"", Value(price, "AMOUNT"), "\"\nrestricted_deal: ",
        Value(f"RENEW-{code.upper()}", "CUSTOM"), "\n",
    )


def support_export(code):
    return (
        "Из открытой статьи можно цитировать ", Value("TLS 1.3", "CUSTOM", "keep"),
        ". В закрытой истории обращения указан адрес клиента ",
        Value(f"client.{code}@correspondence.example", "EMAIL"), " и номер ",
        Value(f"ATT-{code.upper()}", "CUSTOM"), ".",
    )


def tool_result(code):
    ip = f"fd3a:{code[:4]}:{code[4:8]}::1"
    return (
        '{"source":"active private integration","headers":{"Authorization":"Bearer ',
        Value(f"fixture_{code}_p6", "SECRET"), '"},"destination":"[', Value(ip, "IP"),
        ']:', Value("443", "CUSTOM", "keep"), '"}',
    )


def registry_report(code):
    return (
        "Image deployed from private registry ", Value(f"image-{code[:5]}", "SUB"), ".",
        Value(f"bayleaf-{code[5:]}", "DOM"), ".example/", Value(f"transfer-{code}", "PROJ"),
        "/", Value(f"loader-{code[:6]}", "SVC"), ":", Value("latest", "CUSTOM", "keep"), ".",
    )


def public_protocol_example(code):
    return (
        "Открытая документация. Имя ", Value(f"remote-{code}", "USER", "keep"),
        " и узел ", Value(f"relay-{code[:5]}.example", "HOST", "keep"),
        " придуманы для упражнения. Версия ", Value("HTTP/3", "CUSTOM", "keep"), " опубликована.",
    )


def conflicting_status(code):
    return (
        "В выгрузке рядом с идентификатором ", Value(f"EXPORT-{code.upper()}", "CUSTOM", "review"),
        " указаны одновременно «публичный пример» и «рабочее значение». Источник не подтверждён.",
    )


def employee_export(code):
    surname = ("Avelorn", "Bellmere", "Cairnwell", "Morvane")[int(code[0], 16) % 4]
    name = f"Talia {surname}"
    phone = f"+1-202-555-01{int(code[2:4], 16) % 100:02d}"
    return (
        "The approved speaker biography names ", Value(name, "PERSON", "keep"),
        ". A confidential employee request from ", Value(name, "PERSON"),
        " lists the private callback number ", Value(phone, "PHONE"), ".",
    )


def customer_registry(code):
    return (
        "Закрытая сверка реестра. Клиент ", Value(f"registry-mora-{code[:5]}", "CLIENT"),
        " связан с организацией ", Value(f"Меридальный Свод {code[5:]}", "ORG"),
        " в проекте ", Value(f"Южный Вестник {code[:4].upper()}", "PROJ"),
        ". Общий формат выгрузки ", Value("CSV", "CUSTOM", "keep"), ".",
    )


TEMPLATES = {
    "train": (24, (
        (linux_home, "ru", "log"), (directory_login, "ru", "text"), (windows_profile, "en", "json"),
        (ssh_uri, "en", "text"), (database_env, "en", "env"), (two_prices, "ru", "text"),
        (private_contact, "ru", "text"), (internal_event, "en", "log"), (kubernetes_config, "en", "yaml"),
        (signed_link, "ru", "text"), (acquisition_note, "ru", "text"), (public_manual, "en", "text"),
        (detached_reference, "ru", "text"), (duty_roster, "ru", "text"),
        (account_agreement, "en", "text"),
    )),
    "validation": (12, (
        (mac_ticket, "ru", "text"), (windows_audit, "en", "json"), (invoice_csv, "en", "csv"),
        (internal_signature, "ru", "text"), (encoded_principal, "en", "log"), (ops_inventory, "en", "yaml"),
        (public_training_notice, "ru", "text"), (unresolved_ticket, "en", "text"),
        (escalation_card, "ru", "text"), (temporary_access_notice, "en", "text"),
    )),
    "test": (12, (
        (sssd_trace, "ru", "log"), (systemd_unit, "en", "ini"), (smb_failure, "ru", "text"),
        (renewal_sheet, "en", "yaml"), (support_export, "ru", "text"), (tool_result, "en", "json"),
        (registry_report, "en", "text"), (public_protocol_example, "ru", "text"),
        (conflicting_status, "ru", "text"), (employee_export, "en", "text"),
        (customer_registry, "ru", "text"),
    )),
}


def synthetic_rows():
    for split, (count, recipes) in TEMPLATES.items():
        for make_parts, language, format_name in recipes:
            group = make_parts.__name__.replace("_", "-")
            for index in range(1, count + 1):
                row_id = f"{group}-{index:02d}"
                code = sha256(f"kaponir/privacy-v0/{row_id}".encode()).hexdigest()[:10]
                pieces, spans = [], []
                offset = 0
                for part in make_parts(code):
                    text = part.text if isinstance(part, Value) else part
                    end = offset + len(text.encode("utf-8"))
                    if isinstance(part, Value):
                        spans.append({"start_byte": offset, "end_byte": end, "value": text,
                                      "kind": part.kind, "action": part.action})
                    pieces.append(text)
                    offset = end
                yield split, {"id": row_id, "group": group, "language": language, "format": format_name,
                              "source": "synthetic-template", "text": "".join(pieces), "spans": spans}
