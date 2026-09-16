"""Independent expectations for real-field budgeting and distant shop delivery."""
import json
import unittest
from copy import deepcopy
from test_economy import state_with
from agent import brain, upgrade_itinerary as upgrade, turnactions, task_progress, task_tools
from agent.protocol import Turn, Pos, distance, WEAPON_UPGRADE_VOUCHER_1 as WV, STATION_UPGRADE_VOUCHER_1 as SV, WALL_UPGRADE_VOUCHER_1 as AV
from agent.task_answer_contract import derive,validate
from agent.task_journal import TaskJournal


def unit(uid,kind,x,y,level=1,health=1000):
    return dict(id=uid,roleType=kind,pos={'x':x,'y':y},health=health,level=level,
                backpack=[],backPackCapability=100,cooldown=0)


def board(side='challenger'):
    roles=[unit(1,'station',3,4,health=1500),unit(2,'rocket',6,3),unit(3,'rocket',6,5),
           unit(4,'railgun',3,7),unit(11,'worker',7,4,health=220),unit(12,'worker',4,6,health=220),
           unit(13,'pioneer',2,8,health=200)]
    state=state_with(gold=200,roles=roles,zones=[{'pos':{'x':15,'y':5},'neutralType':'weaponShop'}],round_no=13)
    state['weaponShopList']=[{'name':WV,'price':100},{'name':SV,'price':100},{'name':AV,'price':20}]
    state['teamOur']['type']=side
    state.update(phaseTask='',llmResp='',lastCmdResult='',errors=[],lastRoundRoleActionResults={})
    if side=='defender':
        # Mirror x, including the top-left base anchor for its two-cell footprint.
        for role in state['teamOur']['roles']:role['pos']['x']=40-role['pos']['x']-(role['roleType']=='station')
        for zone in state['mapInfo']['zones']:zone['pos']['x']=40-zone['pos']['x']
    return state


class UpgradeTests(unittest.TestCase):
    def test_far_shop_real_buy_return_use_both_sides(self):
        for side in ('challenger','defender'):
            state=board(side);before=deepcopy(state);buys=uses=0;phases=[]
            for _ in range(40):
                turn=Turn.load(state);proposal,report=upgrade.plan(turn,state,{})
                self.assertIsNotNone(proposal,report)
                owner,command=proposal;phases.append(report['phase'])
                role=next(r for r in state['teamOur']['roles'] if r['id']==owner)
                if command['action']=='move':
                    pos=Pos.load(command['targetPos'][0]);current=next(r for r in turn.workers() if r.unit_id==owner)
                    self.assertEqual(distance(current.pos,pos),1)
                    self.assertTrue(turn.land(pos));self.assertNotIn(pos,turn.blocked(current))
                    role['pos']=pos.dump()
                elif command['action']=='buy':
                    self.assertEqual(command['name'],WV)
                    turnactions.buy(state,role,item=command['name'],amount=1);buys+=1
                elif command['action']=='use':
                    target=Pos.load(command['targetPos'][0])
                    turnactions.use(state,role,item=command['name'],target=target,effects={});uses+=1
                    break
                state=json.loads(json.dumps(state));state['roundNo']+=1
            self.assertEqual((buys,uses),(1,1))
            self.assertEqual(state['teamOur']['goldNum'],before['teamOur']['goldNum']-100)
            self.assertIn('return_with_voucher',phases)
            self.assertEqual(sum(r['level']==2 for r in state['teamOur']['roles']),1)
            self.assertTrue(any(r['level']==2 and r['health']==1500 for r in state['teamOur']['roles']))

    def test_base_and_wall_vouchers_are_eligible_and_carried_item_needs_no_shop(self):
        for kind,voucher in [('station',SV),('wall',AV)]:
            state=board();roles=state['teamOur']['roles']
            for r in roles:
                if r['roleType'] in ('rocket','railgun','station'):r['level']=3
            target=roles[0]
            if kind=='station':target['level']=1
            else:
                target=unit(30,'wall',3,6,health=300);roles.append(target)
            worker=next(r for r in roles if r['id']==11)
            worker['pos']={'x':target['pos']['x']-1,'y':target['pos']['y']}
            worker['backpack']=[voucher];state['mapInfo']['zones']=[]
            proposal,report=upgrade.plan(Turn.load(state),state,{})
            self.assertEqual(proposal[1]['action'],'use');self.assertEqual(proposal[1]['name'],voucher)

    def test_guard_budget_time_full_bag_and_vendor_only(self):
        for change,reason in [
            (lambda s:s.update(roundNo=71),'night_defence'),
            (lambda s:s['mapInfo']['zones'][0].update(neutralType='vendor'),'weapon_shop_not_observed'),
            (lambda s:s.update(roundNo=55),'trip_unreachable_full_or_too_late'),
            (lambda s:s['teamOur']['roles'][5].update(health=0),'keep_last_worker_home'),
            (lambda s:s['teamOur'].update(goldNum=99),'no_affordable_upgrade')]:
            state=board();change(state)
            proposal,report=upgrade.plan(Turn.load(state),state,{})
            self.assertIsNone(proposal);self.assertEqual(report['reason'],reason)
        state=board();state['teamOur']['goldNum']=150
        proposal,report=upgrade.plan(Turn.load(state),state,{13:{'action':'buy','name':WV,'num':1}})
        self.assertIsNone(proposal);self.assertEqual(report['gold_available'],50)
        state=board()
        for r in state['teamOur']['roles']:
            if r['roleType']=='worker':r['backpack']=['stone']*100
        self.assertIsNone(upgrade.plan(Turn.load(state),state,{})[0])

    def test_current_day_plan_does_not_override_shopping_with_wall_work(self):
        state=board();commands={}
        brain._day(Turn.load(state),commands,state)
        report=brain._UPGRADE_REPORT.get()
        self.assertEqual(report['phase'],'to_shop')
        self.assertEqual(commands[report['worker']]['action'],'move')
        state['mapInfo']['zones'][0]['neutralType']='vendor'
        commands={};brain._day(Turn.load(state),commands,state)
        self.assertEqual(brain._UPGRADE_REPORT.get()['reason'],'weapon_shop_not_observed')


