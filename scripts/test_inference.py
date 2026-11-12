"""Budget planning, source coverage, provider parsing, and credential-free telemetry."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from kaponir_inference import plan_text, split_span, document_hint
from evaluate_external_model import ExternalClient, ScopedRedirect, parse_model_json
from urllib.request import Request


class PlanningTests(unittest.TestCase):
    def test_every_character_survives_unicode_and_long_tables(self):
        text='# Закрытая смета\n\n| Позиция | Цена |\n|---|---:|\n'+''.join(f'| Строка {i} | {i} 320,00 ₽ |\n' for i in range(40))+'\nПоследняя строка без перевода'
        pieces=plan_text(text,lambda source,ctx:len((source+ctx).encode())<=350)
        self.assertEqual(''.join(text[p['start']:p['end']] for p in pieces),text)
        self.assertTrue(all(len(text[p['start']:p['end']].encode())<=350 for p in pieces))
        self.assertEqual(pieces[0]['start'],0)
        self.assertEqual(pieces[-1]['end'],len(text))

    def test_budget_includes_instructions_and_per_fragment_context(self):
        text='alpha beta\n\ngamma delta\n\nepsilon zeta'
        def context(offset):return 'document context' if offset else 'title'
        pieces=plan_text(text,lambda source,ctx:len('SYSTEM'+source+ctx)<=35,context)
        for piece in pieces:
            self.assertLessEqual(len('SYSTEM'+text[piece['start']:piece['end']]+piece['context']),35)
            self.assertEqual(piece['context'],context(piece['start']))

    def test_paragraph_mode_keeps_short_paragraphs_separate(self):
        text='paragraph one\n\nparagraph two\n\nlast'
        self.assertEqual(len(plan_text(text,lambda *_:True)),1)
        self.assertEqual(len(plan_text(text,lambda *_:True,strategy='paragraph')),3)

    def test_safety_limit_counts_generated_fragments_not_input_paragraphs(self):
        text='short\n\n'*2000
        self.assertEqual(len(plan_text(text,lambda *_:True,max_fragments=2)),1)
        with self.assertRaisesRegex(ValueError,'document_fragment_limit'):
            plan_text(text,lambda *_:True,strategy='paragraph',max_fragments=2)

    def test_impossible_budget_and_single_character_terminate(self):
        with self.assertRaisesRegex(ValueError,'model_fragment_cannot_split'):
            plan_text('字',lambda *_:False)

    def test_split_prefers_whole_log_lines(self):
        text='one long record with spaces\nsecond record with spaces\nthird record'
        left,right=split_span(text,0,len(text))
        self.assertTrue(text[left[0]:left[1]].endswith('\n'))
        self.assertEqual(text[left[0]:left[1]]+text[right[0]:right[1]],text)

    def test_headings_and_table_header_survive_at_later_rows(self):
        text='# Private contract\n\n## Prices\n\n| Item | Price |\n|---|---:|\n| one | 10 USD |\n| two | 20 USD |\n'
        hint=document_hint(text,text.index('| two'),200,len)
        self.assertIn('# Private contract',hint)
        self.assertIn('## Prices',hint)
        self.assertIn('| Item | Price |',hint)
        self.assertNotIn('20 USD',hint)

    def test_embedded_worker_does_not_require_adjacent_python_module(self):
        root=Path(__file__).resolve().parent
        code="__name__='embedded_test'\n"+(root/'kaponir_inference.py').read_text()+'\n'+(root/'kaponir_worker.py').read_text()+"\nassert callable(Worker.evaluate)\n"
        with tempfile.TemporaryDirectory() as cwd:
            result=subprocess.run([sys.executable,'-c',code],cwd=cwd,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


class ProviderTests(unittest.TestCase):
    def test_only_a_complete_json_document_or_complete_fence_is_accepted(self):
        for text in [' {"candidates":[]} ','```json\n{"candidates":[]}\n```','```\n{"candidates":[]}\n```']:
            self.assertEqual(parse_model_json(text),{'candidates':[]})
        for text in ['here is JSON {"candidates":[]}', '```json\n{}', '```json\n{}\n``` trailing', '{} trailing']:
            with self.assertRaises(ValueError):parse_model_json(text)

    def test_telemetry_does_not_contain_key_or_source_or_model_output(self):
        client=ExternalClient('https://fixture.invalid/v1','fixture','PRIVATE_KEY')
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return json.dumps({'model':'fixture','choices':[{'finish_reason':'stop','message':{'content':'```json\n{"value":"PRIVATE_OUTPUT"}\n```'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':8,'total_tokens':18,'unsafe':'PRIVATE_USAGE'}}).encode()
        with patch.object(client.opener,'open',return_value=Response()) as call:
            client.raw({'messages':[{'role':'user','content':'PRIVATE_SOURCE'}],'max_tokens':20})
            self.assertEqual(call.call_args.args[0].get_header('Authorization'),'Bearer PRIVATE_KEY')
        record=json.dumps(client.records)
        self.assertNotIn('PRIVATE_',record)
        self.assertEqual(client.records[0]['content_format'],'json_fence')
        self.assertTrue(client.records[0]['content_json_valid'])

    def test_authorization_is_not_redirected_to_another_host(self):
        with self.assertRaisesRegex(ValueError,'provider_redirect_rejected'):
            ScopedRedirect().redirect_request(Request('https://first.invalid/v1',headers={'Authorization':'Bearer fixture'}),None,302,'redirect',{},'https://other.invalid/v1')


if __name__=='__main__':unittest.main()
