#!/usr/bin/env python3
"""Render readable tables from the saved report, without running inference."""
import argparse
import json
from pathlib import Path

from compare_local_models import ROOT
from evaluate_scheme_matrix import METHODS

LABELS={'detector_proposals':'Предложения детекторов','current_union':'Детекторы + поиск LLM',
        'point':'Проверка предложений детекторов','regions':'Поиск в записях срабатывания',
        'union_point':'Один проход по объединённому пулу','double':'Два прохода по тому же пулу'}
MODELS={'qwen3.5-0.8b':'Qwen3.5-0.8B','qwen3.5-2b':'Qwen3.5-2B',
        'minicpm5-2b':'MiniCPM5-2B','huihui-qwen3.5-2b-abliterated':'Huihui',
        'nanbeige42-3b':'Nanbeige4.2-3B','granite42-3b':'Granite4.2-3B','gemma4-e4b-qat':'Gemma4 E4B QAT'}
STATUS={'not_run':'не запускался','running':'выполняется','completed':'завершён',
        'failed':'сбой','interrupted':'прерван'}


def table(headers,rows):
    return ['| '+' | '.join(headers)+' |','|'+'|'.join('---' for _ in headers)+'|',
            *['| '+' | '.join(map(str,row))+' |' for row in rows]]


def cell(metric):
    return '—' if not metric else f"{metric['visible_matches']} / {metric['visible_outside']}"


def error_note(row):
    notes=[]
    for key,label in [('request_errors','ошибки запросов/JSON, включая восстановленные дроблением'),
                      ('inference_errors','ошибки этапов; могут относиться к тем же запросам')]:
        errors=row.get(key,{})
        if errors:notes.append(label+': '+', '.join(
            f'{code} × {count}' for code,count in sorted(errors.items())))
    return '; '+'; '.join(notes) if notes else ''


