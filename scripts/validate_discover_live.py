#!/usr/bin/env python3
"""Audit opt-in discovery with real SystemOne, without keeping its credential."""
import argparse
import getpass
import json
from pathlib import Path
from live_validation import Audit


def run(args, key):
    audit = Audit(args, '', key)
    docs = [
        {'name': 'private.txt', 'text': 'Проект: Северный Вектор.\nЗакрытая цена договора: 1801234.37 RUB.\npassword: Synthetic_Discover_Q7!'},
        {'name': 'public.txt', 'text': 'Публичная цена в справочнике: 1801234.37 RUB.\nКомпания: «Тихий Берег».\ncontract_id: CN-381'},
    ]
    try:
        audit.configure('shadow', 'l2')
        first = len(audit.records)
        local = audit.api('/api/v1/ui/discover', {'documents': docs})
        assert len(audit.records) == first, 'local preview called S1'
        remote = audit.api('/api/v1/ui/discover', {'documents': docs, 'use_s1': True, 'remember': True})
        wire = [r for r in audit.records[first:] if r['path'] == '/v1/systemone']
        audit.save('local.json', local)
        audit.save('scored.json', remote)
        assert remote['s1_status'] == 'scored', remote['s1_status']
        assert wire and all('Synthetic_Discover_Q7!' not in json.dumps(r['request']) for r in wire)
        assert any(s['value'] == 'Северный Вектор' for s in remote['suggestions'])
        amount = next(s for s in remote['suggestions'] if s['kind'] == 'AMOUNT')
        assert amount['count'] == 2 and amount['documents'] == 2
        assert len(amount['examples']) == 2 and all(e['s1_score'] is not None for e in amount['examples'])
        again = audit.api('/api/v1/ui/discover', {'documents': docs, 'remember': True})
        assert next(s for s in again['suggestions'] if s['id'] == amount['id'])['count'] == 2
        audit.fault = True
        try:
            outage = audit.api('/api/v1/ui/discover', {'documents': docs, 'use_s1': True})
            assert outage['s1_status'] == 'unavailable'
            assert [s['id'] for s in outage['suggestions']] == [s['id'] for s in local['suggestions']]
            audit.save('outage.json', outage)
        finally:
            audit.fault = False
        audit.stop()
        audit.start()
        saved = audit.api('/api/v1/ui/discover/profile')
        assert next(s for s in saved['report']['suggestions'] if s['id'] == amount['id'])['count'] == 2
        audit.save('result.json', {'local_no_network': True, 'scored': remote['s1_scored'],
            'secret_absent_from_s1_requests': True, 'compound_name_found': True,
            'amount_examples': amount['examples'], 'repeat_not_double_counted': True,
            'outage_preserves_candidates': True, 'collection_survives_restart': True})
        print('READY ' + audit.base + '/ui/#discover', flush=True)
        print('Saved collection and S1 are configured. Use the browser, then press Enter to clean up.', flush=True)
        input()
        audit.save('final-profile.json', audit.api('/api/v1/ui/discover/profile'))
    finally:
        audit.close()
        audit.save('cleanup.json', {'temporary_directory_removed': not Path(audit.temp.name).exists(), 'server_stopped': audit.proc.poll() is not None})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', default='target/release/kaponir')
    p.add_argument('--upstream', default='https://route.fn.x3dev.ru/v1')
    p.add_argument('--output', default='artifacts/discovery-s1-2026-11-01')
    args = p.parse_args()
    run(args, getpass.getpass('S1 key (hidden): '))
