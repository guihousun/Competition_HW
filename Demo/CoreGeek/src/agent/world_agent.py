"""Public news and cross-day treasure reasoning using the shared LLM router.

R01/R06/R07; no simulator-private facts, future publications or model execution.
Model interpretations remain labelled inferences with verbatim source evidence.
"""
from copy import deepcopy
import hashlib
import json

from .task_agent import _json_unique
from .treasure import notes_from_news, _RUMOUR

SCHEMA = "competition-world-agent/1"
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
            identity = sha(text)
            if not any(r["id"] == identity for r in self.sources[owner]):
                self.sources[owner].append({"id": identity, "firstRound": round_no,
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
            count = self.attempts[owner]["emitted"]
            if count >= 2:
                self.status[owner] = "retry_limit"
                continue
            token = sha(owner + version + str(count))[:24]
            prompt = self._prompt(owner, token, resources, info)
            request = router.offer(owner, version, version, kind="prompt", payload=prompt,
                                   nonce=token, expected_result_shape="plan_json",
                                   operation_id=token, priority=25 if owner == "treasure" else 30)
            if request is None:
                self.status[owner] = "proposal_rejected"
                continue
            self.links[owner] = {"request_id": request.request_id, "token": token, "version": version,
                                 "resources": resources, "width": info.get("width", 41),
                                 "height": info.get("height", 32), "acknowledged": False}
            self.status[owner] = "queued"

    def _prompt(self, owner, token, resources, info):
        records = self.sources[owner]
        cap = min(TEXT_LIMIT, 7600 // max(1, len(records)))
        view = [{**r, "text": r["text"][:cap],
                 "truncated": r["truncated"] or len(r["text"]) > cap} for r in records]
        common = (
            '根据公开资料做游戏决策辅助，只返回JSON，不返回角色动作、Shell或隐藏思维链。'
            '所有结论均是推断，证据必须引用sourceId和该来源中逐字连续的quote。'
            '每130回合一天，白天前70回合；第d天第一回合=(d-1)*130+1。'
            '相对日期以资料firstRound所在日为首次观测基准，不随重读时间变化。'
            '资料截断、冲突或缺失时保留未知，不能猜测未提供条件。\n'
            f'request_id必须为{token}。\n')
        if owner == "news":
            schema = ('返回{"request_id":"' + token + '","events":[{'
                      '"resource":"公开矿名","availability":"available|unavailable|unknown",'
                      '"startDay":1,"endDay":2,"priceDirection":"up|down|unchanged|unknown",'
                      '"evidence":[{"sourceId":"来源id","quote":"原文"}]}]}。'
                      '最多8条事件，日期在1..10且含首尾。普通价格涨幅未知就不能生成价格数字。'
                      '综合更正消息，不把互相矛盾的事件当成确定结论。允许空events。'
                      '\n公开矿名：' + json.dumps(resources, ensure_ascii=False))
        else:
            schema = ('返回{"request_id":"' + token + '","hypothesis":{'
                      '"site":null,"items":null,"opensAt":null,"closesAt":null,'
                      '"uncertain":true,"evidence":{"site":[],"items":[],"window":[]}}}。'
                      '已知site填写{"x":整数,"y":整数}，items是精确物品名数组（保留重数），'
                      'opensAt/closesAt是含首尾的绝对回合。每个已知部分的证据是'
                      '[{"sourceId":"来源id","quote":"原文"}]，缺失部分保留null。'
                      '只有所有条件完整且无冲突才设uncertain=false。合并此前各日同一祭坛的资料，'
                      '遇到更正或召唤失败应重新审视，而不是沿用旧答案。'
                      f'地图宽{info.get("width", 41)}高{info.get("height", 32)}。'
                      '\n己方公开召唤反馈：' + json.dumps(self.feedback, ensure_ascii=False))
        return common + schema + '\n公开来源：' + json.dumps(view, ensure_ascii=False)

    def _evidence(self, owner, evidence):
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            return False
        records = {r["id"]: r for r in self.sources[owner]}
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"sourceId", "quote"}:
                return False
            record = records.get(item.get("sourceId"))
            quote = item.get("quote")
            if (record is None or record["truncated"] or not isinstance(quote, str)
                    or not quote.strip() or len(quote) > 500 or quote not in record["text"]):
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
            return
        if receipt.status != "received":
            return
        self.links[owner] = None
        try:
            if (link["version"] != self.version(owner) or not isinstance(raw, str)
                    or sha(raw) != receipt.text_sha256 or len(raw) > 12000):
                raise ValueError("unavailable or obsolete receipt")
            data = _json_unique(raw)
            field = "events" if owner == "news" else "hypothesis"
            if not isinstance(data, dict) or set(data) != {"request_id", field} or data["request_id"] != link["token"]:
                raise ValueError("invalid envelope")
            if owner == "news":
                events = data[field]
                if not isinstance(events, list) or len(events) > 8:
                    raise ValueError("invalid events")
                for event in events:
                    if (not isinstance(event, dict) or set(event) != {
                            "resource", "availability", "startDay", "endDay", "priceDirection", "evidence"}
                            or not isinstance(event["resource"], str) or not 0 < len(event["resource"]) <= 80
                            or event["resource"] not in link["resources"]
                            or event["availability"] not in ("available", "unavailable", "unknown")
                            or event["priceDirection"] not in ("up", "down", "unchanged", "unknown")
                            or type(event["startDay"]) is not int or type(event["endDay"]) is not int
                            or not 1 <= event["startDay"] <= event["endDay"] <= 10
                            or not self._evidence(owner, event["evidence"])):
                        raise ValueError("invalid event")
                self.news_events = deepcopy(events)
            else:
                item = data[field]
                if not isinstance(item, dict) or set(item) != {"site", "items", "opensAt", "closesAt", "uncertain", "evidence"}:
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
                            or not self._evidence(owner, evidence["window"])):
                        raise ValueError("invalid window")
                self.hypothesis = deepcopy(item)
            self.resolved[owner] = link["version"]
            self.status[owner] = "interpreted"
        except (ValueError, TypeError, KeyError, RecursionError):
            self.status[owner] = "invalid_reply"

    def acknowledge(self, payload, router, response):
        round_no = int(payload.get("roundNo") or 1)
        pending = router.pending["prompt"]
        for owner, link in self.links.items():
            if (link and pending and not link["acknowledged"]
                    and pending.request_id == link["request_id"] and pending.sent_round == round_no
                    and response.prompt == pending.payload):
                self.attempts[owner]["emitted"] += 1
                link["acknowledged"] = True
                self.status[owner] = "waiting_model"
        for command in response.commands.values():
            if command.get("action") == "summonTreasure":
                self.last_attempt = {"round": round_no, "site": deepcopy(command.get("targetPos")),
                                     "items": deepcopy(command.get("item")), "received": False}

    def policy_view(self, round_no):
        day = (round_no - 1) // 130 + 1
        current = [e for e in self.news_events if e["startDay"] <= day <= e["endDay"]]
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
            complete = not h["uncertain"] and all(h.get(k) is not None for k in ("site", "items", "opensAt", "closesAt"))
            notes.update({k: deepcopy(h[k]) for k in ("site", "items", "opensAt", "closesAt")})
            notes["known"] = complete
        notes["taken"] = self.taken
        notes["open"] = bool(notes["known"] and notes["opensAt"] <= round_no <= notes["closesAt"])
        return {"unavailable": sorted(unavailable) if not self.degraded else [],
                "treasure": notes, "interpretation": "model_inference_with_public_evidence"}

    def dump(self):
        return {"schema": SCHEMA, **deepcopy(self.__dict__)}

    @classmethod
    def load(cls, raw):
        result = cls()
        try:
            if (not isinstance(raw, dict) or set(raw) != set(result.dump()) or raw["schema"] != SCHEMA
                    or len(json.dumps(raw, ensure_ascii=False)) > 150000
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
                        or not 0 <= count["emitted"] <= 2):
                    raise ValueError("invalid attempt count")
            # Restore structure, then validate all conclusions via the same
            # schema/evidence gate used for live results below.
            for key in result.__dict__:
                setattr(result, key, deepcopy(raw[key]))
            result._validate_restored()
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            result = cls()
            result.degraded = True
        return result

    def _validate_restored(self):
        from types import SimpleNamespace
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
                    "request_id", "token", "version", "resources", "width", "height", "acknowledged"}
                    or any(not isinstance(link[k], str) or len(link[k]) > 160 for k in ("request_id", "token", "version"))
                    or not isinstance(link["resources"], list) or len(link["resources"]) > 32
                    or any(not isinstance(v, str) or len(v) > 80 for v in link["resources"])
                    or type(link["width"]) is not int or type(link["height"]) is not int
                    or not 1 <= link["width"] <= 200 or not 1 <= link["height"] <= 200
                    or type(link["acknowledged"]) is not bool):
                raise ValueError("invalid request link")
        # Stored candidates must not bypass validation merely by being JSON.
        for owner, value in (("news", self.news_events), ("treasure", self.hypothesis)):
            if owner == "treasure" and value is None:
                continue
            probe = WorldAgent()
            probe.sources = deepcopy(self.sources)
            probe.feedback = deepcopy(self.feedback)
            resources = [e.get("resource") for e in value if isinstance(e, dict)] if owner == "news" and isinstance(value, list) else []
            token = "restore-check"
            probe.links[owner] = {"request_id": token, "token": token, "version": probe.version(owner),
                                  "resources": resources, "width": 41, "height": 32}
            text = json.dumps({"request_id": token, "events" if owner == "news" else "hypothesis": value}, ensure_ascii=False)
            probe.consume(SimpleNamespace(owner=owner, request_id=token),
                          SimpleNamespace(status="received", text_sha256=sha(text)), text)
            if probe.status[owner] != "interpreted":
                raise ValueError("invalid stored interpretation")
        if self.direct is not None:
            # Rebuild exact-parser notes from an actual retained public source.
            candidates = [exact_notes(r["text"])
                          for r in self.sources["treasure"] if not r["truncated"]]
            fields = ("site", "items", "opensAt", "closesAt")
            if not isinstance(self.direct, dict) or not any(
                    c and c.get("known") and all(c.get(k) == self.direct.get(k) for k in fields) for c in candidates):
                raise ValueError("invalid direct interpretation")
