"""Public news and cross-day treasure reasoning using the shared LLM router.

R01/R06/R07; no simulator-private facts, future publications or model execution.
Model interpretations remain labelled inferences with verbatim source evidence.
"""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import re

from . import news_ledger
from .world_memory import WorldMemory
from .task_agent import _json_unique
from .treasure import notes_from_news, _RUMOUR

SCHEMA = "competition-world-agent/5"
# /2 is the six-field archive (migrated without inventing a witness).  /4 is the
# previous ledger; it is migrated, while the interim /3 format degrades.
PREV_SCHEMAS = ("competition-world-agent/2", "competition-world-agent/4")
MAX_STEPS = 8  # engineering cap; the shared ordinary daily limit remains three
DRAFT_LIMIT = 5000
OWNERS = ("news", "treasure")
SOURCE_LIMIT = 12
TEXT_LIMIT = 3000
EVIDENCE_LIMIT = 8
MAX_NEWS_EVENTS = 24
MAX_NEWS_GAPS = 8
GAP_LOST_LIMIT = 64
LEDGER_PROMPT_LIMIT = 12
LEDGER_CONFLICT_LIMIT = 12
NEWS_VIEW_SCHEMA = "competition-news-view/1"

_NEWS_BASE_KEYS = frozenset({"resource", "availability", "startDay", "endDay",
                             "priceDirection", "evidence"})
_NEWS_OPTIONAL_KEYS = frozenset({"resumeDay", "priceAmount", "priceBasis", "resolution"})
_NEWS_LEDGER_KEYS = (_NEWS_BASE_KEYS | _NEWS_OPTIONAL_KEYS
                     | frozenset({"id", "sourceRound", "kind", "status", "witness"}))
_GAP_KEYS = frozenset({"resource", "startDay", "endDay", "count", "lostIds", "overflow"})
_NEWS_ID_RE = re.compile(r'n[0-9a-f]{16}\Z')
_KNOWN_DIRECTIONS = ("up", "down", "unchanged")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _news_witness(record):
    """Bind the recorded evidence (and its resolution evidence) to one digest."""
    return news_ledger.witness({"evidence": record.get("evidence"),
                                "resolution": record.get("resolution")})


