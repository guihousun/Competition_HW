"""Public news and cross-day treasure reasoning using the shared LLM router.

R01/R06/R07; no simulator-private facts, future publications or model execution.
Model interpretations remain labelled inferences with verbatim source evidence.
"""
from copy import deepcopy
import hashlib
import json
import re

from .world_memory import WorldMemory
from .task_agent import _json_unique
from .treasure import notes_from_news, _RUMOUR

SCHEMA = "competition-world-agent/2"
MAX_STEPS = 8  # engineering cap; the shared ordinary daily limit remains three
DRAFT_LIMIT = 5000
OWNERS = ("news", "treasure")
SOURCE_LIMIT = 12
TEXT_LIMIT = 3000


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exact_notes(text, width=41, height=32, round_no=1):
    """Only an entire supported explicit note can bypass model interpretation."""
    match = _RUMOUR.search(text)
    if (match is None or len(text) > TEXT_LIMIT
            or text[:match.start()] not in ("", "民间传闻（本地夹具）：") or text[match.end():].strip()):
        return None
    parsed = notes_from_news({"worldNews": {"folkLegends": text}}, round_no)
    site = parsed["site"]
    if (not 0 <= site["x"] < width or not 0 <= site["y"] < height
            or not 1 <= parsed["opensAt"] <= parsed["closesAt"] <= 1300
            or not 1 <= len(parsed["items"]) <= 40):
        return None
    return parsed


