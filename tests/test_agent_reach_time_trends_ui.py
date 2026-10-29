import unittest
from pathlib import Path
import agent_reach_views
class TimeTrendsUI(unittest.TestCase):
 def test_ranges_and_defaults(self):
  h=agent_reach_views.render_workspace('fixture')
  self.assertIn('name="content_range"',h)
  self.assertIn('value="180"',h); self.assertIn('value="365"',h)
  self.assertIn('value="30" selected',h)
  self.assertIn('value="0">全部',h)
  self.assertIn('90、180、365天',h)
 def test_three_source_tabs(self):
  h=agent_reach_views.render_workspace('fixture')
  for s in ['xhs','weibo','zhihu']: self.assertIn('data-trend-tab="'+s+'"',h)
  self.assertIn('data-xhs-sort',h);self.assertIn('value="multi"',h)
 def test_time_and_local_authority(self):
  j=Path('assets/agent-reach.js').read_text()
  self.assertIn('AgentReachTime.format',j);self.assertIn('AgentReachTime.publication',j)
  self.assertIn('AgentReachRanking.filterXhs',j)
  self.assertIn('content_range:item.content_range',j)
if __name__=='__main__':unittest.main()