def _canonical_news_event(event, origins, fallback_round=1, bind_witness=True):
    """Canonical ledger record from a validated (or legacy) interpretation.

    The program assigns the stable id, the inferred label and the initial active
    status.  A model reply can never set them itself.  `bind_witness` is False
    only for a migrated archive: computing a digest would claim a verification
    history the old format never had, so those records stay unverifiable until
    a retained source can be checked or a live reply re-establishes them.
    """
    amount, ok = news_ledger.normalize_amount(event.get("priceAmount"))
    if not ok:
        raise ValueError("invalid price amount")
    basis = event.get("priceBasis", "unknown")
    evidence = deepcopy(event.get("evidence", []))
    source_ids = [item.get("sourceId") for item in evidence
                  if isinstance(item, dict) and isinstance(item.get("sourceId"), str)]
    rounds = [origins.get(source, {}).get("first_round") for source in source_ids]
    rounds = [value for value in rounds if type(value) is int]
    record = {
        "id": news_ledger.news_id(resource=event.get("resource"), availability=event.get("availability"),
                                  start_day=event.get("startDay"), end_day=event.get("endDay"),
                                  resume_day=event.get("resumeDay"), price_direction=event.get("priceDirection"),
                                  price_amount=amount, price_basis=basis, source_ids=source_ids),
        "resource": event.get("resource"), "availability": event.get("availability"),
        "startDay": event.get("startDay"), "endDay": event.get("endDay"),
        "resumeDay": event.get("resumeDay"), "priceDirection": event.get("priceDirection"),
        "priceAmount": amount, "priceBasis": basis, "evidence": evidence,
        "sourceRound": min(rounds) if rounds else fallback_round, "kind": "inferred",
        "status": "active", "resolution": None,
        "witness": news_ledger.witness({"evidence": evidence, "resolution": None}) if bind_witness else None,
    }
    return record


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
        self.news_gaps = []
        self.news_gap_overflow = False
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

    def _ledger_view(self):
        """Bounded ledger shipped to the model so it can cite/correct it.

        Only records with currently verifiable provenance are sent, with the
        per-day effectiveness the consumer actually uses, and the caller also
        sends the unresolved-conflict summary plus the omitted/unverifiable
        counts, so a truncated tail never looks like a settled history.
        """
        rows = []
        for fact in self.news_view()["facts"][-LEDGER_PROMPT_LIMIT:]:
            row = {key: fact[key] for key in ("id", "resource", "availability", "startDay",
                                              "endDay", "resumeDay", "priceDirection",
                                              "priceAmount", "priceBasis", "status")}
            row["effectiveDays"] = fact["effectiveDays"] if fact["effectiveDays"] is not None else "unknown"
            if fact["resolution"]:
                row["resolvesTarget"] = fact["resolution"]["targetId"]
            rows.append(row)
        return rows

    def _ledger_conflicts(self):
        """Unified unresolved-conflict summary, including possible overlaps."""
        return [{"resource": entry["resource"], "ids": entry["ids"],
                 "dimensions": entry["dimensions"], "days": entry["days"],
                 "possible": not entry["definite"]}
                for entry in self.news_view()["conflicts"]]

    def _ledger_context(self):
        """Prompt-side summary that keeps omitted history and gaps visible."""
        view = self.news_view()
        conflicts = self._ledger_conflicts()
        return {
            "conflicts": conflicts[:LEDGER_CONFLICT_LIMIT],
            "conflictCount": len(conflicts),
            "gaps": deepcopy(view["gaps"]),
            "gapOverflow": view["gapOverflow"],
            "omitted": max(0, len(view["facts"]) - LEDGER_PROMPT_LIMIT),
            "unverifiable": view["unverifiable"],
        }

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
            '检索回复示例：{"request_id":"本次标识","inspect":{"source_id":"来源id","offset":2000,"length":2000},"draft":主题草稿}。'
            'draft是顶层字段，length最多2000；需要更多字数时继续分段。'
            '未读完来源不批准完整解释；保留数字、否定和矛盾的候选，不能仅记一段自由摘要。\n'
            f'request_id必须为{token}。\n')
        if owner == "news":
            schema = ('返回{"request_id":"' + token + '","events":[{'
                      '"resource":"公开矿名","availability":"available|unavailable|unknown",'
                      '"startDay":1,"endDay":2,"resumeDay":3,'
                      '"priceDirection":"up|down|unchanged|unknown",'
                      '"priceAmount":2,"priceBasis":"absolute|delta|percent|unknown",'
                      '"evidence":[{"sourceId":"来源id","quote":"原文"}],'
                      '"resolution":null}]}。'
                      '最多8条事件，日期在1..10且含首尾；startDay/endDay/resumeDay各自可未知，'
                      '已知端点必须有公开日期依据；resumeDay可空且不能早于/等于已知的endDay或startDay。'
                      'priceAmount只在逐字引文明确写出金额且未被否定时填写：'
                      '"上涨到6金币"/"下降到6金币"→absolute 6，"上涨2金币"/"下降2金币"→delta 2，'
                      '"上涨20%"/"下降20%"→percent 20；'
                      '引文只有日期数字、写的是"个百分点"、有否定词或未给幅度时必须为null且priceBasis=unknown，'
                      '不得换算或猜测。金额必须与引文完全相等（6与6.0等价，6不等于6.1）。'
                      '更正/撤回旧事件时在resolution填{"kind":"corrected|cancelled","targetId":"账本id",'
                      '"evidence":[{"sourceId":"来源id","quote":"含更正/撤回字样的原文"}]}；'
                      '更正只覆盖它自身有日期证据的区间，旧事件在未覆盖日期仍然有效'
                      '（账本的effectiveDays就是消费端采用的有效日）；'
                      '同一旧事件可被多个不同时间范围的更正分别覆盖，再次更正可以指向此前的更正事件；'
                      '重复同一更正可幂等重发。更正必须与目标同资源、有可能日期交集，禁止自指和环，'
                      '不得给同一事实换targetId。'
                      '没有明确更正关系的相反消息（含不同价格/方向）各自保留证据，不得用最后一句覆盖；'
                      '未知日期的矛盾要在账本状态里保留possible冲突，不能当作已解决。允许空events。'
                      '\n新闻事件账本（id可被resolution.targetId引用）：'
                      + json.dumps(self._ledger_view(), ensure_ascii=False)
                      + '\n账本状态（未解决冲突、范围缺口、省略条数）：'
                      + json.dumps(self._ledger_context(), ensure_ascii=False)
                      + '\n公开矿名：' + json.dumps(resources, ensure_ascii=False))
        else:
            schema = ('返回{"request_id":"' + token + '","hypothesis":{'
                      '"site":null,"items":null,"opensAt":null,"closesAt":null,'
                      '"uncertain":true,"unknowns":["site","items","window"],"candidates":[],"evidence":{"site":[],"items":[],"window":[]}}}。'
                      '已知site填写{"x":整数,"y":整数}，items是精确物品名数组（保留重数），'
                      'opensAt/closesAt是含首尾的绝对回合。每个已知部分的证据是'
                      '[{"sourceId":"来源id","quote":"原文"}]，缺失部分保留null。'
                      'unknowns只列当前尚未解决的项，从site/items/window/conflict/publication_time/unread/prerequisites中选择；'
                      '每次获得新原文后重新检查并移除已解决项，不机械沿用草稿标记。只有唯一缺项window才允许提前备料。'
                      'publication_time仅用于相对日期缺少基准；原文给出绝对游戏日或回合时，不因文章发布日期未知而添加它。'
                      'prerequisites仅用于原文实际提出且尚未核验的开启前置条件，须保留其原文候选；不要假设未提及的隐藏仪式或前置任务。'
                      '尚未采购材料、尚未抵达地点由执行层检查，不属于线索缺项。禁止的物品保留exclude；'
                      '候选材料不含该物品时，该禁令本身不产生未核验的prerequisites。'
                      'candidates保留不同候选：[{"field":"site|items|window|prerequisite","value":值,"evidence":引用数组}]。'
                      '候选可加polarity:"assert"或"exclude"（否定）；明确更正/撤回旧候选时，在旧候选上加'
                      'resolution:{"kind":"corrected|cancelled","evidence":更正原文引用}，保留旧证据。'
                      '未显式解决的旧候选会继续保留，不能用省略候选的办法遗忘矛盾或否定。'
                      'window候选value含opensAt/closesAt；prerequisite为条件原文字符串，未核验时必须保留unknowns。'
                      '只有所有条件完整且无冲突才设uncertain=false。合并此前各日同一祭坛的资料，'
                      '遇到更正或召唤失败应重新审视，而不是沿用旧答案。'
                      f'地图宽{info.get("width", 41)}高{info.get("height", 32)}。'
                      '\n己方公开召唤反馈：' + json.dumps(self.feedback, ensure_ascii=False))
        retry = ('\n上次回复未通过校验：请核对JSON层次、原文逐字引用和未解决的冲突；检索结果不是最终行动计划。'
                 if self.failures[owner] else '')
        return (common + schema + retry + '\n已验证结构化草稿：' + json.dumps(self.drafts[owner], ensure_ascii=False)
                + '\n最近原文检索：' + json.dumps(self.focus[owner], ensure_ascii=False)
                + '\n公开来源：' + json.dumps(view, ensure_ascii=False))

    def _evidence(self, owner, evidence):
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= EVIDENCE_LIMIT:
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

    @staticmethod
    def _ledger_evidence_syntax(evidence):
        """Structural check: a bounded list of {sourceId, quote} with sane types."""
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= EVIDENCE_LIMIT:
            return False
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"sourceId", "quote"}:
                return False
            source, quote = item.get("sourceId"), item.get("quote")
            if (not isinstance(source, str) or len(source) != 64
                    or any(c not in "0123456789abcdef" for c in source)
                    or not isinstance(quote, str) or not 0 < len(quote) <= 500):
                return False
        return True

    def _news_record_usable(self, record):
        """Trust only evidence that is still visible or bound by its witness.

        Retained sources must expose the quote verbatim; a source that has since
        scrolled out of the bounded window is trusted only through the digest
        captured when the quote was verified.  A migrated record that never had
        such a binding is explicitly unusable instead of silently trusted.
        """
        witness = record.get("witness")
        if witness is not None and witness != _news_witness(record):
            return False
        retained = {item["id"] for item in self.sources["news"]}
        groups = [record.get("evidence")]
        if record.get("resolution"):
            groups.append(record["resolution"].get("evidence"))
        evicted = False
        for group in groups:
            if group is None:
                continue
            if not self._ledger_evidence_syntax(group):
                return False
            for item in group:
                if item["sourceId"] in retained:
                    if not self.memories["news"].quote_visible(item["sourceId"], item["quote"]):
                        return False
                else:
                    evicted = True
        return not (evicted and witness is None)

    def _usable_ledger(self):
        return [record for record in self.news_events if self._news_record_usable(record)]

    def _news_record_verified(self, record):
        """Stricter than usable: every citation is retained and verbatim visible."""
        retained = {item["id"] for item in self.sources["news"]}
        groups = [record.get("evidence")]
        if record.get("resolution"):
            groups.append(record["resolution"].get("evidence"))
        for group in groups:
            if group is None:
                continue
            if not self._ledger_evidence_syntax(group):
                return False
            for item in group:
                if item["sourceId"] not in retained:
                    return False
                if not self.memories["news"].quote_visible(item["sourceId"], item["quote"]):
                    return False
        return True

    # ---- unified per-day validity and dimensional conflicts -----------------

    @staticmethod
    def _asserted_days(record):
        start, end = record["startDay"], record["endDay"]
        if type(start) is not int or type(end) is not int:
            return None
        return set(range(start, end + 1))

    @staticmethod
    def _possible_days(record):
        start = record["startDay"] if type(record["startDay"]) is int else 1
        end = record["endDay"] if type(record["endDay"]) is int else 10
        return set(range(start, end + 1))

    @staticmethod
    def _news_incoming(index):
        incoming = {}
        for record in index.values():
            resolution = record.get("resolution")
            if resolution and resolution["targetId"] in index:
                incoming.setdefault(resolution["targetId"], []).append(record)
        return incoming

    def _news_effective(self, record, incoming):
        """Definite days exclude every direct corrector's possible interval.

        The subtraction uses each corrector's own bounded interval, never its
        reduced effective set: a later correction only edits that correction, so
        it can never silently revive a date an explicit correction retracted.
        """
        asserted = self._asserted_days(record)
        if asserted is None:
            return None
        days = set(asserted)
        for corrector in incoming.get(record["id"], ()):
            # An uncertain correction does not establish a replacement fact,
            # but its possible interval cannot leave the old claim certain.
            # The possible view below still retains those uncertain old days.
            days -= self._possible_days(corrector)
        return days

    def _news_possible_effective(self, record, incoming):
        days = self._possible_days(record)
        for corrector in incoming.get(record["id"], ()):
            covered = self._asserted_days(corrector)
            if covered:
                days -= covered
        return days

    @staticmethod
    def _news_dimensions(left, right):
        dimensions = []
        if {left["availability"], right["availability"]} == {"available", "unavailable"}:
            dimensions.append("availability")
        if (left["priceAmount"] is not None and right["priceAmount"] is not None
                and left["priceBasis"] == right["priceBasis"]
                and left["priceAmount"] != right["priceAmount"]):
            dimensions.append("price")
        if (left["priceDirection"] in _KNOWN_DIRECTIONS and right["priceDirection"] in _KNOWN_DIRECTIONS
                and left["priceDirection"] != right["priceDirection"]):
            dimensions.append("direction")
        return dimensions

    @staticmethod
    def _news_reaches(index, start_id, goal_id):
        seen = set()
        current = start_id
        while current is not None and current not in seen:
            if current == goal_id:
                return True
            seen.add(current)
            record = index.get(current)
            resolution = record.get("resolution") if isinstance(record, dict) else None
            current = resolution["targetId"] if resolution else None
        return False

    @staticmethod
    def _derive_status(kinds):
        """status is an audit marker derived only from the incoming edges."""
        if not kinds:
            return "active"
        return "cancelled" if "cancelled" in kinds else "corrected"

    def _refresh_status(self, index, target_id):
        target = index.get(target_id)
        if target is None:
            return
        kinds = [other["resolution"]["kind"] for other in index.values()
                 if other.get("resolution") and other["resolution"]["targetId"] == target_id]
        target["status"] = self._derive_status(kinds)

    def news_view(self):
        """One bounded validity/conflict view shared by policy, prompt and page."""
        usable = [] if self.degraded else self._usable_ledger()
        index = {record["id"]: record for record in usable}
        incoming = self._news_incoming(index)
        definite = {record["id"]: self._news_effective(record, incoming) for record in usable}
        possible = {record["id"]: self._news_possible_effective(record, incoming) for record in usable}
        facts = []
        for record in usable:
            facts.append({
                "id": record["id"], "resource": record["resource"],
                "availability": record["availability"], "status": record["status"],
                "kind": record["kind"], "inferred": True,
                "startDay": record["startDay"], "endDay": record["endDay"],
                "resumeDay": record["resumeDay"], "priceDirection": record["priceDirection"],
                "priceAmount": record["priceAmount"], "priceBasis": record["priceBasis"],
                "partial": record["startDay"] is None or record["endDay"] is None,
                "possibleDays": sorted(possible[record["id"]]),
                "effectiveDays": (sorted(definite[record["id"]])
                                  if definite[record["id"]] is not None else None),
                "sources": sorted({entry["sourceId"] for entry in record["evidence"]}),
                "resolution": ({"kind": record["resolution"]["kind"],
                                "targetId": record["resolution"]["targetId"]}
                               if record["resolution"] else None),
            })
        conflicts = []
        for position, left in enumerate(usable):
            for right in usable[position + 1:]:
                if left["resource"] != right["resource"]:
                    continue
                dimensions = self._news_dimensions(left, right)
                if not dimensions:
                    continue
                shared = possible[left["id"]] & possible[right["id"]]
                if not shared:
                    continue
                left_days, right_days = definite[left["id"]], definite[right["id"]]
                overlap = (left_days & right_days
                           if left_days is not None and right_days is not None else set())
                conflicts.append({
                    "resource": left["resource"],
                    "ids": sorted((left["id"], right["id"])),
                    "dimensions": sorted(dimensions),
                    "days": sorted(overlap),
                    "possibleDays": sorted(shared),
                    "definite": bool(overlap),
                    "availability": sorted({left["availability"], right["availability"]}),
                    "directions": sorted({left["priceDirection"], right["priceDirection"]}),
                    "prices": sorted(f"{item['priceBasis']}:{item['priceAmount']}"
                                     for item in (left, right) if item["priceAmount"] is not None),
                    "sources": [sorted({entry["sourceId"] for entry in left["evidence"]}),
                                sorted({entry["sourceId"] for entry in right["evidence"]})],
                })
        conflicts.sort(key=lambda entry: (entry["resource"], entry["ids"], entry["dimensions"]))
        return {
            "schema": NEWS_VIEW_SCHEMA,
            "facts": facts,
            "conflicts": conflicts,
            "gaps": deepcopy(self.news_gaps),
            "gapOverflow": self.news_gap_overflow,
            "unverifiable": len(self.news_events) - len(usable),
        }

    def _merge_news(self, events):
        """Add or correct facts; a new reply never erases still-valid dates."""
        records = deepcopy(self.news_events)
        index = {record["id"]: record for record in records}
        origins = self.memories["news"].origins
        fallback = self.memories["news"].last_round or 1
        for event in events:
            record = _canonical_news_event(event, origins, fallback)
            existing = index.get(record["id"])
            if existing is None:
                records.append(record)
                existing = record
                index[record["id"]] = record
            else:
                merged = list(existing["evidence"])
                for item in record["evidence"]:
                    if item not in merged:
                        merged.append(item)
                existing["evidence"] = merged[:EVIDENCE_LIMIT]
                existing["sourceRound"] = min(existing["sourceRound"], record["sourceRound"])
                existing["witness"] = _news_witness(existing)
            resolution = event.get("resolution")
            if resolution is not None:
                self._bind_resolution(index, existing, resolution)
            elif existing.get("resolution") is None:
                self._associate_correction(index, existing)
        records, lost = self._bound_news(records)
        if lost:
            self._merge_gaps(lost)
        self._recover_gaps(records)
        return records

    def _bind_resolution(self, index, record, resolution):
        """Bind (or idempotently re-bind) one provenance-checked correction edge."""
        target = index.get(resolution["targetId"])
        if target is None or target["id"] == record["id"]:
            raise ValueError("invalid resolution target")
        if target["resource"] != record["resource"] or not self._news_overlap(target, record):
            raise ValueError("unrelated resolution target")
        previous = record.get("resolution")
        if previous is not None:
            if previous["targetId"] != target["id"] or previous["kind"] != resolution["kind"]:
                raise ValueError("resolution target swap")
            merged = list(previous["evidence"])
            for item in resolution["evidence"]:
                if item not in merged:
                    merged.append(item)
            record["resolution"] = {"kind": previous["kind"], "targetId": target["id"],
                                    "evidence": merged[:EVIDENCE_LIMIT]}
        else:
            # An edge may be added to an already-corrected fact (several bounded
            # corrections are allowed) but never so as to close a cycle.
            if self._news_reaches(index, target["id"], record["id"]):
                raise ValueError("cyclic resolution")
            record["resolution"] = {"kind": resolution["kind"], "targetId": target["id"],
                                    "evidence": deepcopy(resolution["evidence"])}
        record["witness"] = _news_witness(record)
        self._refresh_status(index, target["id"])

    def _associate_correction(self, index, record):
        """Deterministic target for a legacy "更正：已恢复" reply.

        The correction wording plus exactly one clearly matching fact of the
        same resource with a possible date overlap binds the target.  Anything
        ambiguous stays as two records, and the same cycle/idempotency rules as
        the explicit branch apply.
        """
        if not any(news_ledger.has_correction_marker(item.get("quote")) for item in record["evidence"]):
            return
        candidates = [other for other in index.values()
                      if other["id"] != record["id"]
                      and other["resource"] == record["resource"]
                      and other["availability"] != record["availability"]
                      and self._news_overlap(other, record)
                      and not self._news_reaches(index, other["id"], record["id"])]
        if len(candidates) != 1:
            return
        target = candidates[0]
        evidence = [deepcopy(item) for item in record["evidence"]
                    if news_ledger.has_correction_marker(item.get("quote"))]
        self._bind_resolution(index, record, {"kind": "corrected", "targetId": target["id"],
                                              "evidence": evidence})

    @staticmethod
    def _news_overlap(left, right):
        # Unknown endpoints allow possible overlap, never a TypeError or an
        # invented infinite collection ban. All game dates lie inside 1..10.
        def bounds(item):
            return (item["startDay"] if type(item["startDay"]) is int else 1,
                    item["endDay"] if type(item["endDay"]) is int else 10)
        lstart, lend = bounds(left)
        rstart, rend = bounds(right)
        return lstart <= rend and rstart <= lend

    def _bound_news(self, records):
        """Keep the ledger bounded; every evicted component leaves a lost-id gap."""
        lost = []
        while len(records) > MAX_NEWS_EVENTS:
            components = self._news_components(records)
            victim = next((members for members in components
                           if all(record["status"] != "active" for record in members)), None)
            if victim is None:
                victim = components[0] if components else None
            if not victim:
                break
            for record in victim:
                records.remove(record)
                start = record["startDay"] if type(record["startDay"]) is int else 1
                end = record["endDay"] if type(record["endDay"]) is int else 10
                lost.append((record["resource"], start, end, record["id"]))
        return records, lost

    def _merge_gaps(self, lost):
        """Fold evicted ids into bounded gaps; overflowing metadata stays opaque."""
        for resource, start, end, identity in lost:
            found = next((gap for gap in self.news_gaps if gap["resource"] == resource), None)
            if found is None:
                if len(self.news_gaps) >= MAX_NEWS_GAPS:
                    self.news_gap_overflow = True
                    continue
                found = {"resource": resource, "startDay": start, "endDay": end, "count": 1,
                         "lostIds": [], "overflow": False}
                self.news_gaps.append(found)
            found["startDay"] = min(found["startDay"], start)
            found["endDay"] = max(found["endDay"], end)
            found["count"] = min(found["count"] + 1, MAX_NEWS_EVENTS)
            if identity not in found["lostIds"]:
                if len(found["lostIds"]) >= GAP_LOST_LIMIT:
                    found["overflow"] = True
                else:
                    found["lostIds"].append(identity)

    def _recover_gaps(self, records):
        """Clear a gap only when every lost id is verified again in the ledger.

        Runs after eviction, so restoring one side of an evicted conflict while
        losing the other cannot clear the gap.  Overflowed gaps stay opaque.
        """
        if not self.news_gaps:
            return
        verified = {record["id"] for record in records if self._news_record_verified(record)}
        self.news_gaps = [gap for gap in self.news_gaps
                          if gap["overflow"] or not gap["lostIds"]
                          or not all(identity in verified for identity in gap["lostIds"])]

    @classmethod
    def _news_components(cls, records):
        """Correction edges and definite conflicts evict together as one group."""
        parent = {record["id"]: record["id"] for record in records}

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left, right):
            lroot, rroot = find(left), find(right)
            if lroot != rroot:
                parent[lroot] = rroot

        for record in records:
            resolution = record.get("resolution")
            if resolution and resolution["targetId"] in parent:
                union(record["id"], resolution["targetId"])
        for position, left in enumerate(records):
            for right in records[position + 1:]:
                if left["resource"] != right["resource"] or not cls._news_dimensions(left, right):
                    continue
                ldays, rdays = cls._asserted_days(left), cls._asserted_days(right)
                if ldays is not None and rdays is not None and ldays & rdays:
                    union(left["id"], right["id"])
        groups, order = {}, []
        for record in records:
            root = find(record["id"])
            if root not in groups:
                groups[root] = []
                order.append(root)
            groups[root].append(record)
        return [groups[root] for root in order]

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
                field = 'events' if owner == 'news' else 'hypothesis'
                if (set(data) - {'request_id', 'inspect', 'draft', field}
                        or data.get('request_id') != link['token']):
                    raise ValueError('invalid inspect envelope')
                query = data['inspect']
                if (not isinstance(query, dict) or set(query) - {'source_id', 'offset', 'length', 'query', 'draft'}
                        or not isinstance(query.get('source_id'), str)
                        or query.get('source_id') not in {r['id'] for r in self.sources[owner]}
                        or type(query.get('offset', 0)) is not int or query.get('offset', 0) < 0
                        or type(query.get('length', 2000)) is not int or query.get('length', 2000) < 1
                        or not isinstance(query.get('query', ''), str) or len(query.get('query', '')) > 200):
                    raise ValueError('invalid inspect source')
                draft = self.drafts[owner]
                # Accept equivalent placements observed in real replies. If a
                # model repeats the same value it remains unambiguous; different
                # draft values are rejected instead of silently picking one.
                alternatives = [data[k] for k in ('draft', field) if k in data]
                if 'draft' in query:
                    alternatives.append(query['draft'])
                if alternatives:
                    candidate = alternatives[0]
                    canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                    if any(json.dumps(other, ensure_ascii=False, sort_keys=True) != canonical for other in alternatives[1:]):
                        raise ValueError('conflicting inspect drafts')
                    if len(json.dumps(candidate, ensure_ascii=False)) > DRAFT_LIMIT:
                        raise ValueError('draft budget exceeded')
                    draft = self._validated_draft(owner, candidate, link)
                self.focus[owner] = self.memories[owner].inspect(query['source_id'],
                    offset=query.get('offset', 0), length=min(query.get('length', 2000), 2000), query=query.get('query', ''))
                self.drafts[owner] = draft
                self.status[owner] = 'inspected'
                return
            field = "events" if owner == "news" else "hypothesis"
            if not isinstance(data, dict) or set(data) != {"request_id", field} or data["request_id"] != link["token"]:
                raise ValueError("invalid envelope")
            value = self._validated_draft(owner, data[field], link)
            if len(json.dumps(value, ensure_ascii=False)) > DRAFT_LIMIT:
                raise ValueError('draft budget exceeded')
            if not self._reviewed(owner):
                self.drafts[owner] = value
                self.status[owner] = 'needs_reading'
                return
            if owner == 'news':
                merged = self._merge_news(value)
                self.drafts[owner] = value
                self.news_events = merged
            else:
                self.drafts[owner] = value
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
        value = candidate['value']
        if candidate['field'] == 'items':
            value = sorted(value)
        # A shorter quote (or omitted trailing punctuation) is not a second
        # assertion when field, value and originating publication are identical.
        # Distinct publications remain separate and retain independent evidence.
        return json.dumps({'field': candidate['field'], 'value': value,
            'polarity': candidate.get('polarity', 'assert'),
            'sources': sorted({item['sourceId'] for item in candidate['evidence']})},
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
            candidates.extend(deepcopy(previous.get('candidates', [])))
            # Explicitly supplied resolutions take precedence over auto-added
            # duplicate assertions, while both old and correcting quotes remain.
            unique = {}
            for candidate in candidates:
                key = self._candidate_key(candidate)
                if key not in unique:
                    unique[key] = candidate
                    continue
                retained = unique[key]
                retained['evidence'] += [e for e in candidate['evidence'] if e not in retained['evidence']]
                if 'resolution' in candidate:
                    retained.setdefault('resolution', candidate['resolution'])
            value['candidates'] = list(unique.values())
            value = self._validate_value(owner, value, link)
        if len(json.dumps(value, ensure_ascii=False)) > DRAFT_LIMIT:
            raise ValueError('draft budget exceeded')
        return value

    def _validate_news_events(self, events, link):
        if not isinstance(events, list) or len(events) > 8:
            raise ValueError("invalid events")
        for event in events:
            self._validate_news_event(event, link, ledger=False)
        return deepcopy(events)

    def _validate_news_event(self, event, link, *, ledger):
        """Typed event gate shared by live replies, drafts and canonical records."""
        allowed = _NEWS_LEDGER_KEYS if ledger else (_NEWS_BASE_KEYS | _NEWS_OPTIONAL_KEYS)
        if (not isinstance(event, dict) or not _NEWS_BASE_KEYS <= set(event) or set(event) - allowed
                or not isinstance(event["resource"], str) or not 0 < len(event["resource"]) <= 80
                or (not ledger and event["resource"] not in link["resources"])
                or event["availability"] not in ("available", "unavailable", "unknown")
                or event["priceDirection"] not in ("up", "down", "unchanged", "unknown")):
            raise ValueError("invalid event")
        amount, ok = news_ledger.normalize_amount(event.get("priceAmount"))
        basis = event.get("priceBasis", "unknown")
        if (not ok or basis not in news_ledger.PRICE_BASES
                or (amount is None) != (basis == "unknown")
                or not news_ledger.dates_consistent(event["startDay"], event["endDay"], event.get("resumeDay"))):
            raise ValueError("invalid event fields")
        # Every known endpoint needs public date evidence; a lone resumeDay is a
        # known endpoint too and must not slip through because startDay is null.
        if (any(event.get(key) is not None for key in ("startDay", "endDay", "resumeDay"))
                and not self._dated("news", event["evidence"])):
            raise ValueError("undated event")
        evidence = event["evidence"]
        quotes = [item.get("quote") for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
        if not news_ledger.verify_price(quotes, amount, basis):
            raise ValueError("unverified price amount")
        if ledger:
            if not self._ledger_evidence_syntax(evidence):
                raise ValueError("invalid ledger evidence")
        elif not self._evidence("news", evidence):
            raise ValueError("invalid evidence")
        resolution = event.get("resolution")
        if resolution is not None:
            if (not isinstance(resolution, dict) or set(resolution) != {"kind", "targetId", "evidence"}
                    or resolution["kind"] not in ("corrected", "cancelled")
                    or not isinstance(resolution["targetId"], str)
                    or not _NEWS_ID_RE.match(resolution["targetId"])
                    or not isinstance(resolution["evidence"], list)):
                raise ValueError("invalid resolution")
            # A resolution must cite correction/retraction wording; an arbitrary
            # targetId with unrelated evidence cannot retire a public fact.
            if not any(isinstance(item, dict) and news_ledger.has_correction_marker(item.get("quote"))
                       for item in resolution["evidence"]):
                raise ValueError("resolution lacks correction evidence")
            if ledger:
                if not self._ledger_evidence_syntax(resolution["evidence"]):
                    raise ValueError("invalid resolution evidence")
            elif not self._evidence("news", resolution["evidence"]):
                raise ValueError("invalid resolution evidence")

    def _validate_news_ledger(self, records):
        """Symmetric dump/load validation of the bounded acyclic correction graph."""
        if not isinstance(records, list) or len(records) > MAX_NEWS_EVENTS:
            raise ValueError("invalid news ledger")
        index = {}
        for record in records:
            if (not isinstance(record, dict) or set(record) != _NEWS_LEDGER_KEYS
                    or not isinstance(record["id"], str) or not _NEWS_ID_RE.match(record["id"])
                    or record["id"] in index):
                raise ValueError("invalid ledger record")
            index[record["id"]] = record
            if record["kind"] != "inferred" or record["status"] not in ("active", "corrected", "cancelled"):
                raise ValueError("invalid news provenance")
            witness = record["witness"]
            if witness is not None and (not isinstance(witness, str) or len(witness) != 64
                                        or any(c not in "0123456789abcdef" for c in witness)):
                raise ValueError("invalid news witness")
            self._validate_news_event(record, {"resources": [record.get("resource")],
                                               "width": 41, "height": 32}, ledger=True)
            if news_ledger.record_id(record) != record["id"]:
                raise ValueError("unstable news identity")
            if witness is not None and witness != _news_witness(record):
                raise ValueError("tampered news evidence")
            rounds = [self.memories["news"].origins.get(item["sourceId"], {}).get("first_round")
                      for item in record["evidence"]]
            if any(type(value) is not int for value in rounds) or record["sourceRound"] != min(rounds):
                raise ValueError("invalid news source round")
        incoming = {record["id"]: [] for record in records}
        for record in records:
            resolution = record["resolution"]
            if resolution is None:
                continue
            target = index.get(resolution["targetId"])
            if target is None or target["id"] == record["id"]:
                raise ValueError("dangling news resolution")
            if target["resource"] != record["resource"] or not self._news_overlap(target, record):
                raise ValueError("unrelated news resolution")
            if self._news_reaches(index, target["id"], record["id"]):
                raise ValueError("cyclic news resolution")
            incoming[target["id"]].append(resolution["kind"])
        for record in records:
            # Several bounded corrections may point at one fact; status is only
            # the audit label those incoming edges imply.  A resolver need not
            # itself stay active (a later correction may cover it).
            if record["status"] != self._derive_status(incoming[record["id"]]):
                raise ValueError("inconsistent news status")

    def _validate_news_gaps(self, gaps):
        if not isinstance(gaps, list) or len(gaps) > MAX_NEWS_GAPS:
            raise ValueError("invalid news gaps")
        seen = set()
        for gap in gaps:
            if (not isinstance(gap, dict) or set(gap) != _GAP_KEYS
                    or not isinstance(gap["resource"], str) or not 0 < len(gap["resource"]) <= 80
                    or type(gap["startDay"]) is not int or type(gap["endDay"]) is not int
                    or not 1 <= gap["startDay"] <= gap["endDay"] <= 10
                    or isinstance(gap["count"], bool) or type(gap["count"]) is not int
                    or not 1 <= gap["count"] <= MAX_NEWS_EVENTS or gap["resource"] in seen
                    or not isinstance(gap["overflow"], bool)
                    or not isinstance(gap["lostIds"], list) or len(gap["lostIds"]) > GAP_LOST_LIMIT
                    or any(not isinstance(identity, str) or not _NEWS_ID_RE.match(identity)
                           for identity in gap["lostIds"])
                    or len(set(gap["lostIds"])) != len(gap["lostIds"])):
                raise ValueError("invalid news gap")
            seen.add(gap["resource"])

    def _validate_value(self, owner, value, link):
        if owner == "news":
            return self._validate_news_events(value, link)
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
        # Excluding an ingredient also excludes a larger offering containing
        # it. Likewise an excluded time interval cannot overlap an approved one.
        for positive in seen.get('items', set()):
            counts = Counter(json.loads(positive))
            for negative in excluded.get('items', set()):
                if all(counts[name] >= count for name, count in Counter(json.loads(negative)).items()):
                    conflicts = True
        for positive in seen.get('window', set()):
            interval = json.loads(positive)
            for negative in excluded.get('window', set()):
                forbidden = json.loads(negative)
                if interval['opensAt'] <= forbidden['closesAt'] and forbidden['opensAt'] <= interval['closesAt']:
                    conflicts = True
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

    def economic_view(self, round_no):
        """Project the shared ledger onto a trade day, without another resolver."""
        day = (round_no - 1) // 130 + 1
        view = self.news_view()
        active = {fact['id'] for fact in view['facts']
                  if fact['effectiveDays'] is not None and day in fact['effectiveDays']}
        gaps = deepcopy(view['gaps'])
        if view['gapOverflow'] or self.degraded:
            gaps.append({'resource': '*', 'startDay': 1, 'endDay': 10})
        return {'events': [deepcopy(record) for record in self.news_events if record['id'] in active],
                'conflicts': [deepcopy(entry) for entry in view['conflicts'] if day in entry['possibleDays']],
                'gaps': gaps}

    def policy_view(self, round_no):
        day = (round_no - 1) // 130 + 1
        # One unified view drives the ban, the conflict summary and the page: a
        # bounded gap (or an opaque overflow) keeps uncertainty instead of a
        # one-sided ban, and only the availability dimension can block a ban.
        view = self.news_view()
        if self.degraded:
            view = {"facts": [], "conflicts": [], "gaps": [], "gapOverflow": True}
        # An opaque overflow means we cannot tell which resources lost facts, so
        # every ban is withheld rather than manufacturing certainty.
        gapped = {gap["resource"] for gap in view["gaps"]
                  if gap["startDay"] <= day <= gap["endDay"]}
        # Only the availability dimension suppresses a ban -- definite or merely
        # possible, so a shown possible contradiction is never consumed as a
        # one-sided ban.  Price/direction conflicts leave an agreed outage.
        suppressed = {(entry["resource"], conflict_day)
                      for entry in view["conflicts"] if "availability" in entry["dimensions"]
                      for conflict_day in entry["possibleDays"]}
        unavailable = []
        if not view["gapOverflow"]:
            for resource in sorted({fact["resource"] for fact in view["facts"]}):
                facts = [fact for fact in view["facts"] if fact["resource"] == resource
                         and fact["effectiveDays"] is not None and day in fact["effectiveDays"]]
                if not facts or resource in gapped or (resource, day) in suppressed:
                    continue
                # A price-only fact makes no availability assertion. Its unknown
                # value neither refutes another outage nor creates one itself.
                known_availability = [fact["availability"] for fact in facts
                                      if fact["availability"] != "unknown"]
                if known_availability and all(value == "unavailable" for value in known_availability):
                    unavailable.append(resource)  # price/direction conflicts never cancel this
        conflicts = [{**entry, "day": day} for entry in view["conflicts"]
                     if day in entry["possibleDays"]]
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
                "conflicts": conflicts if not self.degraded else [],
                "treasure": notes, "interpretation": "model_inference_with_public_evidence"}

    def dump(self):
        raw = {'schema': SCHEMA, **{k: deepcopy(v) for k, v in self.__dict__.items() if k != 'memories'},
               'memories': {owner: memory.dump() for owner, memory in self.memories.items()}}
        # Derived, non-input view so the page and the prompt read exactly the
        # validity/conflict computation the policy consumer uses.
        raw['news_view'] = self.news_view()
        return raw

    @classmethod
    def load(cls, raw):
        result = cls()
        migrated = False
        try:
            if isinstance(raw, dict) and raw.get("schema") in PREV_SCHEMAS:
                raw = cls._migrate(raw)
                migrated = True
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
            if not migrated and raw["news_view"] != result.news_view():
                raise ValueError("inconsistent news view")
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            result = cls()
            result.degraded = True
        return result

    @staticmethod
    def _migrate(raw):
        """Upgrade a v2 six-field archive or a v4 ledger into this schema.

        Ids, inferred labels and statuses are recomputed from typed fields.  A
        v2 archive never had a witness, so none is invented; a migrated gap
        keeps unverifiable loss metadata opaque instead of pretending it can be
        recovered.  A rejected upgrade still degrades instead of refreshing the
        shared ordinary quota.
        """
        upgraded = deepcopy(raw)
        previous = upgraded.get("schema")
        upgraded["schema"] = SCHEMA
        upgraded["news_gap_overflow"] = False
        upgraded["news_view"] = None
        if previous == "competition-world-agent/2":
            upgraded["news_gaps"] = []
            memories = upgraded.get("memories")
            origins = {}
            if isinstance(memories, dict) and isinstance(memories.get("news"), dict):
                candidate = memories["news"].get("origins")
                if isinstance(candidate, dict):
                    origins = candidate
            events = upgraded.get("news_events")
            if isinstance(events, list):
                upgraded["news_events"] = [
                    _canonical_news_event(event, origins, bind_witness=False)
                    if isinstance(event, dict) else event
                    for event in events]
        else:
            gaps = upgraded.get("news_gaps")
            if isinstance(gaps, list):
                # A v4 gap did not record which ids were lost; mark it opaque so
                # recovery cannot clear it and manufacture certainty.
                upgraded["news_gaps"] = [
                    {**gap, "lostIds": [], "overflow": True} if isinstance(gap, dict) else gap
                    for gap in gaps]
        return upgraded

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
        self._validate_news_gaps(self.news_gaps)
        if type(self.news_gap_overflow) is not bool:
            raise ValueError('invalid news gap overflow marker')
        # Stored facts must not bypass validation merely by being JSON: ledger
        # ids, sources and correction pairs are re-derived and cross-checked.
        self._validate_news_ledger(self.news_events)
        if self.hypothesis is not None:
            self._validate_value("treasure", self.hypothesis, {"resources": [], "width": 41, "height": 32})
        for owner, draft in self.drafts.items():
            if draft is None:
                continue
            if len(json.dumps(draft, ensure_ascii=False)) > DRAFT_LIMIT:
                raise ValueError('draft too large')
            if owner == 'news':
                resources = [e.get('resource') for e in draft if isinstance(e, dict)] if isinstance(draft, list) else []
                self._validate_news_events(draft, {'resources': resources, 'width': 41, 'height': 32})
            else:
                self._validate_value(owner, draft, {'resources': [], 'width': 41, 'height': 32})
        if self.direct is not None:
            # Rebuild exact-parser notes from an actual retained public source.
            candidates = [exact_notes(r["text"])
                          for r in self.sources["treasure"] if not r["truncated"]]
            fields = ("site", "items", "opensAt", "closesAt")
            if not isinstance(self.direct, dict) or not any(
                    c and c.get("known") and all(c.get(k) == self.direct.get(k) for k in fields) for c in candidates):
                raise ValueError("invalid direct interpretation")
