"""The comparison must reject mixed datasets, prompts and inference profiles."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from compare_local_models import DOCUMENTS, ROOT
from check_learning_corpus import load_corpus
from evaluate_scheme_matrix import COHORT, PROFILE, summarize
from report_validation_results import build_report, reference_coverage, run_summary


class MatrixTests(unittest.TestCase):
    def test_reference_coverage_keeps_invalid_keep_visible_and_attributes_by_gold_type(self):
        stage={'candidates':[
            {'id':'a','file':'doc','value':'Alice','kind':'SVC','protected':False},
            {'id':'b','file':'doc','value':'host','kind':'HOST','protected':False},
            {'id':'c','file':'doc','value':'secret','kind':'SECRET','protected':True}],
            'decisions':{'a':{'valid':False,'verdict':'keep'},
                         'b':{'valid':True,'verdict':'keep'},
                         'c':{'valid':True,'verdict':'keep'}}}
        expected={('doc','Alice'):('PERSON','personal'),('doc','host'):('HOST','team'),
                  ('doc','secret'):('SECRET','personal')}
        rows=reference_coverage({'stages':{'point':stage}},expected)['point']
        self.assertEqual(rows['PERSON']['visible'],1)
        self.assertEqual(rows['PERSON']['confirmed_including_protection'],0)
        self.assertEqual(rows['HOST']['filtered'],1)
        self.assertEqual(rows['SECRET']['confirmed_including_protection'],1)

    def state(self, model):
        _,_,_,sources,_=load_corpus()
        return {'status':'completed','signature':{'model':model,'artifact':[],
            'runtime_sha256':'runtime',
            'source_sha256':{name:hashlib.sha256(sources[name].encode()).hexdigest() for name in DOCUMENTS},
            'prompts':{'primary':'prompt'},
            'settings':{'documents':DOCUMENTS,'schemes':['free','point','regions','double'],
                        **PROFILE}},'metrics':{},
            'annotations_sha256':hashlib.sha256((ROOT/'tests/docs/expected.json').read_bytes()).hexdigest()}

    def write(self, directory, model, state):
        path=directory/COHORT[model]/'result.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(state))

    def test_partial_matrix_keeps_missing_models_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            model=next(iter(COHORT));self.write(directory,model,self.state(model))
            result=summarize(directory)
            self.assertEqual(result['expected_values'],92)
            self.assertEqual(result['models'][model]['status'],'completed')
            self.assertEqual(sum(row['status']=='not_run' for row in result['models'].values()),len(COHORT)-1)

    def test_probe_profile_or_different_corpus_cannot_enter_document_matrix(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);model=next(iter(COHORT))
            for settings in [{'schemes':[]},{'documents':['01-cost-estimate.md']},
                             {'batch_size':1},{'temperature':1},{'max_candidates':48},
                             {'max_fragments':512},{'timeout':600},{'top_k':64},{'seed':42},
                             {'thinking':'on'},{'validation_context':'document'}]:
                state=self.state(model);state['signature']['settings'].update(settings)
                self.write(directory,model,state)
                with self.assertRaisesRegex(ValueError,'matrix_profile_mismatch'):summarize(directory)

    def test_different_runtime_or_prompt_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);a,b=list(COHORT)[:2]
            self.write(directory,a,self.state(a))
            for field,value in [('runtime_sha256','different'),('prompts',{'primary':'changed'})]:
                state=copy.deepcopy(self.state(b));state['signature'][field]=value
                self.write(directory,b,state)
                with self.assertRaisesRegex(ValueError,'matrix_provenance_mismatch'):summarize(directory)

    def test_matching_old_corpus_hashes_do_not_override_current_annotations(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            for model in COHORT:
                state=self.state(model);state['annotations_sha256']='old'
                self.write(directory,model,state)
            with self.assertRaisesRegex(ValueError,'matrix_corpus_mismatch'):summarize(directory)

    def test_report_keeps_unexecuted_models_and_holdout_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            report=build_report(Path(temporary))
            self.assertEqual(report['calibration_expected_values'],92)
            self.assertEqual(report['holdout_expected_values'],127)
            self.assertTrue(all(row['status']=='not_run' for row in report['primary_matrix']['models'].values()))
            self.assertEqual(report['selected_holdout']['status'],'not_run')
            self.assertTrue(all(row['status']=='not_run' for row in report['additional_document_runs'].values()))
            self.assertTrue(all(row['status']=='not_run' for row in report['context_controls'].values()))

    def test_diagnostic_probes_cannot_be_presented_as_document_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);model=next(iter(COHORT))
            state=self.state(model)
            state.update(shadow=True,rules_changed=0,calls=[],responses={},
                         probes={'summary':{'point':{'cases':20}},'rows':[]})
            path=directory/'minicpm-probes'/'result.json';path.parent.mkdir()
            path.write_text(json.dumps(state))
            report=build_report(directory)
            self.assertEqual(report['diagnostic_runs']['minicpm-probes']['probes']['point']['cases'],20)
            self.assertEqual(report['primary_matrix']['models'][model]['status'],'not_run')

    def test_malformed_probe_answers_remain_missing_in_raw_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'result.json';model=next(iter(COHORT))
            for malformed in [None,[{'id':[]}]]:
                state=self.state(model)
                state.update(shadow=True,rules_changed=0,calls=[],responses={
                    'first':{'reply':{'decisions':malformed}},
                    'second':{'reply':{'decisions':[{'id':'case','verdict':'keep'}]}},
                },probes={'summary':{'point':{'cases':1},'audit':{'cases':1}},
                          'rows':[{'id':'case','case':{'expected_kind':'USER'}}]})
                path.write_text(json.dumps(state))
                report=run_summary(path)['raw_verdict_diagnostics']
                self.assertEqual(report['point']['missing_or_unparseable'],1)
                self.assertEqual(report['audit']['private_kept'],1)


if __name__=='__main__':unittest.main()
