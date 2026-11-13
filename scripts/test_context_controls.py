import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from compare_local_models import DOCUMENTS
from evaluate_context_controls import ControlledClient, prepare_cases, review_payload
from validation_pipeline import Candidate


class ContextControlTests(unittest.TestCase):
    def client(self):
        client=ControlledClient.__new__(ControlledClient)
        client.args=argparse.Namespace(model='fixture',context_tokens=1000,output_tokens=100,
            reserve_tokens=10,timeout=2)
        client.mode='grammar';client.sampling={'temperature':0}
        client.worker=argparse.Namespace(ocr_base='http://fixture',
            prompt_token_count=lambda *args:17,count_text_tokens=len,resources=lambda:{},
            process=argparse.Namespace(poll=lambda:None))
        return client

    def test_truncated_reply_is_not_accepted_even_when_json_is_parseable(self):
        reply={'choices':[{'finish_reason':'length','message':{
            'content':'{"decisions":[]}','reasoning_content':'short thought'}}]}
        with patch('evaluate_context_controls.http_json',return_value=reply) as request:
            result=self.client().call('instruction',{'source':'quoted'}, {'type':'object'},True,100)
        self.assertEqual(result['error'],'model_response_truncated')
        self.assertNotIn('reply',result)
        self.assertTrue(request.call_args.args[1]['chat_template_kwargs']['enable_thinking'])
        self.assertGreater(result['record']['reasoning_text_tokens'],0)
        self.assertEqual(result['raw_final'],'{"decisions":[]}')

    def test_oversized_full_context_is_reported_without_shortening_input(self):
        client=self.client();client.worker.prompt_token_count=lambda *args:951
        with patch('evaluate_context_controls.http_json') as request:
            result=client.call('instruction',{'source':'whole document'}, {'type':'object'},False,100)
        self.assertEqual(result['error'],'model_context_budget_exceeded')
        request.assert_not_called()

    def test_bounded_thinking_reserves_output_for_final_answer_without_model_name_rules(self):
        client=self.client();client.job_id='job';client.args.reasoning_budget=None
        client.worker.start_runtime=Mock()
        client.configure_budget('bounded')
        self.assertEqual(client.current_budget,50)
        self.assertEqual(client.worker.start_runtime.call_args.kwargs['reasoning_budget'],50)
        client.configure_budget('on')
        self.assertEqual(client.current_budget,-1)

    def test_full_context_is_shared_and_original_detector_kind_is_not_sent(self):
        text='# Closed estimate\n\n| item | price |\n| a | 10 USD |\n'
        start=text.index('10 USD');c=Candidate('opaque','doc','10 USD',start,start+6,kind='SVC')
        value,contexts=review_payload([c],{'doc':text},len,256,'document')
        self.assertEqual(value['document'],text)
        self.assertNotIn('kind',value['items'][0])
        self.assertNotIn('context',value['items'][0])
        self.assertEqual(contexts[c.id]['context'],text)

    def test_reference_case_labels_and_types_are_not_candidate_fields(self):
        sources={name:'alpha beta gamma' for name in DOCUMENTS}
        expected={(DOCUMENTS[0],'alpha'):('USER','personal'),(DOCUMENTS[1],'beta'):('PERSON','personal')}
        public={name:['gamma'] for name in DOCUMENTS}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            cases,data=prepare_cases(root/'cases.json',sources,expected,public,4)
            self.assertEqual({case.file for case in cases},set(DOCUMENTS))
            self.assertEqual((data['reference_private'],data['reference_public']),(2,2))
            self.assertTrue(all(not case.protected and case.kind=='CUSTOM' for case in cases))
            self.assertEqual(set(data['candidates'][0]),{'id','file','value','start','end'})


if __name__=='__main__':unittest.main()
