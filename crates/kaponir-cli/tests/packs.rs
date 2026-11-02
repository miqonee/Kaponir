//! Языковые пакеты в сборе: размеченные корпуса `packs/*/tests/corpus.jsonl` и круговой путь
//! «маскирование → ответ модели с окончаниями → восстановление».

use kaponir_cli::{Config, build_engine, load_packs};
use kaponir_core::{Engine, Escape, Input, MaskOptions, MemoryVault, Principal};
use std::path::Path;

fn engine() -> Engine {
    let cfg = Config::default();
    build_engine(&cfg, load_packs(&cfg).unwrap(), &[], [3; 32], None).unwrap()
}

fn corpus(pack: &str) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../packs")
        .join(pack)
        .join("tests/corpus.jsonl");
    let cases =
        kaponir_packs::corpus::parse_jsonl(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert!(!cases.is_empty());
    let r = kaponir_packs::corpus::run(&engine(), &cases);
    assert!(r.passed(), "{pack}: {:#?}", r.failures);
}

#[test]
fn lang_ru_corpus() {
    corpus("lang-ru");
}

#[test]
fn lang_en_corpus() {
    corpus("lang-en");
}

fn opts() -> MaskOptions {
    MaskOptions {
        owner: "me".into(),
        ..MaskOptions::default()
    }
}

#[test]
fn names_keep_case_endings_and_restore_inflected_forms() {
    let e = engine();
    let mut v = MemoryVault::new();
    let me = Principal::local("me");
    let m = e
        .mask(
            &Input::text("Иван Петров согласовал. Передай Анне Смирновой отчёт."),
            &opts(),
            &mut v,
        )
        .unwrap();
    assert_eq!(
        m.text,
        "ANON_FNAME_1 ANON_LNAME_1 согласовал. Передай ANON_FNAME_2е ANON_LNAME_2ой отчёт."
    );

    // Модель склоняет плейсхолдеры сама: окончание после токена восстанавливается по парадигме.
    let answer = "Напомню ANON_FNAME_1у ANON_LNAME_1у, а с ANON_FNAME_2ой ANON_LNAME_2ой я уже говорил. ANON_LNAME_1 в курсе.";
    let r = e.restore(answer, &me, &v, Escape::None).unwrap();
    assert_eq!(
        r.text,
        "Напомню Ивану Петрову, а с Анной Смирновой я уже говорил. Петров в курсе."
    );

    // Другая форма той же фамилии получает тот же токен: в vault — начальная форма.
    let again = e
        .mask(&Input::text("Звонил Петрову Ивану"), &opts(), &mut v)
        .unwrap();
    assert_eq!(again.text, "Звонил ANON_LNAME_1у ANON_FNAME_1у");
}

#[test]
fn session_memory_finds_inflected_forms_of_known_names() {
    let e = engine();
    let mut v = MemoryVault::new();
    let first = e
        .mask(
            &Input::text("Ответственный: Голошанов Сергей"),
            &opts(),
            &mut v,
        )
        .unwrap();
    assert_eq!(first.text, "Ответственный: ANON_LNAME_1 ANON_FNAME_1");
    // Контекст человека позволяет разобрать и строчную словоформу. Регистр входит в ключ
    // соответствия: у одинаковых «токен + окончание» должны быть разные исходные формы.
    let o = MaskOptions {
        known: first.known,
        ..opts()
    };
    let next = e
        .mask(
            &Input::text("отправь голошанову и Голошанову ссылку"),
            &o,
            &mut v,
        )
        .unwrap();
    assert_eq!(next.text, "отправь ANON_LNAME_2у и ANON_LNAME_1у ссылку");
    let restored = e
        .restore(&next.text, &Principal::local("me"), &v, Escape::None)
        .unwrap();
    assert_eq!(restored.text, "отправь голошанову и Голошанову ссылку");
}

#[test]
fn lowercase_name_needs_morphology_and_person_context() {
    let e = engine();
    let mut v = MemoryVault::new();
    let m = e
        .mask(
            &Input::text("передайте голошанову письмо и передайте документы"),
            &opts(),
            &mut v,
        )
        .unwrap();
    assert_eq!(
        m.text,
        "передайте ANON_LNAME_1у письмо и передайте документы"
    );
}

#[test]
fn name_before_json_escape_is_not_a_windows_path() {
    let e = engine();
    let mut v = MemoryVault::new();
    let m = e
        .mask(
            &Input::text(r"reviewer: Alice Brown\nКонтакты"),
            &opts(),
            &mut v,
        )
        .unwrap();
    assert_eq!(m.text, r"reviewer: ANON_FNAME_1 ANON_LNAME_1\nКонтакты");
}

#[test]
fn lowercase_name_in_json_roundtrips_exactly() {
    let e = engine();
    let mut v = MemoryVault::new();
    let text = concat!(
        "{\n  \"instructions\": \"Ответственный: Михаил Голошанов\\n",
        "передайте голошанову письмо о проверке доступа.\\n\\nРаздел 01.\\n\"\n}"
    );
    let m = e.mask(&Input::text(text), &opts(), &mut v).unwrap();
    let candidates = e.analyze(&Input::text(text));
    assert!(
        !m.text.contains("голошанову"),
        "{}\n{candidates:#?}",
        m.text
    );
    assert!(!m.text.contains("Голошанов"), "{}", m.text);
    let restored = e
        .restore(&m.text, &Principal::local("me"), &v, Escape::None)
        .unwrap();
    assert_eq!(restored.text, text);
}

#[test]
fn english_possessive_stays_after_token() {
    let e = engine();
    let mut v = MemoryVault::new();
    let m = e
        .mask(
            &Input::text("Mr. Smith's laptop and John Doe’s phone"),
            &opts(),
            &mut v,
        )
        .unwrap();
    assert_eq!(
        m.text,
        "Mr. ANON_LNAME_1's laptop and ANON_FNAME_1 ANON_LNAME_2’s phone"
    );
    let r = e
        .restore(&m.text, &Principal::local("me"), &v, Escape::None)
        .unwrap();
    assert_eq!(r.text, "Mr. Smith's laptop and John Doe’s phone");
}