class WorldAgent:
    def __init__(self):
        self.sources = {owner: [] for owner in OWNERS}
        self.links = {owner: None for owner in OWNERS}
        self.attempts = {owner: {"version": "", "emitted": 0} for owner in OWNERS}
        self.resolved = {owner: "" for owner in OWNERS}
        self.status = {owner: "idle" for owner in OWNERS}
        self.news_events = []
        self.hypothesis = None
        self.direct = None
        self.feedback = []
        self.last_attempt = None
        self.taken = False
        self.degraded = False
        self.memories = {owner: WorldMemory(owner) for owner in OWNERS}
        self.focus = {owner: None for owner in OWNERS}
        self.drafts = {owner: None for owner in OWNERS}
        self.failures = {owner: 0 for owner in OWNERS}

    def version(self, owner):
        inputs = [(r["id"], r["firstRound"]) for r in self.sources[owner]]
        if owner == "treasure":
            inputs.append(self.feedback)
        return sha(json.dumps(inputs, ensure_ascii=False, sort_keys=True))[:24]

    def observe(self, payload, router):
        if self.degraded:
            return
        round_no = int(payload.get("roundNo") or 1)
        news = payload.get("worldNews") or {}
        for owner, field in (("news", "officialNews"), ("treasure", "folkLegends")):
            text = news.get(field) if isinstance(news, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            identity = self.memories[owner].observe(text, round_no)
            if identity is None:
                continue
            if not any(r["id"] == identity for r in self.sources[owner]):
                self.focus[owner] = None
                # A new publication extends a topic; it is not a new task. Keep
                # the cited constraints learned on earlier days.
                self.sources[owner].append({"id": identity, "firstRound": self.memories[owner].origins[identity]['first_round'],
                                           "text": text[:TEXT_LIMIT], "truncated": len(text) > TEXT_LIMIT})
                self.sources[owner] = self.sources[owner][-SOURCE_LIMIT:]
                if owner == "treasure":
                    info = payload.get("mapInfo") or {}
                    self.direct = exact_notes(text, info.get("width", 41), info.get("height", 32), round_no)
        code = payload.get("lastSummonTreasureResult", 0)
        if type(code) is int and code in (1, 4):
            self.taken = True
        attempt = self.last_attempt
        if (attempt and not attempt.get("received") and type(code) is int and code in (1, 2, 3, 4)
                and round_no == attempt["round"] + 1):
            attempt["received"] = True
            self.feedback = (self.feedback + [{"round": round_no, "code": code,
                                               "attempt": deepcopy(attempt)}])[-4:]
            if code in (2, 3):
                self.direct = None
                self.hypothesis = None
        resources = sorted({r["name"] for r in payload.get("vendorShopList", [])
                            if isinstance(r, dict) and isinstance(r.get("name"), str) and 0 < len(r["name"]) <= 80})[:32]
        info = payload.get("mapInfo") or {}
        for owner in OWNERS:
            if not self.sources[owner] or (owner == "treasure" and (self.taken or self.direct)):
                continue
            version = self.version(owner)
            if self.resolved[owner] == version:
                continue
            link = self.links[owner]
            if link and link["version"] == version:
                continue
            # Retire obsolete queued interpretations without overwriting an
            # already in-flight reply. That reply will later be ignored by ID.
            for request in router.queue:
                if request.owner == owner and request.generation != version:
                    request.status = "obsolete"
            if self.attempts[owner]["version"] != version:
                self.attempts[owner] = {"version": version, "emitted": 0}
                self.failures[owner] = 0
            count = self.attempts[owner]["emitted"]
            if count >= MAX_STEPS or self.failures[owner] >= 2:
                self.status[owner] = "retry_limit"
                continue
            token = sha(owner + version + str(count))[:24]
            prompt = self._prompt(owner, token, resources, info)
            if len(prompt) > 12000:
                self.status[owner] = "context_budget_exceeded"
                continue
            request = router.offer(owner, version, version, kind="prompt", payload=prompt,
                                   nonce=token, expected_result_shape="plan_json",
                                   operation_id=token, priority=25 if owner == "treasure" else 30)
            if request is None:
                self.status[owner] = "proposal_rejected"
                continue
            self.links[owner] = {"request_id": request.request_id, "token": token, "version": version,
                                 "resources": resources, "width": info.get("width", 41),
                                 "height": info.get("height", 32), "acknowledged": False,
                                 "views": self._views(owner)}
            self.status[owner] = "queued"

    def _source_view(self, owner):
        index = {r['id']: r for r in self.memories[owner].index()}
        cap = min(TEXT_LIMIT, 2000 // max(1, len(self.sources[owner])))
        view = [{**r, 'text': r['text'][:cap], 'truncated': r['truncated'] or len(r['text']) > cap,
                 'memory': {k: v for k, v in index.get(r['id'], {}).items() if k not in ('id', 'label', 'first_round')}}
                for r in self.sources[owner]]
        # Prompt metadata describes the text included in THIS request. Do not
        # mutate delivered coverage here: previews and queued prompts are not
        # acknowledgements. Otherwise a model following next_unread literally
        # wastes a call requesting the prefix it has just been shown.
        for row in view:
            ranges = self.memories[owner].visible.get(row['id'], []) + [[0, len(row['text'])]]
            focus = self.focus[owner]
            if focus and focus.get('source_id') == row['id'] and 'text' in focus:
                ranges = ranges + [[focus['offset'], focus['end']]]
            offset = 0
            for start, end in sorted(ranges):
                if start > offset:
                    break
                offset = max(offset, end)
            meta = row['memory']
            meta['next_unread'] = offset if meta.get('available') and offset < meta.get('stored_chars', 0) else None
            meta['reviewed_after_this_prompt'] = bool(meta.get('complete') and offset == meta.get('received_chars'))
        return view

    def _views(self, owner):
        result = [{'id': r['id'], 'start': 0, 'end': len(r['text'])} for r in self._source_view(owner) if r['text']]
        focus = self.focus[owner]
        if focus and 'text' in focus:
            result.append({'id': focus['source_id'], 'start': focus['offset'], 'end': focus['end']})
        return result

    def _reviewed(self, owner):
        return all(self.memories[owner].reviewed(r['id']) for r in self.sources[owner])

    def _sent_views(self, owner, prompt):
        """Derive coverage from the actual wire text, never a saved range claim."""
        try:
            before, sources = prompt.rsplit('\n公开来源：', 1)
            focus = json.loads(before.rsplit('\n最近原文检索：', 1)[1])
            rows = json.loads(sources)
            if not isinstance(rows, list):
                return []
            result = []
            for row in rows:
                original = self.memories[owner].archive.document(row['id'])
                text = row['text']
                if (original and isinstance(original['text'], str) and isinstance(text, str)
                        and text and original['text'].startswith(text)):
                    result.append({'id': row['id'], 'start': 0, 'end': len(text)})
            if isinstance(focus, dict) and 'text' in focus:
                original = self.memories[owner].archive.document(focus['source_id'])
                if (original and isinstance(original['text'], str)
                        and type(focus['offset']) is int and type(focus['end']) is int
                        and original['text'][focus['offset']:focus['end']] == focus['text']):
                    result.append({'id': focus['source_id'], 'start': focus['offset'], 'end': focus['end']})
            return result
        except (ValueError, TypeError, KeyError, AttributeError):
            return []

    def _prompt(self, owner, token, resources, info):
        view = self._source_view(owner)
        common = (
            '根据公开资料做游戏决策辅助，只返回JSON，不返回角色动作、Shell或隐藏思维链。'
            '所有结论均是推断，证据必须引用sourceId和该来源中逐字连续的quote。'
            '每130回合一天，白天前70回合；第d天第一回合=(d-1)*130+1。'
            'firstRound是首次看到的回合，不等于发布日期；memory.anchor_day为空时相对日期起点未知。'
            '只能用明确游戏日期/回合或有anchor_day的证据确定日期，否则日期填null。'
            '资料截断、冲突或缺失时保留未知，不能猜测未提供条件。'
            'memory.next_unread表示本提示词附带原文之后的未读位置；reviewed_after_this_prompt表示本次是否已覆盖全文。'
            '需要更多原文时返回request_id和inspect对象：'
            '{"source_id":"来源id","offset":0,"length":2000,"query":"可选字面搜索词"}。'
            '这只读取已收到的内存，不是沙盒，不新增官方权限。检索后的下一次模型调用仍占普通日限。'
            'inspect时可附draft，格式与本主题events或hypothesis的值相同，用于保存结构化中间约束。'
            '未读完来源不批准完整解释；保留数字、否定和矛盾的候选，不能仅记一段自由摘要。\n'
            f'request_id必须为{token}。\n')
        if owner == "news":
            schema = ('返回{"request_id":"' + token + '","events":[{'
                      '"resource":"公开矿名","availability":"available|unavailable|unknown",'
                      '"startDay":1,"endDay":2,"priceDirection":"up|down|unchanged|unknown",'
                      '"evidence":[{"sourceId":"来源id","quote":"原文"}]}]}。'
                      '最多8条事件，日期在1..10且含首尾；日期未知时startDay/endDay同时null。普通价格涨幅未知就不能生成价格数字。'
                      '综合更正消息，不把互相矛盾的事件当成确定结论。允许空events。'
                      '\n公开矿名：' + json.dumps(resources, ensure_ascii=False))
        else:
            schema = ('返回{"request_id":"' + token + '","hypothesis":{'
                      '"site":null,"items":null,"opensAt":null,"closesAt":null,'
                      '"uncertain":true,"unknowns":["site","items","window"],"candidates":[],"evidence":{"site":[],"items":[],"window":[]}}}。'
                      '已知site填写{"x":整数,"y":整数}，items是精确物品名数组（保留重数），'
                      'opensAt/closesAt是含首尾的绝对回合。每个已知部分的证据是'
                      '[{"sourceId":"来源id","quote":"原文"}]，缺失部分保留null。'
                      'unknowns列出site/items/window/conflict/publication_time/unread/prerequisites。只有唯一缺项window才允许提前备料。'
                      'candidates保留不同候选：[{"field":"site|items|window|prerequisite","value":值,"evidence":引用数组}]。'
                      '候选可加polarity:"assert"或"exclude"（否定）；明确更正/撤回旧候选时，在旧候选上加'
                      'resolution:{"kind":"corrected|cancelled","evidence":更正原文引用}，保留旧证据。'
                      '未显式解决的旧候选会继续保留，不能用省略候选的办法遗忘矛盾或否定。'
                      'window候选value含opensAt/closesAt；prerequisite为条件原文字符串，未核验时必须保留unknowns。'
                      '只有所有条件完整且无冲突才设uncertain=false。合并此前各日同一祭坛的资料，'
                      '遇到更正或召唤失败应重新审视，而不是沿用旧答案。'
                      f'地图宽{info.get("width", 41)}高{info.get("height", 32)}。'
                      '\n己方公开召唤反馈：' + json.dumps(self.feedback, ensure_ascii=False))
        return (common + schema + '\n已验证结构化草稿：' + json.dumps(self.drafts[owner], ensure_ascii=False)
                + '\n最近原文检索：' + json.dumps(self.focus[owner], ensure_ascii=False)
                + '\n公开来源：' + json.dumps(view, ensure_ascii=False))

    def _evidence(self, owner, evidence):
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            return False
        records = {r["id"]: r for r in self.sources[owner]}
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"sourceId", "quote"}:
                return False
            record = records.get(item.get("sourceId"))
            quote = item.get("quote")
            if (record is None or not self.memories[owner].quote_visible(item.get('sourceId'), quote)):
                return False
        return True

    def consume(self, request, receipt, raw):
        owner = request.owner if request else ""
        if owner not in OWNERS or self.degraded:
            return
        link = self.links[owner]
        if not link or link["request_id"] != request.request_id:
            return
        if receipt.status in ("expired", "rejected"):
            self.links[owner] = None
            self.status[owner] = receipt.status
            self.failures[owner] += int(bool(link.get('acknowledged')))
            return
        if receipt.status != "received":
            return
        self.links[owner] = None
        try:
            if (link["version"] != self.version(owner) or not isinstance(raw, str)
                    or sha(raw) != receipt.text_sha256 or len(raw) > 12000):
                raise ValueError("unavailable or obsolete receipt")
            data = _json_unique(raw)
            if isinstance(data, dict) and 'inspect' in data:
                if set(data) - {'request_id', 'inspect', 'draft'} or data.get('request_id') != link['token']:
                    raise ValueError('invalid inspect envelope')
                query = data['inspect']
                if (not isinstance(query, dict) or set(query) - {'source_id', 'offset', 'length', 'query'}
                        or not isinstance(query.get('source_id'), str)
                        or query.get('source_id') not in {r['id'] for r in self.sources[owner]}
                        or type(query.get('offset', 0)) is not int or query.get('offset', 0) < 0
                        or type(query.get('length', 2000)) is not int or not 1 <= query.get('length', 2000) <= 2000
                        or not isinstance(query.get('query', ''), str) or len(query.get('query', '')) > 200):
                    raise ValueError('invalid inspect source')
                draft = self.drafts[owner]
                if 'draft' in data:
                    if len(json.dumps(data['draft'], ensure_ascii=False)) > DRAFT_LIMIT:
                        raise ValueError('draft budget exceeded')
                    draft = self._validated_draft(owner, data['draft'], link)
                self.focus[owner] = self.memories[owner].inspect(query['source_id'],
                    offset=query.get('offset', 0), length=query.get('length', 2000), query=query.get('query', ''))
                self.drafts[owner] = draft
                self.status[owner] = 'inspected'
                return
            field = "events" if owner == "news" else "hypothesis"
            if not isinstance(data, dict) or set(data) != {"request_id", field} or data["request_id"] != link["token"]:
                raise ValueError("invalid envelope")
            value = self._validated_draft(owner, data[field], link)
            if len(json.dumps(value, ensure_ascii=False)) > DRAFT_LIMIT:
                raise ValueError('draft budget exceeded')
            self.drafts[owner] = value
            if not self._reviewed(owner):
                self.status[owner] = 'needs_reading'
                return
            if owner == 'news':
                self.news_events = value
            else:
                self.hypothesis = value
            self.resolved[owner] = link["version"]
            self.status[owner] = "interpreted"
        except (ValueError, TypeError, KeyError, RecursionError):
            self.status[owner] = "invalid_reply"
            self.failures[owner] += 1

    def _dated(self, owner, evidence):
        for item in evidence if isinstance(evidence, list) else []:
            origin = self.memories[owner].origins.get(item.get('sourceId'), {})
            quote = item.get('quote', '')
            if origin.get('anchor_day') is not None:
                return True
            if isinstance(quote, str) and re.search(
                    r'(?:游戏|比赛|今天|今日)(?:是|为)?第?[0-9一二三四五六七八九十]+天|'
                    r'第[0-9一二三四五六七八九十]+天(?:白昼|白天|夜晚)|第\s*\d+\s*[-—至]\s*\d+\s*回合', quote):
                return True
        return False

    @staticmethod
    def _candidate_key(candidate):
        return json.dumps({k: candidate.get(k, 'assert') for k in ('field', 'value', 'evidence', 'polarity')},
                          ensure_ascii=False, sort_keys=True)

    def _validated_draft(self, owner, value, link):
        # The model supplies typed facts, not a free-form compression. Merge the
        # previous ledger before accepting an answer so omitted exclusions and
        # conflicts cannot silently disappear between chunks or publications.
        value = self._validate_value(owner, value, link)
        if owner == 'treasure':
            candidates = deepcopy(value.get('candidates', []))
            for field in ('site', 'items', 'window'):
                item = {'opensAt': value['opensAt'], 'closesAt': value['closesAt']} if field == 'window' else value[field]
                if item is not None and (field != 'window' or value['opensAt'] is not None):
                    candidates.append({'field': field, 'value': item, 'evidence': value['evidence'][field]})
            previous = self.drafts['treasure'] or self.hypothesis or {}
            keys = {self._candidate_key(c) for c in candidates}
            candidates.extend(deepcopy(c) for c in previous.get('candidates', []) if self._candidate_key(c) not in keys)
            # Explicitly supplied resolutions take precedence over auto-added
            # duplicate assertions, while both old and correcting quotes remain.
            unique = {}
            for candidate in candidates:
                unique.setdefault(self._candidate_key(candidate), candidate)
            value['candidates'] = list(unique.values())
            value = self._validate_value(owner, value, link)
        if len(json.dumps(value, ensure_ascii=False)) > DRAFT_LIMIT:
            raise ValueError('draft budget exceeded')
        return value

    def _validate_value(self, owner, value, link):
        if owner == "news":
            events = value
            if not isinstance(events, list) or len(events) > 8:
                raise ValueError("invalid events")
            for event in events:
                if (not isinstance(event, dict) or set(event) != {
                        "resource", "availability", "startDay", "endDay", "priceDirection", "evidence"}
                        or not isinstance(event["resource"], str) or not 0 < len(event["resource"]) <= 80
                        or event["resource"] not in link["resources"]
                        or event["availability"] not in ("available", "unavailable", "unknown")
                        or event["priceDirection"] not in ("up", "down", "unchanged", "unknown")
                        or not ((event['startDay'] is None and event['endDay'] is None)
                            or (type(event['startDay']) is int and type(event['endDay']) is int
                                and 1 <= event['startDay'] <= event['endDay'] <= 10
                                and self._dated(owner, event['evidence'])))
                        or not self._evidence(owner, event["evidence"])):
                    raise ValueError("invalid event")
            return deepcopy(events)
        else:
            item = value
            if (not isinstance(item, dict) or not {'site', 'items', 'opensAt', 'closesAt', 'uncertain', 'evidence'} <= set(item)
                    or set(item) - {'site', 'items', 'opensAt', 'closesAt', 'uncertain', 'evidence', 'unknowns', 'candidates'}):
                raise ValueError("invalid hypothesis")
            evidence = item["evidence"]
            if type(item["uncertain"]) is not bool or not isinstance(evidence, dict) or set(evidence) != {"site", "items", "window"}:
                raise ValueError("invalid hypothesis evidence")
            if any(not isinstance(v, list) or len(v) > 8 for v in evidence.values()):
                raise ValueError("invalid evidence collections")
            site = item["site"]
            if site is not None and (not isinstance(site, dict) or set(site) != {"x", "y"}
                    or type(site["x"]) is not int or type(site["y"]) is not int
                    or not 0 <= site["x"] < link["width"] or not 0 <= site["y"] < link["height"]
                    or not self._evidence(owner, evidence["site"])):
                raise ValueError("invalid site")
            items = item["items"]
            if items is not None and (not isinstance(items, list) or not 1 <= len(items) <= 40
                    or any(not isinstance(v, str) or not v or len(v) > 80 for v in items)
                    or not self._evidence(owner, evidence["items"])):
                raise ValueError("invalid items")
            start, end = item["opensAt"], item["closesAt"]
            if start is not None or end is not None:
                if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= 1300
                        or not self._evidence(owner, evidence["window"]) or not self._dated(owner, evidence['window'])):
                    raise ValueError("invalid window")
            self._constraints(item, owner, link)
            return deepcopy(item)

    def _constraints(self, item, owner, link):
        unknowns = item.get('unknowns', [])
        candidates = item.get('candidates', [])
        if (not isinstance(unknowns, list) or len(unknowns) > 7
                or any(v not in ('site', 'items', 'window', 'conflict', 'publication_time', 'unread', 'prerequisites') for v in unknowns)
                or not isinstance(candidates, list) or len(candidates) > 16
                or (unknowns and not item['uncertain'])):
            raise ValueError('invalid uncertainty ledger')
        seen, excluded = {}, {}
        for candidate in candidates:
            if (not isinstance(candidate, dict) or not {'field', 'value', 'evidence'} <= set(candidate)
                    or set(candidate) - {'field', 'value', 'evidence', 'polarity', 'resolution'}
                    or candidate['field'] not in ('site', 'items', 'window', 'prerequisite')
                    or candidate.get('polarity', 'assert') not in ('assert', 'exclude')
                    or candidate['value'] is None
                    or not self._evidence(owner, candidate['evidence'])):
                raise ValueError('invalid candidate')
            field, value = candidate['field'], candidate['value']
            resolution = candidate.get('resolution')
            if resolution is not None and (not isinstance(resolution, dict) or set(resolution) != {'kind', 'evidence'}
                    or resolution['kind'] not in ('corrected', 'cancelled')
                    or not self._evidence(owner, resolution['evidence'])):
                raise ValueError('invalid candidate resolution')
            if field == 'prerequisite':
                if (not isinstance(value, str) or not 0 < len(value) <= 400
                        or (resolution is None and 'prerequisites' not in unknowns)):
                    raise ValueError('unverified prerequisite')
            else:
                probe = {'site': None, 'items': None, 'opensAt': None, 'closesAt': None,
                         'uncertain': True, 'evidence': {'site': [], 'items': [], 'window': []}}
                if field == 'window':
                    if not isinstance(value, dict) or set(value) != {'opensAt', 'closesAt'}:
                        raise ValueError('invalid candidate window')
                    probe.update(value)
                else:
                    probe[field] = value
                probe['evidence'][field] = candidate['evidence']
                self._validate_value(owner, probe, link)
            if resolution is None:
                target = excluded if candidate.get('polarity') == 'exclude' else seen
                canonical = sorted(value) if field == 'items' else value
                target.setdefault(field, set()).add(json.dumps(canonical, ensure_ascii=False, sort_keys=True))
        conflicts = any(len(values) > 1 or values & excluded.get(field, set()) for field, values in seen.items())
        if conflicts and ('conflict' not in unknowns or not item['uncertain']):
            raise ValueError('unresolved conflicting candidates')

    def acknowledge(self, payload, router, response):
        round_no = int(payload.get("roundNo") or 1)
        pending = router.pending["prompt"]
        for owner, link in self.links.items():
            if (link and pending and not link["acknowledged"]
                    and pending.request_id == link["request_id"] and pending.sent_round == round_no
                    and response.prompt == pending.payload):
                self.attempts[owner]["emitted"] += 1
                for view in self._sent_views(owner, response.prompt):
                    self.memories[owner].expose(view['id'], view['start'], view['end'])
                link["acknowledged"] = True
                self.status[owner] = "waiting_model"
        for command in response.commands.values():
            if command.get("action") == "summonTreasure":
                self.last_attempt = {"round": round_no, "site": deepcopy(command.get("targetPos")),
                                     "items": deepcopy(command.get("item")), "received": False}

    def policy_view(self, round_no):
        day = (round_no - 1) // 130 + 1
        current = [e for e in self.news_events if type(e['startDay']) is int and type(e['endDay']) is int
                   and e["startDay"] <= day <= e["endDay"]
                   and self.resolved['news'] == self.version('news')]
        resources = {e["resource"] for e in current}
        unavailable = []
        for resource in resources:
            states = {e["availability"] for e in current if e["resource"] == resource}
            if states == {"unavailable"}:
                unavailable.append(resource)  # conflicts/unknowns do not become bans
        notes = {"known": False, "taken": self.taken, "source": "public_world_agent"}
        if not self.degraded and self.direct:
            notes.update(deepcopy(self.direct))
        elif (not self.degraded and self.hypothesis and self.resolved["treasure"] == self.version("treasure")):
            h = self.hypothesis
            complete = self._reviewed('treasure') and not h["uncertain"] and not h.get('unknowns') and all(h.get(k) is not None for k in ("site", "items", "opensAt", "closesAt"))
            notes.update({k: deepcopy(h[k]) for k in ("site", "items", "opensAt", "closesAt")})
            notes["known"] = complete
            notes['unknowns'] = deepcopy(h.get('unknowns', []))
            notes["preparable"] = bool(self._reviewed('treasure') and h["site"] and h["items"]
                and h["opensAt"] is None and h["closesAt"] is None and set(h.get('unknowns', [])) == {'window'})
        notes["taken"] = self.taken
        notes["open"] = bool(notes["known"] and notes["opensAt"] <= round_no <= notes["closesAt"])
        return {"unavailable": sorted(unavailable) if not self.degraded else [],
                "treasure": notes, "interpretation": "model_inference_with_public_evidence"}

    def dump(self):
        return {'schema': SCHEMA, **{k: deepcopy(v) for k, v in self.__dict__.items() if k != 'memories'},
                'memories': {owner: memory.dump() for owner, memory in self.memories.items()}}

    @classmethod
    def load(cls, raw):
        result = cls()
        try:
            if (not isinstance(raw, dict) or set(raw) != set(result.dump()) or raw["schema"] != SCHEMA
                    or len(json.dumps(raw, ensure_ascii=False)) > 800000
                    or type(raw["degraded"]) is not bool or type(raw["taken"]) is not bool):
                raise ValueError("invalid world memory")
            for name in ("sources", "links", "attempts", "resolved", "status"):
                if not isinstance(raw[name], dict) or set(raw[name]) != set(OWNERS):
                    raise ValueError("invalid topic map")
            for owner in OWNERS:
                rows = raw["sources"][owner]
                if not isinstance(rows, list) or len(rows) > SOURCE_LIMIT:
                    raise ValueError("invalid source ledger")
                for r in rows:
                    if (not isinstance(r, dict) or set(r) != {"id", "firstRound", "text", "truncated"}
                            or not isinstance(r["text"], str) or len(r["text"]) > TEXT_LIMIT
                            or type(r["firstRound"]) is not int or not 1 <= r["firstRound"] <= 1300
                            or type(r["truncated"]) is not bool or not isinstance(r["id"], str) or len(r["id"]) != 64
                            or (not r["truncated"] and sha(r["text"]) != r["id"])):
                        raise ValueError("invalid source record")
                if len({r["id"] for r in rows}) != len(rows):
                    raise ValueError("duplicate source identity")
                if not isinstance(raw["resolved"][owner], str) or not isinstance(raw["status"][owner], str):
                    raise ValueError("invalid topic status")
                count = raw["attempts"][owner]
                if (not isinstance(count, dict) or set(count) != {"version", "emitted"}
                        or not isinstance(count["version"], str) or type(count["emitted"]) is not int
                        or not 0 <= count["emitted"] <= MAX_STEPS):
                    raise ValueError("invalid attempt count")
            # Restore structure, then validate all conclusions via the same
            # schema/evidence gate used for live results below.
            for key in result.__dict__:
                if key != 'memories':
                    setattr(result, key, deepcopy(raw[key]))
            if not isinstance(raw['memories'], dict) or set(raw['memories']) != set(OWNERS):
                raise ValueError('invalid archives')
            result.memories = {owner: WorldMemory.load(raw['memories'][owner], owner=owner) for owner in OWNERS}
            for owner in OWNERS:
                memory = result.memories[owner]
                if memory.degraded:
                    raise ValueError('degraded archive')
                for record in result.sources[owner]:
                    original = memory.archive.document(record['id'])
                    if (original is None or memory.origins[record['id']]['first_round'] != record['firstRound']
                            or (original['text'] is not None and not original['text'].startswith(record['text']))):
                        raise ValueError('unassociated retained source')
            result._validate_restored()
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            result = cls()
            result.degraded = True
        return result

    def _validate_restored(self):
        for mapping in (self.focus, self.drafts, self.failures):
            if not isinstance(mapping, dict) or set(mapping) != set(OWNERS):
                raise ValueError('invalid working memory')
        for owner in OWNERS:
            if type(self.failures[owner]) is not int or not 0 <= self.failures[owner] <= MAX_STEPS:
                raise ValueError('invalid failure count')
            focus = self.focus[owner]
            if focus is not None:
                if not isinstance(focus, dict) or 'request' not in focus:
                    raise ValueError('invalid focus')
                if self.memories[owner].inspect(focus.get('source_id'), **focus['request']) != focus:
                    raise ValueError('forged focus')
        if not isinstance(self.feedback, list) or len(self.feedback) > 4:
            raise ValueError("invalid feedback")
        for row in self.feedback:
            if not isinstance(row, dict) or set(row) != {"round", "code", "attempt"} or row["code"] not in (1, 2, 3, 4):
                raise ValueError("invalid feedback record")
        if self.last_attempt is not None and (not isinstance(self.last_attempt, dict)
                or set(self.last_attempt) != {"round", "site", "items", "received"}
                or type(self.last_attempt["round"]) is not int or type(self.last_attempt["received"]) is not bool):
            raise ValueError("invalid attempt")
        for owner in OWNERS:
            link = self.links[owner]
            if link is not None and (not isinstance(link, dict) or set(link) != {
                    "request_id", "token", "version", "resources", "width", "height", "acknowledged", "views"}
                    or any(not isinstance(link[k], str) or len(link[k]) > 160 for k in ("request_id", "token", "version"))
                    or not isinstance(link["resources"], list) or len(link["resources"]) > 32
                    or any(not isinstance(v, str) or len(v) > 80 for v in link["resources"])
                    or type(link["width"]) is not int or type(link["height"]) is not int
                    or not 1 <= link["width"] <= 200 or not 1 <= link["height"] <= 200
                    or type(link["acknowledged"]) is not bool):
                raise ValueError("invalid request link")
            if link is not None:
                if not isinstance(link['views'], list) or len(link['views']) > SOURCE_LIMIT + 1:
                    raise ValueError('invalid pending views')
                for view in link['views']:
                    if not isinstance(view, dict) or set(view) != {'id', 'start', 'end'}:
                        raise ValueError('invalid pending view')
                    memory = deepcopy(self.memories[owner])
                    if not memory.expose(view['id'], view['start'], view['end']):
                        raise ValueError('unavailable pending view')
        # Stored candidates must not bypass validation merely by being JSON.
        for owner, value in (("news", self.news_events), ("treasure", self.hypothesis)):
            if owner == "treasure" and value is None:
                continue
            probe = WorldAgent()
            probe.sources = deepcopy(self.sources)
            probe.feedback = deepcopy(self.feedback)
            probe.memories = deepcopy(self.memories)
            resources = [e.get("resource") for e in value if isinstance(e, dict)] if owner == "news" and isinstance(value, list) else []
            probe._validate_value(owner, value, {"resources": resources, "width": 41, "height": 32})
        for owner, draft in self.drafts.items():
            if draft is not None:
                if len(json.dumps(draft, ensure_ascii=False)) > DRAFT_LIMIT:
                    raise ValueError('draft too large')
                resources = [e.get('resource') for e in draft] if owner == 'news' and isinstance(draft, list) else []
                self._validate_value(owner, draft, {'resources': resources, 'width': 41, 'height': 32})
        if self.direct is not None:
            # Rebuild exact-parser notes from an actual retained public source.
            candidates = [exact_notes(r["text"])
                          for r in self.sources["treasure"] if not r["truncated"]]
            fields = ("site", "items", "opensAt", "closesAt")
            if not isinstance(self.direct, dict) or not any(
                    c and c.get("known") and all(c.get(k) == self.direct.get(k) for k in fields) for c in candidates):
                raise ValueError("invalid direct interpretation")
