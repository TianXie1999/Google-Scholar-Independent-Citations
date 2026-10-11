import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from report import classify, generate, scholar_record, name_key
from collect import safe_data
from collect import collect, Scholar


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.authors=['Tian Xie','Xueru Zhang']
        self.ids={name_key('Tian Xie'):'A',name_key('Xueru Zhang'):'B'}

    def test_id_precedence(self):
        self.assertEqual(classify(self.authors,[{'name':'X Zhang','author_id':'C'}],self.ids,False)[0],'independent')
        self.assertEqual(classify(self.authors,[{'name':'Different Name','author_id':'B'}],self.ids,True)[0],'non_independent')

    def test_initial_match_is_definitive(self):
        self.assertEqual(classify(self.authors,[{'name':'X Zhang'}],self.ids,False)[0],'non_independent')
        self.assertEqual(classify(self.authors,[{'name':'B Daniels'}],self.ids,False)[0],'independent')

    def test_ellipsis_only_in_author_part(self):
        _,cut=scholar_record({'title':'Paper','publication_info':{'summary':'B Daniels - … Conference …, 2025'}})
        self.assertFalse(cut)
        authors,cut=scholar_record({'title':'Paper','publication_info':{'summary':'B Daniels… - Conference, 2025'}})
        self.assertEqual(classify(self.authors,authors,self.ids,cut)[0],'unresolved')

    def test_per_paper_rule(self):
        citing=[{'name':'Z Zuo'}]
        self.assertEqual(classify(self.authors,citing,self.ids,False)[0],'independent')
        self.assertEqual(classify(['Tian Xie','Zhiqun Zuo'],citing,self.ids,False)[0],'non_independent')

    def test_unicode_names_do_not_collapse(self):
        self.assertEqual(classify(['张三'],[{'name':'李四'}],{},False)[0],'independent')
        self.assertEqual(classify(['张三'],[{'name':'张三'}],{},False)[0],'non_independent')

    def test_missing_authors_is_explicit_failure(self):
        with self.assertRaises(ValueError):
            scholar_record({'title':'Missing'})

    def test_empty_profile_has_valid_two_deliverables(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'profile_OTHER.json').write_text('{}')
            data={'author_id':'OTHER','author':{'name':'Other Person'},'snapshot_date':'2026-10-11','papers':[]}
            r=generate(data,root,root/'output',True)
            self.assertEqual(r['retrieved'],0)
            self.assertEqual(sorted(p.name for p in (root/'output').iterdir()),['independent_citations.csv','report.html'])
            with (root/'output/independent_citations.csv').open(encoding='utf-8-sig') as f:
                self.assertEqual(list(csv.DictReader(f)),[])

    def test_secret_sanitization(self):
        data=safe_data({'api_key':'secret','search_parameters':{'api_key':'secret'},'url':'https://example.org/?api_key=secret&q=a'})
        self.assertNotIn('secret',str(data))

    def test_profile_and_citation_pagination_for_another_author(self):
        def paper(i):
            return {'title':'Source '+str(i),'citation_id':'OTHER:'+str(i),
                    'link':'https://scholar.google.com/citations?user=OTHER',
                    'cited_by':{'value':21 if i==1 else 0}}
        def fake_get(api,path,**params):
            if params.get('view_op')=='view_citation':
                cid=params['citation_id']
                return {'citation':{'authors':'Other Person, Alex Smith',
                                    'total_citations':{'cited_by':{'cites_id':'CITES'}} if cid=='OTHER:1' else {}}}
            if params['engine']=='google_scholar_author':
                if params.get('start')==100:
                    return {'articles':[paper(2)]}
                return {'author':{'name':'Other Person'},'articles':[paper(1)],'serpapi_pagination':{'next':'page2'}}
            start=params['start']
            items=[{'result_id':str(i),'title':'Citing '+str(i)} for i in range(start,min(start+20,21))]
            return {'organic_results':items,'serpapi_pagination':{'next':'page2'} if start==0 else None}
        with tempfile.TemporaryDirectory() as folder, patch.object(Scholar,'get',fake_get):
            data=collect('OTHER',folder)
            self.assertEqual(len(data['papers']),2)
            self.assertEqual(data['papers'][0]['retrieved'],21)
            self.assertEqual(data['papers'][1]['retrieved'],0)


if __name__=='__main__':
    unittest.main()
