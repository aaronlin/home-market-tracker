from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capture_history import capture_baseline, cohort_fingerprint, _digest
from models import ComparableSale,HomeConfig,HomeRun,RedfinReference,RetrievalInfo


def home_run():
    home=HomeConfig('sample','Synthetic home','1 Example St, Sampletown, CA 90000','Single Family',1.5,365,25,
                    display_count=1,ranked_comp_square_footage='1000:2000')
    rows=[ComparableSale(str(i),f'{i} Example St, Sampletown, CA 90000',sqft=1500,beds=3,baths=2,
                         listing_price=1000000,status='Active',last_seen_date='2026-11-10',listed_date='2026-11-01',
                         raw={'_listing_record':{'mlsNumber':f'MLS{i}'}},source_property_types={'listing':'Single Family'})
          for i in range(2,7)]
    retrieval=[RetrievalInfo('listings_active',5,1,True,{'propertyType':'Single Family'}),
               RetrievalInfo('listings_inactive',8,1,True,{'propertyType':'Single Family'}),
               RetrievalInfo('properties',10,1,True,{})]
    return HomeRun(home,rows,retrieval=retrieval,candidates=rows)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.formal=self.base/'formal';self.research=self.base/'research'
        self.now=datetime(2026,11,10,tzinfo=timezone.utc)

    def capture(self,run,now=None,persist=True):
        return capture_baseline(run,now or self.now,self.formal,persist=persist,research_root=self.research)

    def test_full_cohort_and_episode_identity_ignore_display_limit_and_price(self):
        run=home_run();first=self.capture(run)
        self.assertEqual(first.current_active_counts,{'Single Family':5})
        saved=json.loads(next(self.formal.glob('*/captures/*.json')).read_text())
        self.assertEqual(len(saved['observations']),5)
        changed=replace(run,comps=[replace(row,id='new-'+row.id,listing_price=900000) for row in run.comps],
                        candidates=[])
        self.capture(changed)
        records=[json.loads(p.read_text()) for p in self.formal.glob('*/captures/*.json')]
        self.assertEqual({r['observations'][0]['episode_key'] for r in records},
                         {saved['observations'][0]['episode_key']})
        self.assertEqual(cohort_fingerprint(run),cohort_fingerprint(replace(run,home=replace(run.home,display_count=4))))

    def test_exact_retry_is_idempotent_and_same_day_is_not_baseline(self):
        run=home_run();self.capture(run);second=self.capture(run)
        self.assertEqual(len(list(self.formal.glob('*/captures/*.json'))),1)
        self.assertEqual(second.status,'first_capture')
        later=replace(run,comps=run.comps[:-1],candidates=run.comps[:-1])
        compared=self.capture(later,datetime(2026,11,11,tzinfo=timezone.utc))
        self.assertEqual(compared.status,'comparable')
        self.assertEqual(compared.previous_active_counts,{'Single Family':5})
        self.assertEqual(compared.current_active_counts,{'Single Family':4})

    def test_preview_writes_research_only_and_reads_prior_formal(self):
        run=home_run();preview=self.capture(run,persist=False)
        self.assertFalse(self.formal.exists())
        self.assertTrue(list(self.research.glob('*/captures/*.json')))
        self.capture(run)
        before={p:p.read_bytes() for p in self.formal.rglob('*') if p.is_file()}
        preview=self.capture(run,datetime(2026,11,11,tzinfo=timezone.utc),persist=False)
        self.assertEqual(preview.status,'comparable')
        self.assertEqual(before,{p:p.read_bytes() for p in self.formal.rglob('*') if p.is_file()})

    def test_partial_or_legacy_metadata_cannot_supply_baseline(self):
        run=home_run();partial=replace(run,retrieval=[replace(r,complete=False) for r in run.retrieval])
        self.assertEqual(self.capture(partial).status,'partial')
        self.assertEqual(self.capture(run,datetime(2026,11,11,tzinfo=timezone.utc)).status,'first_capture')
        self.assertEqual(self.capture(replace(run,retrieval=[])).status,'partial')

    def test_filter_change_and_static_source_correction_reset_baseline(self):
        run=home_run();self.capture(run)
        changed=replace(run,home=replace(run.home,ranked_comp_square_footage='1200:2000'))
        self.assertEqual(self.capture(changed,datetime(2026,11,11,tzinfo=timezone.utc)).status,'changed_cohort')
        changed=replace(run,comps=[replace(r,sqft=1600) for r in run.comps],
                        candidates=[replace(r,sqft=1600) for r in run.candidates])
        corrected=self.capture(changed,datetime(2026,11,12,tzinfo=timezone.utc))
        self.assertEqual(corrected.status,'changed_cohort')
        self.assertEqual(corrected.issue,'source_claims_changed')

    def test_condo_stale_identity_conflict_and_duplicate_are_excluded(self):
        run=home_run()
        rows=[replace(run.comps[0],source_property_types={'listing':'Condo'}),
              replace(run.comps[1],last_seen_date='2026-10-01'),
              replace(run.comps[2],raw={'_listing_identity_conflict':True}),run.comps[3],run.comps[3]]
        result=self.capture(replace(run,comps=rows,candidates=rows))
        self.assertEqual(result.current_active_counts,{'Single Family':1})

    def test_corrupt_prior_record_shapes_are_not_exposed_as_typed_baseline(self):
        for invalid in (None, [], {'Single Family': True}):
            with self.subTest(counts=invalid):
                folder=self.base/str(len(list(self.base.iterdir())))
                run=home_run()
                capture_baseline(run,self.now,folder,persist=True)
                path=next(folder.glob('*/captures/*.json'));saved=json.loads(path.read_text())
                saved['active_counts']=invalid;path.unlink()
                path.with_name(_digest(saved)+'.json').write_text(json.dumps(saved))
                result=capture_baseline(run,datetime(2026,11,11,tzinfo=timezone.utc),folder,persist=True)
                self.assertEqual(result.status,'first_capture')

    def test_reference_check_date_does_not_reset_but_semantic_change_does(self):
        run=home_run()
        reference=RedfinReference(run.comps[0].address,run.comps[0].address,
            'https://www.redfin.com/CA/Sampletown/2-Example-St-90000/home/123','2026-11-10','page',
            raw={'checked_at':'2026-11-10','values':{'beds':3},'source_group':'mls_listing'})
        row=replace(run.comps[0],redfin_reference=reference)
        run=replace(run,comps=[row],candidates=[row]);self.capture(run)
        updated=replace(reference,checked_at='2026-11-11',raw={**reference.raw,'checked_at':'2026-11-11'})
        newer=replace(row,redfin_reference=updated)
        next_run=replace(run,comps=[newer],candidates=[newer])
        self.assertEqual(self.capture(next_run,datetime(2026,11,11,tzinfo=timezone.utc)).status,'comparable')
        updated=replace(reference,raw={**reference.raw,'values':{'beds':4}})
        newer=replace(row,redfin_reference=updated)
        self.assertEqual(self.capture(replace(run,comps=[newer],candidates=[newer]),
                                     datetime(2026,11,12,tzinfo=timezone.utc)).status,'changed_cohort')

    def test_invalid_optional_capture_data_does_not_raise(self):
        run=home_run();bad=replace(run,comps=[replace(run.comps[0],sqft=float('nan'))],candidates=[])
        result=self.capture(bad)
        self.assertEqual(result.status,'partial')
        self.assertEqual(result.issue,'capture_store_unavailable')
