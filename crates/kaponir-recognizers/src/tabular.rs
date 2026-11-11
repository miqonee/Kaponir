//! Табличный парсер (`CSV` / `TSV` / разделители `;`) с распространением контекста заголовков
//! (Column Header Propagation). Сохраняет кавычки, разделители и структуру таблицы при маскировании.

use kaponir_core::{AnalyzeCtx, Candidate, Category, Format, Input, Lexicon, Recognizer};

use crate::{is_placeholder, key_kind_class, kind_for_key};

const ID: &str = "tabular";

#[derive(Default)]
pub struct Tabular;

impl Tabular {
    pub fn new() -> Self {
        Self
    }
}

#[derive(Clone, Debug)]
struct ColumnSpec {
    header: String,
    kind: String,
    category: Category,
    hard: bool,
    prior: f32,
}

/// Разбивает одну строку CSV/TSV на байтовые диапазоны значений ячеек `(start, end)` внутри `text`.
/// Кавычки снаружи ячейки остаются за пределами диапазона, чтобы при подстановке `ANON_...`
/// синтаксис CSV оставался валидным.
fn parse_row_spans(
    text: &str,
    line_start: usize,
    line_end: usize,
    delim: char,
) -> Vec<(usize, usize)> {
    let line = &text[line_start..line_end];
    let bytes = line.as_bytes();
    let d_byte = delim as u8;
    let mut cells = Vec::new();
    let mut i = 0;

    loop {
        while i < bytes.len() && bytes[i] == b' ' && d_byte != b' ' {
            i += 1;
        }
        if i >= bytes.len() {
            cells.push((line_start + i, line_start + i));
            break;
        }
        if bytes[i] == b'"' {
            let inner_start = i + 1;
            i += 1;
            let mut inner_end = inner_start;
            while i < bytes.len() {
                if bytes[i] == b'"' {
                    if i + 1 < bytes.len() && bytes[i + 1] == b'"' {
                        i += 2;
                        inner_end = i;
                        continue;
                    }
                    inner_end = i;
                    i += 1;
                    break;
                }
                i += 1;
                inner_end = i;
            }
            let slice = &line[inner_start..inner_end];
            let lead = slice.len() - slice.trim_start().len();
            let trimmed_len = slice.trim().len();
            let s = line_start + inner_start + lead;
            cells.push((s, s + trimmed_len));
            while i < bytes.len() && bytes[i] != d_byte {
                i += 1;
            }
            if i < bytes.len() && bytes[i] == d_byte {
                i += 1;
                if i == bytes.len() {
                    cells.push((line_start + i, line_start + i));
                    break;
                }
            } else {
                break;
            }
        } else {
            let cell_start = i;
            while i < bytes.len() && bytes[i] != d_byte {
                i += 1;
            }
            let slice = &line[cell_start..i];
            let lead = slice.len() - slice.trim_start().len();
            let trimmed_len = slice.trim().len();
            let s = line_start + cell_start + lead;
            cells.push((s, s + trimmed_len));
            if i < bytes.len() && bytes[i] == d_byte {
                i += 1;
                if i == bytes.len() {
                    cells.push((line_start + i, line_start + i));
                    break;
                }
            } else {
                break;
            }
        }
    }
    cells
}

