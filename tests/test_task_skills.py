import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.task_skills import SkillLibrary


def document(text='Use /svc/weather.py --city CITY', generation='task1'):
    return {'path': '/brief/README.txt', 'text': text, 'generation': generation,
            'verified': True, 'exit_code': 0, 'truncated': False}


def query(command='python3 /svc/weather.py --city Beijing', generation='task1'):
    return {'command': command, 'generation': generation, 'verified': True,
            'exit_code': 0, 'truncated': False, 'output': 'secret-answer=23'}


class QuerySkillTests(unittest.TestCase):
    def test_candidate_requires_fresh_document_and_never_caches_answers(self):
        lib = SkillLibrary('team-one')
        identity = lib.learn(document(), query())
        candidates = lib.candidates('阅读 /brief 中的文档，查询上海天气')
        self.assertEqual(candidates[0]['id'], identity)
        self.assertNotIn('template', candidates[0])
        fresh = document(generation='task2')
        hint = lib.hint(identity, fresh)
        self.assertEqual(hint['template'], ['python3', '/svc/weather.py', '--city', '<current-value>'])
        saved = json.dumps(lib.dump())
        for value in ('Beijing', 'secret-answer', '23'):
            # Hashes may incidentally contain digits; inspect the structured template for values.
            self.assertNotIn(value, json.dumps(hint['template']))
        self.assertNotIn('secret-answer', saved)
        self.assertNotIn('Beijing', saved)
        self.assertEqual(lib.candidates('阅读 /brief2 文档'), [])

    def test_document_version_or_unverified_read_prevents_reuse(self):
        lib = SkillLibrary('one'); identity = lib.learn(document(), query())
        self.assertIsNone(lib.hint(identity, document('Use /v2/query.py --place CITY', 'task2')))
        self.assertIsNone(lib.hint(identity, {**document(), 'verified': False}))
        self.assertIsNone(lib.hint(identity, {**document(), 'truncated': True}))
        self.assertIsNone(lib.hint(identity, document('Use /svc/weather.py --city CITY\n')))

    def test_only_same_generation_successful_observed_workflows_are_learned(self):
        for doc, call in (({**document(), 'verified': False}, query()),
                          ({**document(), 'exit_code': 1}, query()),
                          ({**document(), 'truncated': True}, query()),
                          (document(), query(generation='another-task')),
                          (document(), {**query(), 'exit_code': True})):
            self.assertIsNone(SkillLibrary('one').learn(doc, call))

    def test_shell_composition_inline_code_and_old_argument_values_not_reused(self):
        lib = SkillLibrary('one')
        for cmd in ('python3 -c "print(23)"', 'python3 /svc/weather.py --city Beijing; pwd'):
            self.assertIsNone(lib.learn(document(), query(cmd)))
        identity = lib.learn(document(), query('python3 /svc/weather.py --city=Beijing --token secret-value'))
        hint = lib.hint(identity, document(generation='task2'))
        self.assertNotIn('Beijing', str(hint))
        self.assertNotIn('secret-value', str(lib.dump()))

    def test_json_transport_ownership_and_corruption(self):
        lib = SkillLibrary('one'); identity = lib.learn(document(), query())
        saved = json.loads(json.dumps(lib.dump()))
        restored = SkillLibrary.load(saved, owner='one')
        self.assertIsNotNone(restored.hint(identity, document(generation='task2')))
        self.assertTrue(SkillLibrary.load(saved, owner='two').degraded)
        saved['entries'][0]['template'][-1] = 'Beijing'
        self.assertTrue(SkillLibrary.load(saved, owner='one').degraded)


if __name__ == '__main__':
    unittest.main()
