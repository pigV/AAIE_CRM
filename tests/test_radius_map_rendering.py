"""Run real radius map renderers with synthetic schools, without importing CRM."""
import html
import json
import math
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tests.test_school_map_tiles import handler_methods
from tests import test_school_map_tiles as map_tests

ROOT = Path(__file__).resolve().parents[1]
CENTER = dict(geocode_status='Resolved', state='NSW', latitude=-33.8, longitude=151.18,
              display_label='搜索中心：Chatswood Public School 学校位置', cbd_distance_km=8.4,
              cbd_distance_text='距 Sydney CBD 约 8.4 km')
SCHOOLS = [dict(id=i, school_name=f'School {i} with a long name <safe>', latitude=-33.8+i*.001,
                longitude=151.18+i*.001, distance_km=i*.15, distance_text=f'{i*.15:.2f} km',
                direction_label='NE', coordinate_source_label='School exact coordinates',
                sector_key='public', international_year_level_min='Year 7',
                international_year_level_max='Year 12') for i in range(1,51)]


def renderer():
    names=('school_radius_project_point','school_radius_map_svg','school_radius_leaflet_map_payload',
           'school_radius_leaflet_map_html','school_radius_map_visual_html','school_radius_result_rows_html')
    ns=handler_methods(*names)
    ns.update(esc=lambda x: html.escape(str(x),quote=True), json=json, math=math,
              SCHOOL_RADIUS_VALUES_KM=[5,10,15,20],
              SCHOOL_CBD_COORDS={'NSW':dict(label='Sydney CBD',latitude=-33.8688,longitude=151.2093)})
    cls=type('Renderer',(),{name:ns[name] for name in names})
    cls.school_is_private_school=lambda self,s: s.get('sector_key')=='private'
    cls.school_radius_row_effective_sector=lambda self,s: s.get('sector_key','')
    return cls()


class RootTags(HTMLParser):
    def __init__(self): super().__init__(); self.depth=0; self.roots=[]
    def handle_starttag(self,tag,attrs):
        if self.depth==0:self.roots.append((tag,dict(attrs)))
        if tag not in ('link','img','br','input','hr','meta'):self.depth+=1
    def handle_endtag(self,tag): self.depth-=1


class RadiusMapRenderingTests(unittest.TestCase):
    def test_radius_page_allows_only_osm_images_and_local_scripts(self):
        headers=map_tests.SchoolMapTileTests().headers('<div class="school-radius-search-page" data-school-radius-leaflet></div>')
        policy=headers['Content-Security-Policy']
        self.assertIn("img-src 'self' data: blob: https://tile.openstreetmap.org;",policy)
        self.assertIn("script-src 'self' 'unsafe-inline';",policy)
        self.assertNotIn('unpkg.com',policy)
        self.assertNotIn('https:',map_tests.SchoolMapTileTests().headers('<p>Other page</p>')['Content-Security-Policy'])

    def test_map_uses_vendored_leaflet(self):
        content=renderer().school_radius_leaflet_map_html(CENTER,SCHOOLS)
        self.assertIn('/assets/vendor/leaflet-1.9.4/leaflet.js',content)
        self.assertIn('/assets/vendor/leaflet-1.9.4/leaflet.css',content)
        self.assertNotIn('unpkg.com',content)

    def test_filtered_schools_reach_map_and_keep_original_order(self):
        data=renderer().school_radius_leaflet_map_payload(CENTER,SCHOOLS,'3',20)
        self.assertEqual(len(data['schools']),50)
        self.assertEqual([s['number'] for s in data['schools']],list(range(1,51)))
        self.assertEqual(data['schools'][2]['name'],SCHOOLS[2]['school_name'])
        self.assertEqual(data['schools'][2]['latitude'],SCHOOLS[2]['latitude'])
        self.assertTrue(data['schools'][2]['selected'])
        self.assertEqual(data['schools'][2]['sector'],'public')
        self.assertEqual(data['circle_radii_km'],[])
        self.assertEqual(data['cbd']['label'],'Sydney CBD')
        self.assertEqual(renderer().school_radius_leaflet_map_payload(CENTER,[])['schools'],[])

    def test_map_and_fallback_are_one_grid_item(self):
        content=renderer().school_radius_map_visual_html(dict(school_radius_leaflet_map_html='<div data-school-radius-leaflet></div>',school_radius_map_svg='<svg></svg>'))
        parsed=RootTags();parsed.feed(content)
        self.assertEqual(len(parsed.roots),1)
        self.assertIn('school-radius-map-column',parsed.roots[0][1].get('class',''))
        self.assertIn('<details',content)

    def test_static_map_uses_numbers_and_accessible_names(self):
        import xml.etree.ElementTree as ET
        svg=ET.fromstring(renderer().school_radius_map_svg(CENTER,SCHOOLS))
        groups=[g for g in svg.iter('g') if 'school-map-school-node' in g.get('class','')]
        self.assertEqual(len(groups),50)
        for i,g in enumerate(groups,1):
            self.assertEqual(g.find('text').text,str(i))
            self.assertIn(f'School {i} with a long name <safe>',g.find('title').text)
        cards=renderer().school_radius_result_rows_html({'schools_with_distance':SCHOOLS})
        self.assertIn('school-radius-result-number',cards)
        self.assertIn('>50</span>',cards)
        self.assertIn('&lt;safe&gt;',cards)


    def test_vendored_bundle_is_present_and_not_ignored(self):
        import subprocess
        for name in ['leaflet.js','leaflet.css','LICENSE','SOURCES.json','images/layers.png','images/layers-2x.png','images/marker-icon.png','images/marker-icon-2x.png','images/marker-shadow.png']:
            path=ROOT/'assets/vendor/leaflet-1.9.4'/name
            self.assertTrue(path.is_file(),name)
            self.assertEqual(subprocess.run(['git','check-ignore','--quiet',str(path)],cwd=ROOT).returncode,1,name)

    def test_fifty_coincident_school_labels_do_not_overlap(self):
        import xml.etree.ElementTree as ET
        schools=[dict(s,latitude=CENTER['latitude'],longitude=CENTER['longitude']) for s in SCHOOLS]
        svg=ET.fromstring(renderer().school_radius_map_svg(CENTER,schools))
        labels=[(float(t.get('x')),float(t.get('y'))) for t in svg.iter('text') if t.get('class')=='school-map-school-number']
        self.assertEqual(len(labels),50)
        for i,(x,y) in enumerate(labels):
            for a,b in labels[i+1:]:
                self.assertGreaterEqual(math.hypot(x-a,y-b),20)

    def test_number_labels_leave_cbd_text_clear(self):
        import xml.etree.ElementTree as ET
        school=dict(SCHOOLS[0],latitude=-33.8678,longitude=151.2093)
        svg=ET.fromstring(renderer().school_radius_map_svg(CENTER,[school]))
        cbd=next(g for g in svg.iter('g') if g.get('class')=='school-map-cbd').find('text')
        label=next(t for t in svg.iter('text') if t.get('class')=='school-map-school-number')
        x,y=float(label.get('x')),float(label.get('y'))
        left,top=float(cbd.get('x'))-2,float(cbd.get('y'))-14
        right,bottom=left+88,top+26
        self.assertFalse(x-12<right and x+12>left and y-12<bottom and y+12>top)


if __name__=='__main__':unittest.main()