fn classify_header(lex: &Lexicon, raw_header: &str) -> Option<ColumnSpec> {
    let header = raw_header.trim().trim_matches(['"', '\'']);
    if header.is_empty() || header.len() > 64 {
        return None;
    }
    // Если заголовок выглядит как данные (число, email с @, URL), это не строка заголовков.
    if header.contains('@')
        || header.contains("://")
        || header
            .chars()
            .all(|c| c.is_ascii_digit() || ".,+- ".contains(c))
    {
        return None;
    }

    let norm: String = header
        .chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    if norm.is_empty() {
        return None;
    }

    let matched: Option<(&str, Category, bool, f32)> = match norm.as_str() {
        "password"
        | "passwd"
        | "userpassword"
        | "dbpassword"
        | "secret"
        | "secretkey"
        | "apikey"
        | "apitoken"
        | "accesstoken"
        | "refreshtoken"
        | "privatekey"
        | "token"
        | "пароль"
        | "секрет"
        | "токен"
        | "ключдоступа"
        | "приватныйключ" => Some(("SECRET", Category::Credential, true, 0.97)),
        "name"
        | "fullname"
        | "firstname"
        | "lastname"
        | "middlename"
        | "surname"
        | "person"
        | "employee"
        | "employeename"
        | "author"
        | "owner"
        | "manager"
        | "contactname"
        | "fio"
        | "фио"
        | "сотрудник"
        | "имя"
        | "фамилия"
        | "отчество"
        | "полноеимя"
        | "владелец"
        | "руководитель"
        | "ответственный" => Some(("PERSON", Category::Identity, false, 0.90)),
        "user"
        | "username"
        | "userid"
        | "login"
        | "account"
        | "accountname"
        | "логин"
        | "пользователь"
        | "учетнаязапись"
        | "аккаунт" => Some(("USER", Category::Identity, false, 0.90)),
        "email"
        | "useremail"
        | "workemail"
        | "contactemail"
        | "mail"
        | "emailaddress"
        | "почта"
        | "элпочта"
        | "электроннаяпочта" => Some(("EMAIL", Category::Contact, false, 0.92)),
        "phone"
        | "phonenumber"
        | "mobile"
        | "mobilephone"
        | "telephone"
        | "tel"
        | "телефон"
        | "номертелефона"
        | "мобильный" => Some(("PHONE", Category::Contact, false, 0.92)),
        "salary"
        | "wage"
        | "income"
        | "compensation"
        | "bonus"
        | "budget"
        | "balance"
        | "amount"
        | "totalamount"
        | "payment"
        | "зарплата"
        | "оклад"
        | "доход"
        | "премия"
        | "вознаграждение"
        | "бюджет"
        | "сумма"
        | "баланс"
        | "выплата" => Some(("AMOUNT", Category::Financial, false, 0.92)),
        "host"
        | "hostname"
        | "internalhost"
        | "server"
        | "servername"
        | "node"
        | "nodename"
        | "instance"
        | "ip"
        | "ipaddress"
        | "internalip"
        | "хост"
        | "сервер"
        | "узел"
        | "имясервера"
        | "ipадрес" => Some(("HOST", Category::Infra, false, 0.90)),
        "project"
        | "projectname"
        | "repo"
        | "repository"
        | "проект"
        | "названиепроекта"
        | "репозиторий" => Some(("PROJ", Category::Infra, false, 0.88)),
        "service" | "servicename" | "microservice" | "сервис" | "микросервис" => {
            Some(("SVC", Category::Infra, false, 0.88))
        }
        "client"
        | "clientname"
        | "customer"
        | "customername"
        | "counterparty"
        | "клиент"
        | "заказчик"
        | "контрагент" => Some(("CLIENT", Category::Custom, false, 0.90)),
        "company"
        | "companyname"
        | "organization"
        | "org"
        | "orgname"
        | "vendor"
        | "employer"
        | "компания"
        | "организация"
        | "юрлицо"
        | "работодатель" => Some(("ORG", Category::Custom, false, 0.90)),
        "domain" | "corpdomain" | "домен" => Some(("DOM", Category::Infra, false, 0.90)),
        "inn"
        | "snils"
        | "passport"
        | "passportnumber"
        | "taxid"
        | "ssn"
        | "card"
        | "cardnumber"
        | "iban"
        | "accountnumber"
        | "инн"
        | "снилс"
        | "паспорт"
        | "номерпаспорта"
        | "номеркарты"
        | "расчетныйсчет" => Some(("ID", Category::Identity, false, 0.92)),
        _ => None,
    };

    if let Some((kind, category, hard, prior)) = matched {
        return Some(ColumnSpec {
            header: header.to_string(),
            kind: kind.to_string(),
            category,
            hard,
            prior,
        });
    }

    if let Some(kind) = kind_for_key(lex, header) {
        let (category, hard, prior) = key_kind_class(kind);
        return Some(ColumnSpec {
            header: header.to_string(),
            kind: kind.to_string(),
            category,
            hard,
            prior,
        });
    }

    None
}

fn detect_delimiter(lines: &[&str], format: Format) -> Option<char> {
    match format {
        Format::Tsv => return Some('\t'),
        Format::Csv => {
            let first = lines.first().copied().unwrap_or_default();
            if first.contains(';') && !first.contains(',') {
                return Some(';');
            }
            return Some(',');
        }
        Format::Auto | Format::Text => {}
        _ => return None,
    }
    if lines.len() < 2 {
        return None;
    }
    for delim in ['\t', ',', ';'] {
        let c0 = lines[0].chars().filter(|&c| c == delim).count();
        let c1 = lines[1].chars().filter(|&c| c == delim).count();
        if c0 >= 1 && c0 == c1 {
            return Some(delim);
        }
    }
    None
}