class ReadinessTests(unittest.TestCase):
    def test_mixed_default_and_idle_worker_fills_ready_adjacent_weapon(self):
        self.assertEqual(brain.TOWER_LOADOUT,('rocket','rocket','railgun'))
        state=board();state['roundNo']=71
        roles=state['teamOur']['roles'];roles[1]['cooldown']=2;roles[2]['cooldown']=2
        roles[3]['pos']={'x':8,'y':4}
        state['robot']={'roles':[{'id':900,'pos':{'x':10,'y':4},'health':100,'roleType':'smallRobot','targetTeam':'challenger'}]}
        turn=Turn.load(state);commands={}
        brain._fill_ready_weapons(turn,commands,set())
        self.assertEqual(commands[4]['action'],'attack')
        self.assertEqual(commands[4]['controllerId'],'11')
        self.assertNotIn(2,commands)
        commands={11:{'action':'use','name':'Medicine'}}
        brain._fill_ready_weapons(turn,commands,{12,13})
        self.assertNotIn(4,commands)
        report=brain._weapon_readiness(turn,commands)
        self.assertEqual(next(r for r in report if r['weapon']==2)['reason'],'cooldown')


class Issue27Tests(unittest.TestCase):
    def test_goldNum_is_authoritative_and_missing_is_not_zero(self):
        self.assertEqual(task_progress.stats({'goldNum':145,'gold':999,'totalScore':3}),{'gold':145,'totalScore':3})
        self.assertIsNone(task_progress.stats({'gold':999})['gold'])
        journal=TaskJournal()
        a={'roundNo':1,'phaseTask':'task','teamOur':{'goldNum':10,'totalScore':0}}
        journal.observe(a,{'roleCommandMap':{'11':{'action':'submitAnswer','taskAnswer':'{}'}}})
        b={'roundNo':2,'phaseTask':'','teamOur':{'goldNum':90,'totalScore':80}}
        rows=journal.observe(b,{})
        result=json.loads(next(x for x in rows if x['kind']=='task_submission_feedback')['content']['text'])
        self.assertEqual(result['stats_delta'],{'gold':80,'totalScore':80})
        self.assertFalse(result['official_success_confirmed'])

    def test_markdown_submission_section_uses_structure_not_example_values(self):
        text='## 任务要求\n查询当前城市。\n## 提交形式\n以字符串提交，例如：\n```json\n{"city":"样例","total_count":0,"types":["a"]}\n```\n## 提示\n其他信息'
        contract=derive(text,'observed-task')
        self.assertEqual(contract['fields'],{'city':'str','total_count':'int','types':'list'})
        self.assertFalse(validate('15',contract,[])[0])
        self.assertTrue(validate('{"city":"新城市","total_count":7,"types":[]}',contract,[])[0])
        self.assertIsNone(derive('## API响应示例\n```json\n{"value":3}\n```','task'))

    def test_standalone_check_conversion_is_narrow(self):
        for cmd in ('/tmp/ws/check','cd /tmp/ws && ./check',"cd '/tmp/task work' && ./check"):
            self.assertTrue(task_tools.standalone_check(cmd).startswith('# task-check/1\n'))
        for cmd in ('./check','cat /tmp/check','cd relative && ./check','cd /tmp/ws && sed -i x check && ./check','/tmp/ws/check > /tmp/out','/tmp/$(id)/check'):
            self.assertIsNone(task_tools.standalone_check(cmd),cmd)


if __name__=='__main__':unittest.main()
