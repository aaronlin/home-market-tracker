from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime,timezone
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from app_config import SetupError
from models import TrackerRun
from provider import ProviderError
from run_weekly import build_run,run_preview,run_send
from test_home_market_tracker import settings
from test_regional import payload as market_payload


def property_payload():
    return {'avm':{'subjectProperty':{'zipCode':'90000'}},
        'sale_listings':[{'formattedAddress':'2 Example St, Sampletown, CA 90000','propertyType':'Single Family',
            'price':1000000,'listedDate':'2026-11-01','lastSeenDate':'2026-11-10',
            'status':'Active','mlsNumber':'MLS1','squareFootage':1500,'bedrooms':3,'bathrooms':2,'distance':.5}],
        'properties':[], '_retrieval':[
            {'source':'listings_active','rows':1,'pages':1,'complete':True,'filters':{'propertyType':'Single Family'}},
            {'source':'listings_inactive','rows':0,'pages':1,'complete':True,'filters':{'propertyType':'Single Family'}},
            {'source':'properties','rows':0,'pages':1,'complete':True,'filters':{}}]}


class Provider:
    def __init__(self):self.home_calls=[];self.market_calls=[];self.error=None;self.home_error=None;self.payload=property_payload()
    def fetch_home_market(self,home):
        self.home_calls.append(home.id)
        if self.home_error:raise self.home_error
        return self.payload
    def fetch_market_history(self,zipcode,months):
        self.market_calls.append((zipcode,months))
        if self.error:raise self.error
        return market_payload()


class HeatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.config=settings(self.temp.name)
        self.config['tracker']['market_history']={'cache_root':str(self.root/'formal-cache'),
                                                  'capture_root':str(self.root/'formal-captures')}
        self.provider=Provider();self.now='2026-11-10T12:00:00+00:00'
        clock=patch('run_weekly.TrackerRun.now',side_effect=lambda homes:TrackerRun(homes,self.now));clock.start();self.addCleanup(clock.stop)
        normalization=patch('normalize.datetime',wraps=datetime);normalization.start().now.return_value=datetime(2026,11,10,tzinfo=timezone.utc);self.addCleanup(normalization.stop)

    def test_preview_isolated_from_formal_history_and_delivery_state(self):
        with patch('run_weekly.send_email',side_effect=AssertionError('no SMTP')),redirect_stdout(StringIO()):
            self.assertEqual(run_preview(self.config,self.provider),0)
        self.assertEqual(self.provider.market_calls,[('90000',36)])
        self.assertFalse((self.root/'formal-cache').exists())
        self.assertFalse((self.root/'formal-captures').exists())
        self.assertFalse((self.root/'state.yaml').exists())
        self.assertTrue(list((self.root/'previews/research').rglob('*.json')))

    def test_successful_capture_survives_failed_smtp(self):
        with patch('run_weekly.send_email',return_value='failed'):
            with self.assertRaises(SetupError):run_send(self.config,self.provider)
        self.assertFalse((self.root/'state.yaml').exists())
        self.assertTrue(list((self.root/'formal-captures').glob('*/captures/*.json')))
        self.assertTrue(list((self.root/'formal-cache').glob('*/latest-36.json')))
        before={p:p.read_bytes() for p in (self.root/'formal-captures').rglob('*') if p.is_file()}
        self.now='2026-11-11T12:00:00+00:00'
        preview=build_run(self.config,self.provider)
        self.assertEqual(preview.homes[0].capture_baseline.status,'comparable')
        self.assertEqual(before,{p:p.read_bytes() for p in (self.root/'formal-captures').rglob('*') if p.is_file()})

    def test_optional_market_failure_does_not_block_existing_weekly_send(self):
        self.provider.error=RuntimeError('secret-provider-error')
        with patch('run_weekly.send_email',return_value=None) as smtp,redirect_stdout(StringIO()):
            self.assertEqual(run_send(self.config,self.provider),0)
        self.assertTrue((self.root/'state.yaml').exists())
        self.assertNotIn('secret-provider-error',str(smtp.call_args))
        self.assertIn('2 Example St',smtp.call_args.args[2])

    def test_core_fetch_failure_still_blocks(self):
        self.provider.home_error=ProviderError('core-failed')
        with self.assertRaises(ProviderError):build_run(self.config,self.provider)
        self.assertEqual(self.provider.market_calls,[])

    def test_same_zip_across_homes_only_fetches_market_once(self):
        other=deepcopy(self.config['tracker']['homes'][0]);other['id']='other';self.config['tracker']['homes'].append(other)
        run=build_run(self.config,self.provider)
        self.assertEqual(self.provider.home_calls,['home','other'])
        self.assertEqual(self.provider.market_calls,[('90000',36)])
        self.assertEqual([h.regional_history.from_cache for h in run.homes],[False,True])

    def test_disabled_and_unknown_subject_zip_do_not_request(self):
        self.config['tracker']['market_history']['enabled']=False
        run=build_run(self.config,self.provider)
        self.assertIsNone(run.homes[0].regional_history)
        self.assertEqual(self.provider.market_calls,[])
        self.config['tracker']['market_history']['enabled']=True
        self.provider.payload['avm']['subjectProperty']={}
        run=build_run(self.config,self.provider)
        self.assertEqual(run.homes[0].regional_history.issue,'subject_zip_unavailable')
        self.assertEqual(self.provider.market_calls,[])

    def test_bad_options_fail_before_network(self):
        for value in ({'enabled':'true'},{'history_months':True},{'max_age_days':0},{'cache_root':None}):
            with self.subTest(value=value):
                self.config['tracker']['market_history']=value
                with self.assertRaises(SetupError):build_run(self.config,self.provider)
        self.assertEqual(self.provider.home_calls,[])
