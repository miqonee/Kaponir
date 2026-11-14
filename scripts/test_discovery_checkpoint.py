"""A stopped recursive search must retain its completed model requests."""
import argparse
import json
import unittest

from experiment_inference import discover


class Client:
    model='fixture'
    output_mode='grammar'
    sampling={'temperature':0}

    def __init__(self,records=None,interrupt_at=None):
        self.records=[] if records is None else records
        self.invocations=[]
        self.interrupt_at=interrupt_at

    token_count=staticmethod(len)
    input_tokens=staticmethod(lambda *args:1)

    def infer(self,prompt,value,schema,output_tokens):
        self.invocations.append(value['source'])
        if len(self.invocations)==self.interrupt_at:raise KeyboardInterrupt
        self.records.append({'source':value['source']})
        return {'candidates':[{'value':word,'kind':'CUSTOM','confidence':1}
                              for word in dict.fromkeys(value['source'].split())]}


def settings(**overrides):
    return argparse.Namespace(**{'candidate_mode':'typed','max_candidates':2,'hint_tokens':0,
        'context_tokens':1000,'output_tokens':100,'reserve_tokens':10,'target_input_tokens':0,
        'strategy':'auto','max_fragments':32,**overrides})


class DiscoveryCheckpointTests(unittest.TestCase):
    def test_stop_between_parent_and_child_recovers_without_repeating_parent(self):
        text='alpha beta gamma delta'
        expected=discover(Client(),text,'instruction',['CUSTOM'],settings())
        legacy=[{'legacy':i} for i in range(5)]
        client=Client(records=legacy.copy(),interrupt_at=2);progress={};snapshots=[]
        def save():
            snapshots.append(json.loads(json.dumps({'progress':progress,'records':client.records})))
        with self.assertRaises(KeyboardInterrupt):
            discover(client,text,'instruction',['CUSTOM'],settings(),save,progress)
        saved=snapshots[-1]
        self.assertEqual(len(saved['progress']['responses']),1)
        resumed=Client(saved['records'])
        result=discover(resumed,text,'instruction',['CUSTOM'],settings(),progress=saved['progress'])
        self.assertEqual(result,expected)
        self.assertNotIn(text,resumed.invocations)
        self.assertEqual(len(resumed.records),12)
        self.assertEqual(resumed.records[:5],legacy)
        # Replayed parents update their original record, not the last child.
        for record in resumed.records[5:]:
            self.assertEqual(record['candidate_count'],len(record['source'].split()))

    def test_cache_is_scoped_to_format_sampling_output_and_schema(self):
        client=Client();progress={}
        def run(**kwargs):
            return discover(client,'alpha','instruction',['CUSTOM'],settings(**kwargs),progress=progress)
        first=run();self.assertEqual(run(),first)
        self.assertEqual(len(client.invocations),1)
        client.output_mode='prompt';run()
        client.sampling={'temperature':1};run()
        run(output_tokens=200)
        run(max_candidates=3)
        self.assertEqual(len(client.invocations),5)
        self.assertEqual(len(client.records),5)

    def test_missing_physical_record_is_a_checkpoint_error_not_a_model_result(self):
        client=Client();progress={}
        discover(client,'alpha','instruction',['CUSTOM'],settings(),progress=progress)
        next(iter(progress['responses'].values()))['record_index']=999
        with self.assertRaisesRegex(ValueError,'discovery_checkpoint_record_missing'):
            discover(client,'alpha','instruction',['CUSTOM'],settings(),progress=progress)
        self.assertEqual(len(client.invocations),1)

    def test_failed_write_is_fatal_even_if_a_later_write_would_succeed(self):
        client=Client();progress={};writes=[]
        def save():
            writes.append(True)
            if len(writes)==1:raise OSError('fixture disk error')
        with self.assertRaisesRegex(ValueError,'discovery_checkpoint_write_failed'):
            discover(client,'alpha beta gamma delta','instruction',['CUSTOM'],settings(),save,progress)
        self.assertEqual(len(client.invocations),1)
        self.assertEqual(len(writes),1)


if __name__=='__main__':unittest.main()
