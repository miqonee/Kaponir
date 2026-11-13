import copy
import unittest
from validation_pipeline import (Candidate, byte_to_char, candidate_context,
    check_decisions, combine, opaque_id, record_span, score, validation_hint)


class ValidationTests(unittest.TestCase):
    def candidate(self, value='Thu', **kwargs):
        return Candidate(opaque_id('doc', value), 'doc', value, 0, len(value), **kwargs)

    def decision(self, c, verdict='mask', kind='CUSTOM', scope='personal'):
        return {'id': c.id, 'verdict': verdict, 'kind': kind, 'scope': scope,
                'evidence': c.value, 'reason': 'Factual context.', 'valid': True}

    def test_utf8_offsets_are_exact_and_reject_split_codepoints(self):
        self.assertEqual(byte_to_char('Цена ₽', len('Цена '.encode())), 5)
        with self.assertRaises(ValueError): byte_to_char('Цена ₽', 1)

    def test_log_record_keeps_traceback_continuations(self):
        text='2026-11-12T12:00:00 ERROR failure\n  at /srv/private/run.py\n2026-11-12T12:00:01 INFO done\n'
        a=text.index('/srv');b=a+len('/srv/private/run.py')
        left,right=record_span(text,a,b)
        self.assertEqual(text[left:right],text[:text.index('2026-11-12T12:00:01')])

    def test_context_keeps_headers_without_detector_type_or_score(self):
        text='# Closed estimate\n\n## Prices\n\n| Item | Price |\n|---|---:|\n| one | 10 USD |\n'
        a=text.index('10 USD');c=Candidate('opaque','doc','10 USD',a,a+6,kind='SVC')
        value=candidate_context(c,text,len,256)
        self.assertIn('| Item | Price |',value['context'])
        self.assertNotIn('kind',value);self.assertNotIn('probability',value)
        self.assertEqual(value['record'],'| one | 10 USD |\n')

    def test_balanced_context_preserves_closing_preamble_status(self):
        text = '# Estimate\n\n'+'Opening names and details. '*30+'\nClosed commercial terms.\n\n## Prices\n\n| Item | Price |\n|---|---:|\n| a | 50 USD |\n'
        offset = text.index('| a |')
        hint = validation_hint(text, offset, 200, len, 'balanced')
        self.assertIn('# Estimate', hint)
        self.assertIn('Closed commercial terms.', hint)
        self.assertIn('| Item | Price |', hint)
        self.assertLessEqual(len(hint), 200)
        self.assertNotIn('Closed commercial terms.', validation_hint(text, offset, 200, len))

    def test_missing_duplicate_and_invented_ids_remain_reviewable(self):
        a,b=self.candidate(),self.candidate('Nov')
        contexts={c.id:{'record':c.value,'context':''} for c in [a,b]}
        for rows in [[self.decision(a)], [self.decision(a),self.decision(a)],
                     [self.decision(a),{**self.decision(b),'id':'invented'}]]:
            result=check_decisions({'decisions':rows},[a,b],contexts,['CUSTOM'])
            self.assertTrue(all(r['verdict']=='review' for r in result.values()))

    def test_malformed_id_in_prompt_json_does_not_abort_the_audit(self):
        c=self.candidate();context={c.id:{'record':c.value,'context':''}}
        for ident in [[],{},None,42]:
            result=check_decisions({'decisions':[{**self.decision(c),'id':ident}]},[c],context,['CUSTOM'])
            self.assertEqual(result[c.id]['verdict'],'review')
            self.assertFalse(result[c.id]['valid'])

    def test_ungrounded_evidence_and_numeric_mistype_are_rejected(self):
        c=self.candidate('1 320,00 ₽')
        contexts={c.id:{'record':c.value,'context':''}}
        for row in [{**self.decision(c),'evidence':'not in source'},self.decision(c,kind='SVC')]:
            result=check_decisions({'decisions':[row]},[c],contexts,['CUSTOM','SVC'])
            self.assertEqual(result[c.id]['verdict'],'review')

    def test_protected_credentials_cannot_be_kept(self):
        c=self.candidate('fixture-secret',kind='SECRET',protected=True)
        keep=self.decision(c,'keep');original=copy.deepcopy(keep)
        self.assertEqual(combine(c,keep,keep)['verdict'],'mask')
        self.assertEqual(keep,original)

    def test_disagreement_or_transport_error_is_not_a_keep(self):
        c=self.candidate()
        self.assertEqual(combine(c,self.decision(c),self.decision(c,'keep'))['verdict'],'review')
        self.assertEqual(combine(c,self.decision(c),{'valid':False})['verdict'],'review')
        self.assertEqual(combine(c,self.decision(c),self.decision(c,scope='team'))['verdict'],'review')

    def test_two_valid_abstentions_are_a_valid_review_decision(self):
        c = self.candidate()
        result = combine(c, self.decision(c, 'review'), self.decision(c, 'review'))
        self.assertEqual(result['verdict'], 'review')
        self.assertTrue(result['valid'])
        self.assertNotIn('error', result)

    def test_invalid_keep_cannot_count_as_successful_filtering(self):
        c = self.candidate('Private')
        result = score([c], {c.id: {**self.decision(c, 'keep'), 'valid': False}},
                       {('doc', 'Private'): ('CUSTOM', 'personal')}, {})
        self.assertEqual(result['correct_values_filtered'], 0)
        self.assertEqual(result['needs_review_values'], 1)

    def test_metrics_expose_lost_true_values_and_do_not_count_uncertainty_as_filtered(self):
        a,b,c=self.candidate('Private'),self.candidate('Public'),self.candidate('Unknown')
        decisions={a.id:self.decision(a,'keep'),b.id:self.decision(b,'keep'),c.id:self.decision(c,'review')}
        result=score([a,b,c],decisions,{('doc','Private'):('CUSTOM','personal')},{'doc':['Public']})
        self.assertEqual(result['correct_values_filtered'],1)
        self.assertEqual(result['outside_values_filtered'],1)
        self.assertEqual(result['needs_review_values'],1)
        self.assertEqual(result['visible_outside'],1)


if __name__=='__main__': unittest.main()