def render(report):
    lines=['# Сводные таблицы экспериментов','',
        'Источник: [VALIDATION_RESULTS.json](VALIDATION_RESULTS.json). '
        'Методы и ограничения: [SCHEME_COMPARISON.md](SCHEME_COMPARISON.md).','',
        'Это синтетические документы. Совпадение — точное значение в правильном документе. '
        'Значения вне эталона включают ошибки границ и не все являются ложными срабатываниями.','',
        '## Общая матрица младших моделей','',
        'В ячейке: **осталось эталонных из 92 / осталось вне эталона**. '
        'Включены записи на review. Это не число автоматически правильных решений.','']
    primary=report['primary_matrix']['models']
    names=list(primary)
    lines+=table(['Схема',*[MODELS.get(name,name) for name in names]],[
        [LABELS[method],*[cell(primary[name].get('methods',{}).get(method)) for name in names]]
        for method in METHODS])
    lines+=['',*['- '+MODELS.get(name,name)+': '+STATUS.get(row['status'],row['status'])+'.'
        for name,row in primary.items()],'',
        'Пулы поиска ещё не классифицируют приватность и scope. '
        'Защищённые credentials сохранены правилами; это не заслуга LLM.','',
        '## Более тяжёлые модели','',
        'Те же 92 значения и шесть схем. Совместимый JSON-режим выбирается технической '
        'пробой runtime; эффективный режим указан под таблицей. '
        'Основной профиль temperature=0 отделён от рекомендованных sampling-профилей ниже.','']
    lines+=['Контекст 4096 и выход 1600 совпадают с младшими моделями. '
        'Для больших моделей CPU-таймаут увеличен с 300 до 1200 секунд; '
        'все запросы младших моделей основной матрицы завершились в пределах 300 секунд. '
        'В режиме prompt лимит 24 кандидата задаётся инструкцией и порогом дробления входа; '
        'ответ может содержать больше строк. Это отличается от ограничения JSON-грамматикой.','']
    additional=report.get('additional_document_runs',{})
    names=list(additional)
    lines+=table(['Схема',*[MODELS.get(name,name) for name in names]],[
        [LABELS[method],*[cell(additional[name].get('metrics',{}).get(method)) for name in names]]
        for method in METHODS])
    lines+=['',*['- '+MODELS.get(name,name)+': '+STATUS.get(row['status'],row['status'])+
        '; JSON: '+str(row.get('structured_output','—'))+error_note(row)+'.' for name,row in additional.items()],
        '', 'Gemma использует QAT Q4_0, остальные — Q4_K_M. Это не одинаковое квантование.','']
    lines+=['## Объединение сохранённых региональных находок с детекторами','',
        'Анализ уже полученных списков, **без новых запросов LLM**. '
        'Все исходные предложения сохранены и дополнены региональными находками. '
        'Это расчёт кандидатного пула, а не отдельный прогон проверки приватности, '
        'типа, scope или применения правил. Основные шесть схем выше не меняются.','']
    derived=[]
    for name,row in report.get('derived_additive_region_pools',{}).items():
        if row['status']!='derived_from_saved_pools':values=['—','—']
        elif row['source_region_errors']:values=['неполный','—']
        else:values=[cell(row),f"{row['added_matches']} / {row['added_outside']}"]
        derived.append([MODELS.get(name,name),*values])
    lines+=table(['Модель','В эталоне / вне эталона','Добавлено: эталон / вне эталона'],derived)
    lines+=['','## Проверка на правильных границах кандидатов','',
        '32 отрезка: 16 приватных, 16 публичных. Метки и типы не отправляются модели; '
        'обход LLM для защищённых credentials отключён. Это контроль LLM, а не recall детекторов. '
        'Review включает ошибки формата и цитирования. Числа подтверждения приватного '
        'не гарантируют правильный тип и scope.','']
    reviews=[];discovery=[];pending=[]
    for name,run in report.get('context_controls',{}).items():
        # This interrupted pilot is technical evidence, not another quality run.
        if name=='qwen08-native-unbounded':continue
        if run['status']=='not_run':pending.append(name);continue
        for method,m in run.get('metrics',{}).items():
            incomplete=m.get('quality_is_lower_bound_if_incomplete',False)
            if method.startswith('review-'):
                if incomplete:
                    reviews.append([name,method,*['неполный']*6,m.get('failed_batches','—')]);continue
                counts=[m['confirmed_matches'],m['outside_values_filtered'],m['correct_values_filtered'],
                        m['confirmed_outside'],m['needs_review_values']]
                if sum(counts)!=m['cases']:raise ValueError('review_confusion_count_mismatch')
                joint=m.get('kind_and_scope_correct_without_protection')
                metadata=('—' if not counts[0] else
                    f'{joint}/{counts[0]}' if joint is not None else 'нет оценки')
                reviews.append([name,method,*counts,metadata,m.get('failed_batches','—')])
            elif method.startswith('discovery-'):
                discovery.append([name,method,'неполный' if incomplete else cell(m),
                    m.get('complete_documents','—')])
    lines+=table(['Профиль','Метод','Верный mask приватного','Верный keep публичного',
        'Ошибочный keep приватного','Ошибочный mask публичного','Review',
        'Верны тип и scope / mask приватного','Сбоев пакетов'],reviews)
    lines+=['','`record` — запись с подсказкой; `document` — весь документ. '
        '`bounded` — лимит reasoning в половину начального ответа; `on` — без этого ограничения. '
        'Профили `prompt` меняют и инструкцию формата, и способ ограничения JSON. '
        '`single` содержит один кандидат на запрос. Точные параметры — в JSON-сводке.','',
        'Завершённые этапы ещё выполняющегося прогона показаны отдельно. '
        'Технически неполные этапы не получают численную оценку качества. '
        'Сбой пакета оставляет его кандидатов на review; ноль подтверждений после '
        'таймаута не измеряет способность модели распознать приватность.','']
    control_errors=[
        '- `'+name+'`: '+', '.join(f'{code} × {count}' for code,count in sorted(
            run['inference_errors'].items()))+'.'
        for name,run in report.get('context_controls',{}).items() if run.get('inference_errors')]
    if control_errors:
        lines+=['Технические ошибки сохранённых запросов контрольных профилей '
            '(включая ещё выполняющиеся этапы):','',*control_errors,'']
    diagnostic_rows=[]
    for name,run in report.get('context_controls',{}).items():
        for method,diagnostic in run.get('reason_length_diagnostics',{}).items():
            if not diagnostic['rows_rejected_only_for_reason_length']:continue
            for label,key in [('Исходный контракт','strict'),('Обрезан только reason','reason_clipped')]:
                m=diagnostic[key]
                quality=(run.get('metrics',{}).get(method,{}) if key=='strict' else
                         diagnostic['metadata_after_clip'])
                joint=quality.get('kind_and_scope_correct_without_protection')
                metadata=('—' if not m['confirmed_matches'] else
                    f"{joint}/{m['confirmed_matches']}" if joint is not None else 'нет оценки')
                diagnostic_rows.append([name,method,label,m['confirmed_matches'],
                    m['outside_values_filtered'],m['correct_values_filtered'],
                    m['confirmed_outside'],m['needs_review_values'],metadata])
    if diagnostic_rows:
        lines+=['## Диагностика лимита объяснения','',
            'Повторная обработка тех же сохранённых ответов, **без новых запросов модели**. '
            'Во втором варианте reason обрезан до 120 символов; остальные проверки сохранены. '
            'Исходные результаты выше не меняются. Это разбор влияния ограничения формата, '
            'а не проверенный режим автоматического применения.','']
        lines+=table(['Профиль','Метод','Обработка ответа','Верный mask приватного',
            'Верный keep публичного','Ошибочный keep приватного','Ошибочный mask публичного','Review',
            'Верны тип и scope / mask приватного'],diagnostic_rows)
        lines+=['','Обрезка объяснения может пропустить и ошибочные keep. '
            'Соответствие длине текста не доказывает правильность приватности, типа или scope.','']
    lines+=['## Поиск по целому документу','',
        'В ячейке те же эталонные/вне эталона. Пул включает быстрые детекторы. '
        'Флаги confirmed и scope у поискового пула не означают проверенную приватность. '
        'По сравнению с основной матрицей здесь также увеличены лимиты контекста, '
        'ответа и предложений; это сравнение профилей.','']
    lines+=table(['Профиль','Метод','Осталось / вне эталона','Документов завершено'],discovery)
    if pending:lines+=['','Ещё не запускались: '+', '.join('`'+name+'`' for name in pending)+'.']
    holdout=report.get('selected_holdout',{'status':'not_run'})
    lines+=['','## Отложенные документы','',
        '127 эталонных значений в пяти других документах. Выбор добавляющего поиск '
        'профиля сделан по калибровке, без автоматического отклонения кандидатов.','',
        'Статус: '+STATUS.get(holdout['status'],holdout['status'])+'.','']
    selected=holdout.get('signature',{}).get('model') or report.get('holdout_selection',{}).get('model')
    if selected:lines+=['Модель: '+MODELS.get(selected,selected)+'.','']
    if holdout.get('metrics'):
        lines+=table(['Схема','Осталось / вне эталона'],[
            [LABELS[method],cell(holdout['metrics'].get(method))]
            for method in ['detector_proposals','current_union']])
    lines+=['','Результаты коротких диагностических проб на 20 случаях не включены '
        'в документную матрицу. Сырые контрольные точки и их SHA — '
        '[benchmarks](benchmarks/README.md).','']
    return '\n'.join(lines)


def write_tables(report,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    partial=path.with_suffix('.tmp')
    partial.write_text(render(report));partial.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=ROOT/'docs/VALIDATION_RESULTS.json')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/VALIDATION_TABLES.md')
    args=parser.parse_args();write_tables(json.loads(args.report.read_text()),args.output)


if __name__=='__main__':main()
