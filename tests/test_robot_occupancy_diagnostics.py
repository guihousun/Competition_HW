"""Hand-authored public frames; no simulator, strategy or inferred collision rule."""
from copy import deepcopy
import json
import unittest

from test_diagnostics import diagnostics
from agent.console_digest import ConsoleDigest
from agent import robot_occupancy as occupancy


def robot(rid, x=5, y=5, health=40):
    return {'id':rid, 'pos':{'x':x,'y':y}, 'health':health,
            'roleType':'smallRobot', 'targetTeam':'defender'}


def request(rows, round_no=71, side='challenger'):
    return {'roundNo':round_no, 'mapInfo':{'width':41,'height':32},
            'teamOur':{'type':side,'roles':[]}, 'robot':{'roles':rows}}


def lines(digest, payload):
    summary = diagnostics.build_summary(payload, {'roleCommandMap':{}})
    return [json.loads(s.split(' ',1)[1]) for s in digest.observe(summary, stream='test:base')
            if s.startswith('robot_occupancy ')]


class RobotOccupancyTests(unittest.TestCase):
    def test_overlap_counts_and_exact_evidence_do_not_mutate_request(self):
        payload = request([robot(9),robot(12),robot(30,6,5),robot(40,health=0)])
        before = deepcopy(payload)
        result = occupancy.summarize(payload)
        self.assertEqual((result['valid_live_count'],result['unique_cell_count'],
                          result['overlap_group_count'],result['dead_entries']), (3,2,1,1))
        self.assertEqual(result['groups'][0]['pos'], {'x':5,'y':5})
        self.assertEqual(result['groups'][0]['robots'], [
            {'id':9,'health':40,'roleType':'smallRobot','targetTeam':'defender'},
            {'id':12,'health':40,'roleType':'smallRobot','targetTeam':'defender'}])
        self.assertEqual(payload,before)

    def test_dead_same_cell_is_not_overlap(self):
        r=occupancy.summarize(request([robot(1),robot(2,health=0)]))
        self.assertEqual((r['valid_live_count'],r['overlap_group_count'],r['groups']), (1,0,[]))

    def test_duplicate_normalized_ids_are_excluded_even_with_bad_coordinate(self):
        bad=robot(3);bad['pos']={}
        r=occupancy.summarize(request([robot(7),robot('7'),robot(3),bad,robot(True),robot(20)]))
        self.assertEqual(r['duplicate_id_entries'],4)
        self.assertEqual(r['invalid_entries'],2)
        self.assertEqual((r['valid_live_count'],r['overlap_group_count']), (1,0))

    def test_missing_and_placeholder_coordinates_are_invalid(self):
        no_x=robot(1);del no_x['pos']['x']
        payload=request([no_x,robot(2,-1,-1),robot(3,-1,-1),robot(4,41,2),robot(5,2,32)])
        r=occupancy.summarize(payload)
        self.assertEqual((r['invalid_entries'],r['valid_live_count'],r['overlap_group_count']), (5,0,0))
        payload['mapInfo']={'width':5,'height':5}
        r=occupancy.summarize(payload)
        self.assertEqual(r['valid_live_count'],0)

    def test_missing_or_invalid_map_is_unknown_not_zero_overlap(self):
        for map_info in [None,{}, {'width':True,'height':32},{'width':41,'height':0}]:
            payload=request([robot(1),robot(2)])
            payload['mapInfo']=map_info
            r=occupancy.summarize(payload)
            self.assertFalse(r['available'])
            self.assertEqual(r['data_insufficient'],'invalid_map_dimensions')
            self.assertNotIn('overlap_group_count',r)

    def test_repeated_moving_and_damaged_same_members_are_quiet(self):
        digest=ConsoleDigest()
        payload=request([robot(1),robot(2)])
        first=lines(digest,payload)
        self.assertEqual(len(first),1)
        self.assertEqual(first[0]['side'],'challenger')
        self.assertEqual(first[0]['round'],71)
        self.assertEqual(lines(digest,payload),[])
        payload['roundNo']=72
        for r in payload['robot']['roles']:
            r['pos']['x']=6;r['health']=30
        self.assertEqual(lines(digest,payload),[])
        payload['roundNo']=201
        self.assertEqual(len(lines(digest,payload)),1)

    def test_members_merge_split_quality_change_and_new_game_emit(self):
        digest=ConsoleDigest()
        payload=request([robot(1),robot(2)],80)
        self.assertEqual(len(lines(digest,payload)),1)
        payload['roundNo']=81;payload['robot']['roles'].append(robot(3))
        self.assertEqual(len(lines(digest,payload)),1)
        payload['roundNo']=82;payload['robot']['roles'][0]['pos']['x']=8
        self.assertEqual(len(lines(digest,payload)),1)
        payload['roundNo']=83;payload['robot']['roles'].append({'id':8})
        self.assertEqual(len(lines(digest,payload)),1)
        payload['roundNo']=1
        self.assertEqual(len(lines(digest,payload)),1)

    def test_no_overlap_only_first_observed_frame_of_each_night(self):
        digest=ConsoleDigest()
        for n,expected in [(1,0),(70,0),(75,1),(76,0),(131,0),(205,1)]:
            result=lines(digest,request([robot(1),robot(2,8,8)],n))
            self.assertEqual(len(result),expected)
            if result:self.assertEqual(result[0]['groups'],[])

    def test_bounded_input_details_and_output_size(self):
        rows=[robot(i,x=(i//8)%41,y=(i//328)%32) for i in range(occupancy.MAX_ROWS+17)]
        for row in rows:row.update(roleType='💥'*1000,targetTeam='💥'*1000)
        payload=request(rows)
        r=occupancy.summarize(payload)
        self.assertEqual(r['scanned_entries'],occupancy.MAX_ROWS)
        self.assertEqual(r['input_truncated'],17)
        self.assertGreater(r['groups_truncated'],0)
        self.assertEqual(len(r['groups']),occupancy.MAX_GROUPS)
        self.assertTrue(all(len(g['robots'])<=occupancy.MAX_MEMBERS for g in r['groups']))
        result=lines(ConsoleDigest(),payload)[0]
        self.assertIn('valid_live_count',result)
        self.assertLess(len(('robot_occupancy '+json.dumps(result,ensure_ascii=True)).encode()),occupancy.MAX_LINE_BYTES)

    def test_prompt_and_api_fields_are_never_copied(self):
        payload=request([robot(1),robot(2)])
        payload.update(llmResp='SECRET_REPLY',phaseTask='SECRET_TASK',apiKey='SECRET_KEY')
        text=json.dumps(lines(ConsoleDigest(),payload))
        self.assertNotIn('SECRET',text)
        self.assertNotIn('prompt',text)
