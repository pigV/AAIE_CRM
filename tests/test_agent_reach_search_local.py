"""CRM result DTO/local-control boundary. No backend requests."""
import unittest
from pathlib import Path
from agent_reach_gateway import _project,GatewayError
from agent_reach_views import render_workspace
ROOT=Path(__file__).resolve().parents[1]
def result(**updates):
 value={'source':'opencli','stable_identity':'note:abc','note_id':'abc','title':'标题','author':'作者','likes':0,'published_at':None,'last_seen':'2026-09-08T01:00:00+00:00','keywords':['留学'],'latest_search_rank':3,'latest_search_run_at':'2026-09-08T01:00:00+00:00','latest_search_run_id':'run-1','keyword_search_ranks':[{'keyword':'留学','search_rank':3,'run_started_at':'2026-09-08T01:00:00+00:00','run_id':'run-1'}]};value.update(updates);return value
class SearchLocalContract(unittest.TestCase):
 def test_safe_fields_reach_local_ranking(self):
  row=result();projected=_project('search_results',{'items':[row]})['items'][0]
  for key in ['stable_identity','source','keywords','last_seen','latest_search_rank','latest_search_run_at','latest_search_run_id','keyword_search_ranks']:
   self.assertEqual(projected[key],row[key])
  self.assertIsNone(projected['published_at']);self.assertEqual(projected['likes'],0)
 def test_rank_metadata_does_not_leak_private_values(self):
  for changes in [{'keywords':['token=secret']},{'latest_search_run_id':'https://example.invalid/?token=secret'},{'latest_search_rank':True},{'keyword_search_ranks':[{'keyword':'留学','search_rank':2,'run_id':'run-1','run_started_at':'private://secret'}]}]:
   with self.subTest(changes=changes),self.assertRaises(GatewayError):_project('search_results',{'items':[result(**changes)]})
 def test_missing_metadata_stays_unknown(self):
  projected=_project('search_results',{'items':[{'source':'opencli','title':'旧结果'}]})['items'][0]
  self.assertIsNone(projected['latest_search_rank']);self.assertIsNone(projected['last_seen']);self.assertEqual(projected['keywords'],[]);self.assertEqual(projected['keyword_search_ranks'],[])
 def test_bounded_scope_metadata_is_preserved_and_validated(self):
  payload={"items":[result()],"total_count":123,"returned_count":1,"limit":100,"has_more":True,"scope":"latest_saved_results"}
  projected=_project("search_results",payload)
  for key in ["total_count","returned_count","limit","has_more","scope"]:self.assertEqual(projected[key],payload[key])
  for changes in [{"returned_count":2},{"has_more":False},{"limit":200},{"scope":"all_history"},{"total_count":0}]:
   with self.assertRaises(GatewayError):_project("search_results",{**payload,**changes})
 def test_local_controls_and_explicit_loaded_scope(self):
  html=render_workspace('synthetic')
  for value in ['综合排序','最新发布','点赞最多','爆款潜力','最近7天','最近30天','最近3个月','已加载','最多 100 条','作者','关键词','发布日期','点赞数','原始搜索名次','最近采集','立即运行','开始时间','完成时间','agent-reach-ranking.js']:
   self.assertIn(value,html)
  self.assertLess(html.index('agent-reach-ranking.js'),html.index('src="/assets/agent-reach.js"'))
 def test_local_change_handlers_have_no_fetch(self):
  js=(ROOT/'assets/agent-reach.js').read_text()
  self.assertIn('AgentReachRanking.prepare',js);self.assertIn('AgentReachRanking.select',js)
  local=js.split('function applySearchView()',1)[1].split('async function read(',1)[0]
  self.assertNotIn('request(',local);self.assertNotIn('fetch(',local)
