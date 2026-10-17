"""Fictional fixtures only; no user archive, credentials or live requests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from audit_resolver import Resolver, ieee_metadata, openreview_metadata, publisher_record_agrees


GRAPH='Example Metadata Recovery: Toward Reliable Author Lists with Structured Records'
NAMES=['Yalen Lemplate','Yeri Zample','Qalen Tample','Yavrin Wemplate','Lerin Wemplate','Taren Wexamp','Melin Zemplar','Kerin Xample','Yevrin Zample']


class Unavailable:
    def get(self,url):raise RuntimeError('Not retained')
    def json(self,url):raise RuntimeError('Not retained')


class DiscoveryTests(unittest.TestCase):
    def graph_item(self):
        return {'result_id':'G','title':GRAPH.replace('Toward ','Towards '),'link':'https://ieeexplore.ieee.org/abstract/document/12345678/',
                'publication_info':{'summary':'Y Lemplate, Y Zample, Q Tample, Y Wemplate, L Wemplate… - IEEE'}}

    def test_ieee_embedded_authors_resolve_existing_title_variant(self):
        work={'title':GRAPH,'articleId':'12345678','doi':'10.1109/EXAMPLE.2000.1234567','authors':[{'name':n} for n in NAMES]}
        class Fake(Unavailable):
            def get(self,url):return '<script>xplGlobal.document.metadata='+json.dumps(work)+';</script>'
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve(self.graph_item())
            self.assertTrue(result['complete']);self.assertEqual(result['provider'],'ieee_embedded_json')
            self.assertEqual([x['name'] for x in result['authors']],NAMES)
            self.assertEqual(result['record_match'],'explicit_ieee_record')

    def test_crossref_publisher_article_number_survives_title_prefilter(self):
        record={'title':[GRAPH],'DOI':'10.1109/example.2000.1234567',
                'link':[{'URL':'http://xplorestaging.ieee.org/ielx8/8858/12345670/12345678.pdf?arnumber=12345678'}],
                'author':[{'given':n.rsplit(' ',1)[0],'family':n.rsplit(' ',1)[1]} for n in NAMES]}
        class Fake(Unavailable):
            def json(self,url):
                if 'api.crossref.org/works?' in url:return {'message':{'items':[record]}}
                return super().json(url)
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve(self.graph_item())
            self.assertTrue(result['complete']);self.assertEqual(result['provider'],'crossref')
            self.assertEqual(result['record_match'],'explicit_ieee_record')

    def test_ieee_body_must_identify_the_requested_article(self):
        body='xplGlobal.document.metadata='+json.dumps({'articleId':'999','title':GRAPH,'authors':[{'name':n} for n in NAMES]})
        self.assertIsNone(ieee_metadata(self.graph_item()['link'],body))

    def test_title_variant_alone_is_still_insufficient(self):
        candidate={'source':'https://doi.org/10.1234/other','title':GRAPH,'authors':[{'name':n} for n in NAMES]}
        self.assertIsNotNone(Resolver.validate(self.graph_item(),candidate))
        self.assertFalse(publisher_record_agrees(self.graph_item(),candidate))

    def test_openreview_search_recovers_exact_note_after_direct_failure(self):
        original={'result_id':'C','title':'Testing Example Models with Layered Metadata Retrieval','link':'https://openreview.net/forum?id=EXACT',
                  'publication_info':{'summary':'L Jexample, G Pexample, L Qexample, S Sexample, M Mexample… - OpenReview'}}
        authors=['Loren Jexample','Gonçaro Pexample','Lurin Qexample','Steran Sexample','Miren Mexample','Joren Müxample','Swerin Jexample','Moren Wexample','Nerin Bexample']
        class Fake(Unavailable):
            def json(self,url):
                if '/notes/search?' in url:
                    return {'notes':[{'id':'OTHER','content':{'title':{'value':original['title']},'authors':{'value':authors[:-4]}}},
                                     {'id':'EXACT','content':{'title':{'value':original['title']},'authors':{'value':authors}}}]}
                return super().json(url)
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve(original)
            self.assertTrue(result['complete']);self.assertEqual(result['provider'],'openreview_search')
            self.assertEqual([a['name'] for a in result['authors']],authors)
            self.assertEqual(result['record_match'],'explicit_openreview_record')

    def test_search_never_substitutes_a_different_same_title_note(self):
        class Fake(Unavailable):
            def json(self,url):
                if '/notes/search?' in url:return {'notes':[{'id':'OTHER','content':{'title':'Paper','authors':['Alice Example']}}]}
                return super().json(url)
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve({'result_id':'C','title':'Paper','link':'https://openreview.net/forum?id=EXACT','publication_info':{'summary':'A Example… - OpenReview'}})
            self.assertFalse(result['complete'])
            self.assertTrue(any('exact original note ID' in x['reason'] for x in result['rejected_candidates']))

    def test_openreview_note_ids_are_case_sensitive(self):
        payload={'notes':[{'id':'Exact','content':{'title':'Paper','authors':['Alice Example']}}]}
        self.assertIsNone(openreview_metadata(payload,'EXACT','https://api2.openreview.net/notes/search?term=Paper'))

    def test_stable_openreview_id_handles_changed_title_without_general_fuzzy_matching(self):
        item={'title':'Old title','link':'https://openreview.net/forum?id=EXACT','publication_info':{'summary':'A Example'}}
        candidate={'title':'New title','source':item['link'],'openreview_note_id':'EXACT','authors':[{'name':'Alice Example'}]}
        self.assertIsNone(Resolver.validate(item,candidate))
        candidate['openreview_note_id']='OTHER'
        self.assertEqual(Resolver.validate(item,candidate),'Conflicting OpenReview note identifier')

    def test_shorter_search_route_can_recover_legacy_note(self):
        title='Evaluating Example Models with Structured Author Recovery'
        class Fake(Unavailable):
            def json(self,url):
                if 'api.openreview.net/notes/search?' in url and 'term=Structured+Author+Recovery&' in url:
                    return {'notes':[{'id':'EXACT','content':{'title':title,'authors':['Alice Example']}}]}
                return super().json(url)
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve({'result_id':'C','title':title,'link':'https://openreview.net/forum?id=EXACT','publication_info':{'summary':'A Example… - OpenReview'}})
            self.assertTrue(result['complete']);self.assertIn('term=Structured+Author+Recovery&',result['retrieval_url'])

    def test_crossref_prefilter_rejection_is_visible_in_diagnostics(self):
        class Fake(Unavailable):
            def json(self,url):
                if 'api.crossref.org/works?' in url:return {'message':{'items':[{'title':['Different title'],'DOI':'10.1234/other','author':[{'given':'Alice','family':'Example'}]}]}}
                return super().json(url)
        with tempfile.TemporaryDirectory() as directory:
            result=Resolver(directory,Fake()).resolve({'result_id':'C','title':'Paper','publication_info':{'summary':'A Example… - Journal'}})
            self.assertFalse(result['complete'])
            self.assertTrue(any(x.get('candidate_title')=='Different title' and x.get('stage')=='matching' for x in result['rejected_candidates']))

    def annotation_case(self,directory):
        path=Path(directory)/'primary.txt';path.write_text('Paper\nAlice Example a\na University\n')
        item={'result_id':'C','title':'Paper','link':'https://www.researchgate.net/profile/Person/publication/123/Paper.pdf',
              'publication_info':{'summary':'A Examplea… - Repository','authors':[{'name':'A Examplea','author_id':'AUTHOR'}]}}
        candidate={'provider':'retained_text_byline','source':'https://www.researchgate.net/publication/123_Paper','title':'Paper','authors':[{'name':'Alice Example'}],
                   'byline_verified':True,'evidence':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
        return item,candidate

    def annotation_correction(self):
        return {'verified':True,'review_note':'Checked separate a affiliation marker on original byline.',
                'entries':[{'scholar_name':'A Examplea','full_author_name':'Alice Example','affiliation_marker':'a'}]}

    def test_affiliation_letter_is_removed_only_with_checked_literal_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            item,candidate=self.annotation_case(directory)
            self.assertEqual(Resolver.validate(item,candidate),'Full metadata does not cover visible Scholar authors')
            candidate['scholar_name_corrections']=self.annotation_correction()
            self.assertIsNone(Resolver.validate(item,candidate))
            candidate['scholar_name_corrections']['entries'][0]['affiliation_marker']='b'
            self.assertIsNotNone(Resolver.validate(item,candidate))

    def test_annotation_correction_cannot_transfer_to_another_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            item,candidate=self.annotation_case(directory);candidate['scholar_name_corrections']=self.annotation_correction()
            candidate['source']='https://www.researchgate.net/publication/999_Paper'
            self.assertIsNotNone(Resolver.validate(item,candidate))

    def test_annotation_cleanup_preserves_the_explicit_scholar_id(self):
        with tempfile.TemporaryDirectory() as directory:
            item,candidate=self.annotation_case(directory);candidate['scholar_name_corrections']=self.annotation_correction()
            result=Resolver(Path(directory)/'resolved',Unavailable(),supplements={'C':[candidate]}).resolve(item)
            self.assertTrue(result['complete']);self.assertEqual(result['authors'][0]['author_id'],'AUTHOR')


if __name__=='__main__':unittest.main()
