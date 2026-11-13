import unittest

from analyze_reason_limits import clip_reasons
from validation_pipeline import Candidate,check_decisions,score


class ReasonLimitTests(unittest.TestCase):
    def setUp(self):
        self.candidate=Candidate('a','doc','10 ₽',0,4)
        self.contexts={'a':{'record':'10 ₽','context':'Closed commercial quote: 10 ₽'}}
        self.row={'id':'a','verdict':'keep','kind':'AMOUNT','scope':'global',
                  'evidence':'10 ₽','reason':'x'*144}

    def decisions(self,row):
        return check_decisions({'decisions':[row]},[self.candidate],self.contexts,['AMOUNT','SVC'])

    def test_cosmetic_normalization_can_also_expose_an_unsafe_keep(self):
        raw={'decisions':[self.row]};normalized=clip_reasons(raw)
        self.assertEqual(len(raw['decisions'][0]['reason']),144)
        self.assertEqual(len(normalized['decisions'][0]['reason']),120)
        before=self.decisions(self.row);after=self.decisions(normalized['decisions'][0])
        gold={('doc','10 ₽'):('AMOUNT','global')}
        self.assertEqual(score([self.candidate],before,gold,{})['correct_values_filtered'],0)
        self.assertEqual(score([self.candidate],after,gold,{})['correct_values_filtered'],1)

    def test_clipping_does_not_repair_wrong_evidence_type_or_missing_ids(self):
        self.assertEqual(clip_reasons({'decisions':None}),{'decisions':None})
        for row in [{**self.row,'kind':'SVC'},{**self.row,'evidence':'invented quote'}]:
            result=clip_reasons({'decisions':[row]})
            self.assertFalse(self.decisions(result['decisions'][0])['a']['valid'])
        other=Candidate('b','doc','20 ₽',5,9)
        result=check_decisions(clip_reasons({'decisions':[self.row]}),
            [self.candidate,other],self.contexts,['AMOUNT'])
        self.assertTrue(all(not row['valid'] for row in result.values()))


if __name__=='__main__':unittest.main()