fn is_trivial_cell(val: &str, category: Category) -> bool {
    let t = val.trim();
    if t.is_empty() || is_placeholder(t) {
        return true;
    }
    if matches!(
        t.to_ascii_lowercase().as_str(),
        "-" | "—" | "n/a" | "na" | "нет" | "не указано" | "unknown"
    ) {
        return true;
    }
    // Для финансовых колонок (`salary`, `budget`, `amount`) разрешаем короткие числа (`500`, `0` пропускаем).
    if category == Category::Financial {
        return t == "0" || t == "0.0" || t == "0,00";
    }
    t.len() < 2
}

impl Recognizer for Tabular {
    fn id(&self) -> &str {
        ID
    }

    fn analyze(&self, input: &Input<'_>, ctx: &AnalyzeCtx<'_>, out: &mut Vec<Candidate>) {
        let text = input.text;
        let mut line_ranges: Vec<(usize, usize, &str)> = Vec::new();
        let mut pos = 0;
        for raw_line in text.split('\n') {
            let line = raw_line.strip_suffix('\r').unwrap_or(raw_line);
            let start = pos;
            let end = start + line.len();
            pos += raw_line.len() + 1;
            if !line.trim().is_empty() {
                line_ranges.push((start, end, line));
            }
        }
        if line_ranges.len() < 2 {
            return;
        }
        let sample_lines: Vec<&str> = line_ranges.iter().take(4).map(|r| r.2).collect();
        let Some(delim) = detect_delimiter(&sample_lines, input.format) else {
            return;
        };

        let header_spans = parse_row_spans(text, line_ranges[0].0, line_ranges[0].1, delim);
        if header_spans.len() < 2 {
            return;
        }
        let specs: Vec<Option<ColumnSpec>> = header_spans
            .iter()
            .map(|&(s, e)| classify_header(ctx.lexicon, &text[s..e]))
            .collect();
        if !specs.iter().any(Option::is_some) {
            return;
        }

        for &(ls, le, _) in &line_ranges[1..] {
            let cells = parse_row_spans(text, ls, le, delim);
            // Строка должна иметь сопоставимое число колонок с заголовком.
            if cells.len() < 2 || cells.len().abs_diff(header_spans.len()) > 1 {
                continue;
            }
            for (col_idx, &(cs, ce)) in cells.iter().enumerate() {
                let Some(Some(spec)) = specs.get(col_idx) else {
                    continue;
                };
                if cs >= ce || is_trivial_cell(&text[cs..ce], spec.category) {
                    continue;
                }
                let mut cand =
                    Candidate::new(cs, ce, spec.kind.clone(), spec.category, ID, spec.prior)
                        .with_evidence(format!("column:{}", spec.header), 1.1);
                if spec.hard {
                    cand = cand.hard();
                }
                out.push(cand);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn csv_column_header_propagation_preserves_quotes_and_delimiters() {
        let lex = Lexicon::default();
        let csv = "id,user_email,full_name,salary,internal_host,status\n\
                   1,alice@corp.ru,\"Иванова Алиса Сергеевна\",\"285 000\",db-prod-01.internal,active\n\
                   2,bob@corp.ru,Бобров Борис,190000,cache-node-02,pending\n";
        let mut out = Vec::new();
        Tabular::new().analyze(
            &Input {
                text: csv,
                format: Format::Csv,
            },
            &AnalyzeCtx::new(&lex),
            &mut out,
        );
        let found: Vec<(String, String)> = out
            .iter()
            .map(|c| (c.kind.clone(), csv[c.start..c.end].to_string()))
            .collect();
        assert!(
            found.contains(&("EMAIL".into(), "alice@corp.ru".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("PERSON".into(), "Иванова Алиса Сергеевна".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("AMOUNT".into(), "285 000".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("HOST".into(), "db-prod-01.internal".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("PERSON".into(), "Бобров Борис".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("AMOUNT".into(), "190000".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("HOST".into(), "cache-node-02".into())),
            "{found:?}"
        );
        assert!(!found.iter().any(|(_, v)| v == "active" || v == "pending"));
    }

    #[test]
    fn tsv_auto_detection_works() {
        let lex = Lexicon::default();
        let tsv = "сотрудник\tпароль\tпроект\nПетров Пётр\tsup3rSecretPass\tАврора-Пэй\n";
        let mut out = Vec::new();
        Tabular::new().analyze(&Input::text(tsv), &AnalyzeCtx::new(&lex), &mut out);
        let found: Vec<(String, String)> = out
            .iter()
            .map(|c| (c.kind.clone(), tsv[c.start..c.end].to_string()))
            .collect();
        assert!(
            found.contains(&("PERSON".into(), "Петров Пётр".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("SECRET".into(), "sup3rSecretPass".into())),
            "{found:?}"
        );
        assert!(
            found.contains(&("PROJ".into(), "Аврора-Пэй".into())),
            "{found:?}"
        );
    }
}
