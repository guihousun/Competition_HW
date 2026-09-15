"""Team cognitive coordinator; model/tool execution stays in the platform.

R01/R07. Owns task memory and conditional methods, not another quota counter.
Only router-associated receipts enter this memory. Ordinary roles remain under
the deterministic action planner. News/treasure owners use the same router.
"""
from copy import deepcopy
import hashlib
import json
import shlex

from .task_agent import TaskAgent
from .task_skills import SkillLibrary
from .task_context import public_task_confirmed, build_context, COMMAND_LIMIT
from .task_workspace import bootstrap
from . import task_tools
from . import task_answer_contract
from .sandbox import parse_command_result
from .tasks import Plan
from .world_agent import WorldAgent
from .evidence_memory import EvidenceMemory

SCHEMA = "competition-team-agent/4"
EVIDENCE_LIMIT = 8
TEXT_LIMIT = 6000


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TeamAgent:
    def __init__(self, owner):
        self.owner = owner
        self.task = TaskAgent()
        self.skills = SkillLibrary(owner)
        self.evidence = []
        self.link = None
        self.degraded = False
        self.world = WorldAgent()
        self.memory = EvidenceMemory(owner)
        self.focus = None
        self.http_profiles = []

    def consume(self, request, receipt, raw):
        """Called once by planner ingestion, including when defence pauses work."""
        if request is not None and request.owner in ("news", "treasure"):
            self.world.consume(request, receipt, raw)
            return
        link = self.link
        if (not link or request is None or request.owner != "task"
                or request.request_id != link["request_id"]
                or request.generation != self.task.generation):
            return
        if receipt.status in ("expired", "rejected"):
            self.task.failed(link["token"], receipt.reason, round_no=receipt.round_no)
            if receipt.reason == "official_quota_error":
                self.task.finish("official_quota_error", stopped=True)
            self.link = None
            return
        if receipt.status != "received":
            return
        if not isinstance(raw, str) or digest(raw) != receipt.text_sha256:
            self.task.failed(link["token"], "associated_raw_receipt_unavailable", round_no=receipt.round_no)
            self.link = None
            return
        if request.kind == "cmd":
            result = parse_command_result(raw)
            self.focus = None
            self.memory.put(request.request_id, result.output, label=request.payload,
                            upstream_truncated=result.truncated)
            event = {"id": request.request_id, "generation": request.generation,
                     "command": request.payload, "round": receipt.round_no,
                     "verified": True, "status": result.status,
                     "exit_code": result.exit_code,
                     "text": result.output[:TEXT_LIMIT], "sha256": digest(result.output),
                     "truncated": result.truncated or len(result.output) > TEXT_LIMIT}
            try:
                argv = shlex.split(request.payload)
            except ValueError:
                argv = []
            if len(argv) == 2 and argv[0] == "cat" and argv[1].startswith("/"):
                event["path"] = argv[1]
            self.evidence = (self.evidence + [event])[-EVIDENCE_LIMIT:]
            http_request = task_tools.http_request(request.payload)
            if http_request and result.exit_code == 0 and not result.truncated:
                try:
                    wrapper = json.loads(result.output)
                    requested_endpoint = task_tools.endpoint(http_request['url'])
                    if (wrapper.get('tool') == 'task-http/1'
                            and (wrapper.get('status') in (400,401,403) or wrapper.get('error'))):
                        self.http_profiles = [p for p in self.http_profiles
                                              if p['profile']['endpoint'] != requested_endpoint]
                    profile = task_tools.clean_profile(wrapper.get('profile'))
                    if (wrapper.get('tool') == 'task-http/1' and wrapper.get('status') == 200
                            and wrapper.get('truncated') is False
                            and profile['endpoint'] == task_tools.endpoint(http_request['url'])):
                        seen_paths = set()
                        for doc in reversed(self.evidence[:-1]):
                            if doc.get('path') in seen_paths:
                                continue
                            if doc.get('path'):
                                seen_paths.add(doc['path'])
                            full = self._full_document(doc)
                            if (full.get('path') and not full['truncated'] and full.get('exit_code') == 0
                                    and profile['endpoint'] in full['text']):
                                entry = {'document_path': full['path'], 'document_sha256': digest(full['text']),
                                         'profile': profile}
                                for old in self.http_profiles:
                                    if (old['document_path'] == entry['document_path']
                                            and old['document_sha256'] == entry['document_sha256']
                                            and old['profile']['endpoint'] == profile['endpoint']):
                                        combined = {**old['profile']['aliases'], **profile['aliases']}
                                        if len(combined) <= 2:
                                            profile['aliases'] = combined
                                self.http_profiles = [p for p in self.http_profiles if
                                    (p['document_path'], p['profile']['endpoint']) !=
                                    (entry['document_path'], profile['endpoint'])]
                                self.http_profiles = (self.http_profiles + [entry])[-8:]
                                break
                except (ValueError, TypeError, AttributeError):
                    pass
            if request.payload.startswith('# task-workspace/1\n') and result.exit_code == 0:
                # The actual correlated probe receipt, not a model-created path.
                try:
                    probe = json.loads(result.output)
                    docs = probe.get('files', [])
                    if (probe.get('probe') == 'task-workspace/1' and probe.get('status') == 'found'
                            and isinstance(docs, list) and len(docs) <= 4):
                        for index, doc in enumerate(docs):
                            if (not isinstance(doc, dict) or not isinstance(doc.get('path'), str)
                                    or not doc['path'].startswith('/') or len(doc['path']) > 500
                                    or not isinstance(doc.get('text'), str) or len(doc['text']) > TEXT_LIMIT
                                    or type(doc.get('truncated')) is not bool):
                                continue
                            identity = request.request_id + ':' + str(index)
                            cut = result.truncated or doc['truncated']
                            self.memory.put(identity, doc['text'], label=doc['path'], pinned=index == 0, upstream_truncated=cut)
                            self.evidence.append({**event, 'id': identity, 'path': doc['path'],
                                'text': doc['text'], 'sha256': digest(doc['text']), 'truncated': cut})
                        self.evidence = self.evidence[-EVIDENCE_LIMIT:]
                except (ValueError, TypeError, AttributeError):
                    pass  # raw receipt remains available even if probe JSON is incomplete
            # A successful query can teach a method only against a document
            # which actually names that program. Never cache its output/answer.
            if len(argv) >= 2 and argv[0] in ("python", "python3"):
                for doc in reversed(self.evidence[:-1]):
                    full = self._full_document(doc)
                    if full.get("path") and argv[1] in full["text"]:
                        self.skills.learn(full, event)
                        break
            # TaskAgent only needs a bounded event; full retained evidence and
            # explicit loss markers are independently assembled into its prompt.
            delivered = raw if len(raw) <= 16000 else (
                raw[:12000] + "\n[TRUNCATED]\n本地控制器未保留全部输出，请缩小查询范围。")
        else:
            delivered = raw  # oversized model plans are rejected, never chopped
        self.task.receive(link["token"], request.kind, delivered,
                          round_no=receipt.round_no, verified=True)
        self.link = None

    def solve(self, payload, state, context):
        confirmation = public_task_confirmed(payload, context.cycle)
        if self.degraded:
            return Plan("wait", purpose="Agent存档损坏，保持任务等待")
        if not confirmation.confirmed:
            self.task.finish("task_not_confirmed")
            return Plan("wait", purpose="等待公开任务确认")
        changed = self.task.generation != confirmation.generation
        if not self.task.begin(confirmation.generation, confirmation.source_digest):
            return Plan("wait", purpose="Agent不可恢复，保持任务")
        if changed:
            self.evidence, self.link = [], None
            self.memory = EvidenceMemory(self.owner, confirmation.generation)
            self.focus = None
        task_id = "task:" + confirmation.generation
        self.memory.put(task_id, context.phase_task, label="当前任务原文", pinned=True)
        self.task.feedback(payload.get("errors", []), round_no=context.round_no)
        operation = self.task.proposal
        if operation is not None and operation["kind"] == "inspect":
            request = json.loads(operation["payload"])
            result = self.memory.inspect(request.pop("source_id"), **request)
            if self.task.inspected(operation["token"], result, round_no=context.round_no):
                self.focus = result
        # Explicit stop/wait must not fall through to the legacy answer solver.
        task_document = self.memory.document(task_id)
        contract_documents = [
            {'id': doc['id'], 'path': doc['label'], 'text': doc['text'],
             'truncated': not self.memory.complete(doc), 'exit_code': 0, 'verified': True}
            for doc in self.memory.records if doc['label'].startswith('/')]
        contract_documents += [self._full_document(event) for event in self.evidence]
        contract = task_answer_contract.for_task(context.phase_task, contract_documents)
        references = ([{"kind": "agent_memory", "name": task_id,
                        "sha256": task_document["received_sha256"]}]
                      if task_document and self.memory.complete(task_document) else [])
        envelope = build_context("task", confirmation.generation, confirmation.source_digest,
                                 task_text=context.phase_task,
                                 answer_contract={**(contract or {}), "requirement": "遵守题目原文指定的格式、字段与单位"},
                                 trace_refs=references)
        state.ensure_task_context().put(envelope)
        parts = ["本题原文记忆索引（inspect只能访问这些已收到的来源）：" + json.dumps(self.memory.index(), ensure_ascii=False)]
        if contract:
            parts.append('本题提交契约（来自原题；示例数值/占位符不是答案）：' + json.dumps(contract, ensure_ascii=False))
        parts.append('任务时间预算：' + json.dumps({
            'current_round': context.round_no,
            'accepted_round': context.cycle.accepted_round,
            'deadline_exclusive': context.cycle.deadline,
            'rounds_remaining': context.cycle.rounds_left(context.round_no),
            'timeout_known': context.cycle.deadline is not None,
            'submission_buffer': 2}, ensure_ascii=False)
            + '。每次模型请求与沙盒命令均要等待后续回合。优先合并必要操作；'
              '预留2轮提交缓冲，有符合题意的答案立即提交；剩余轮数未知时不能假定15轮。')
        if self.focus:
            focus = json.dumps(self.focus, ensure_ascii=False)
            if len(focus) <= 4000:
                parts.append("最近本地原文检索结果：" + focus)
            else:
                parts.append("本地检索结果转义后过长，请用更短length重新检索。")
        parts.append(envelope.prompt_view(1600 if self.focus else 2600))
        evidence_ids = [row["id"] for row in self.memory.index()]
        def append_context(text):
            if sum(len(p) + 1 for p in parts) + len(text) <= 7500:
                parts.append(text)
        candidates = self.skills.candidates(context.phase_task)
        if candidates:
            append_context("同类方法的候选文档，必须重新读取核验：" + json.dumps(candidates, ensure_ascii=False))
        for profile in self.http_profiles:
            for doc in reversed(self.evidence):
                full = self._full_document(doc)
                if full.get('path') == profile['document_path']:
                    if (not full['truncated'] and full.get('exit_code') == 0
                            and digest(full['text']) == profile['document_sha256']):
                        append_context('同文档重新核验的HTTP方法（只有传输方法，没有凭据或答案；新错误优先）：'
                                       + json.dumps(profile['profile'], ensure_ascii=False))
                    break
        # Latest actual evidence wins space over old history. Include complete
        # medium documents directly instead of forcing multiple inspect rounds.
        recent = list(reversed(self.evidence[-4:]))
        if recent and all(e["command"].startswith('# task-workspace/1\n') for e in recent):
            recent.reverse()  # probe emits task contract first, supporting files next
        for event in recent:
            view = {k: event[k] for k in ("id", "status", "exit_code", "sha256", "truncated")}
            view['command'] = event["command"][:240]
            view['command_truncated'] = len(event["command"]) > 240
            if event.get('path'):
                view['path'] = event['path']
            full = self._full_document(event)
            for cap in (4500, 3000, 1800, 800, 200):
                view["text"] = full["text"][:cap]
                view["truncated"] = full["truncated"] or len(full["text"]) > cap
                item = "关联工具原文（truncated=true时才需定位缺失片段；不要重复读取已有内容）：" + json.dumps(view, ensure_ascii=False)
                if sum(len(p) + 1 for p in parts) + len(item) <= 7500:
                    parts.append(item)
                    break
            if event.get("path"):
                for candidate in candidates:
                    hint = self.skills.hint(candidate["id"], self._full_document(event))
                    if hint:
                        append_context("已核验文档的方法提示：" + json.dumps(hint, ensure_ascii=False))
        text = "\n".join(parts)
        if len(text) > 10000:
            self.task.finish("context_budget_exceeded", stopped=True)
            return Plan("wait", purpose="上下文过大，停止本次解题尝试")
        remaining = context.cycle.rounds_left(context.round_no)
        if (remaining is not None and remaining <= 1 and not self.task.pending and self.link is None
                and (self.task.proposal is None or self.task.proposal['kind'] != 'submit')):
            # A newly issued tool/model receipt could only arrive at/after the
            # deadline. Preserve any already available submit, never guess one.
            self.task.proposal = None
            return Plan("wait", purpose="最后提交窗口没有现成答案，不再启动来不及回收的调用")
        proposal = self.task.decide(text, evidence_ids, active=True, round_no=context.round_no,
                                   bootstrap_command=bootstrap(context.phase_task))
        if proposal is None:
            return Plan("wait", purpose="Agent: " + (self.task.stop_reason or self.task.stage))
        if proposal["kind"] == "submit":
            accepted, answer, reason = task_answer_contract.validate(proposal['payload'], contract, contract_documents)
            if accepted:
                if answer != proposal['payload']:
                    self.task.proposal['payload'] = answer
                    self.task._event('answer_format_normalized', reason, context.round_no)
                return Plan("submit", answer=answer, purpose=proposal["purpose"])
            self.task.proposal = None
            self.task._event('answer_contract_rejected', reason, context.round_no)
            if remaining is not None and remaining <= 1:
                return Plan('wait', purpose='答案不符合本题结构，已无重规划窗口')
            proposal = self.task.decide(text, evidence_ids, active=True, round_no=context.round_no)
            if proposal is None:
                return Plan('wait', purpose=reason)
        request = state.ensure_llm_router().offer(
            "task", confirmation.generation, confirmation.source_digest,
            kind=proposal["kind"], payload=proposal["payload"], purpose=proposal["purpose"],
            in_task=True, priority=40,
            operation_id=proposal["token"],
            nonce=proposal["token"] if proposal["kind"] == "prompt" else "",
            expected_result_shape="plan_json" if proposal["kind"] == "prompt" else "command_output")
        if request is None:
            self.task.finish("router_rejected_proposal", stopped=True)
            return Plan("wait", purpose="通道拒绝提案，停止本次尝试")
        self.link = {"request_id": request.request_id, "token": proposal["token"]}
        return Plan("wait", purpose="Agent提案已排队，等待公共通道")

    def _full_document(self, event):
        original = self.memory.document(event['id'])
        if original and self.memory.complete(original):
            return {**event, 'text': original['text'], 'truncated': False}
        return event

    def acknowledge(self, payload, state, response):
        """Only the final arbiter's actual response counts as an operation."""
        round_no = int(payload.get("roundNo") or 0)
        self.world.acknowledge(payload, state.ensure_llm_router(), response)
        confirmation = public_task_confirmed(payload, state.tasks.get("cycle"))
        if not confirmation.confirmed:
            if self.task.generation:
                self.task.finish("task_not_confirmed")
                self.link = None
            return
        proposal = self.task.proposal
        if not proposal:
            return
        if proposal["kind"] == "submit":
            pioneer_ids = {str(u["id"]) for u in payload.get("teamOur", {}).get("roles", [])
                           if u.get("roleType") == "pioneer"}
            emitted = any(uid in pioneer_ids and command.get("action") == "submitAnswer"
                          and command.get("taskAnswer") == proposal["payload"]
                          for uid, command in response.commands.items())
        else:
            pending = state.ensure_llm_router().pending[proposal["kind"]]
            emitted = bool(self.link and pending and pending.sent_round == round_no
                           and pending.request_id == self.link["request_id"]
                           and (response.prompt if proposal["kind"] == "prompt" else response.execute) == pending.payload)
        if emitted:
            self.task.acknowledge(proposal["token"], round_no=round_no)

    def dump(self):
        return {"schema": SCHEMA, "owner": self.owner, "task": self.task.dump(),
                "skills": self.skills.dump(), "evidence": deepcopy(self.evidence),
                "link": deepcopy(self.link), "degraded": self.degraded, "world": self.world.dump(),
                "memory": self.memory.dump(), "focus": deepcopy(self.focus),
                "httpProfiles": deepcopy(self.http_profiles)}

    def summary(self):
        return {"stage": self.task.stage, "generation": self.task.generation,
                "prompts": self.task.prompts, "commands": self.task.commands,
                "answers": self.task.answers, "evidenceCount": len(self.evidence),
                "methodCount": len(self.skills.entries), "stopReason": self.task.stop_reason,
                "httpMethodCount": len(self.http_profiles),
                "degraded": self.degraded, "world": deepcopy(self.world.status),
                "memoryReads": self.task.inspections, "memorySources": len(self.memory.records)}

    @classmethod
    def load(cls, raw, owner):
        result = cls(owner)
        try:
            if isinstance(raw, dict) and raw.get('schema') == "competition-team-agent/3" and 'httpProfiles' not in raw:
                raw = {**raw, 'schema': SCHEMA, 'httpProfiles': []}
            if (not isinstance(raw, dict) or set(raw) != set(result.dump())
                    or raw["schema"] != SCHEMA or raw["owner"] != owner
                    or type(raw["degraded"]) is not bool
                    or len(json.dumps(raw, ensure_ascii=False)) > 1000000):
                raise ValueError("invalid coordinator state")
            task = TaskAgent.load(raw["task"])
            skills = SkillLibrary.load(raw["skills"], owner=owner)
            if task.stop_reason == "state_restore_rejected" or skills.degraded:
                raise ValueError("invalid subcomponent")
            evidence = raw["evidence"]
            if not isinstance(evidence, list) or len(evidence) > EVIDENCE_LIMIT:
                raise ValueError("invalid evidence list")
            for item in evidence:
                required = {"id", "generation", "command", "round", "verified", "status",
                            "exit_code", "text", "sha256", "truncated"}
                if (not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"path"}
                        or item.get("generation") != task.generation
                        or not isinstance(item.get("text"), str) or len(item["text"]) > TEXT_LIMIT
                        or not isinstance(item.get("id"), str) or len(item["id"]) > 160
                        or not isinstance(item.get("command"), str) or len(item["command"]) > COMMAND_LIMIT
                        or item.get("verified") is not True or type(item.get("truncated")) is not bool
                        or type(item.get("round")) is not int or item["round"] < 1
                        or item.get("status") not in ("exit", "empty", "timeout", "judger_error")
                        or (item.get("exit_code") is not None and type(item["exit_code"]) is not int)
                        or ("path" in item and (not isinstance(item["path"], str) or not item["path"].startswith("/")))
                        or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64
                        or (not item["truncated"] and digest(item["text"]) != item["sha256"])):
                    raise ValueError("invalid evidence")
            link = raw["link"]
            if link is not None and (not isinstance(link, dict) or set(link) != {"request_id", "token"}
                                    or any(not isinstance(v, str) or len(v) > 160 for v in link.values())):
                raise ValueError("invalid pending link")
            operation = task.pending or task.proposal
            if link is not None and (operation is None or operation.get("token") != link["token"]
                                     or operation.get("kind") not in ("prompt", "cmd")):
                raise ValueError("link is not the current controller operation")
            result.task, result.skills = task, skills
            result.evidence, result.link = deepcopy(evidence), deepcopy(link)
            result.degraded = raw["degraded"]
            result.world = WorldAgent.load(raw["world"])
            if result.world.degraded:
                raise ValueError("invalid public world memory")
            result.memory = EvidenceMemory.load(raw['memory'], owner=owner, generation=task.generation)
            if result.memory.degraded:
                raise ValueError('invalid original-text memory')
            focus = raw['focus']
            if focus is not None:
                if not isinstance(focus, dict) or len(json.dumps(focus, ensure_ascii=False)) > 16000:
                    raise ValueError('invalid focus')
                request = focus.get('request')
                if not isinstance(request, dict) or set(request) != {'offset', 'length', 'query'}:
                    raise ValueError('invalid memory request')
                if result.memory.inspect(focus.get('source_id'), **request) != focus:
                    raise ValueError('focus is not the original memory result')
            result.focus = deepcopy(focus)
            profiles = raw['httpProfiles']
            if not isinstance(profiles, list) or len(profiles) > 8:
                raise ValueError('invalid http profile count')
            for entry in profiles:
                if (not isinstance(entry, dict) or set(entry) != {'document_path','document_sha256','profile'}
                        or not isinstance(entry['document_path'], str) or not entry['document_path'].startswith('/')
                        or len(entry['document_path']) > 500 or not isinstance(entry['document_sha256'], str)
                        or len(entry['document_sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in entry['document_sha256'])):
                    raise ValueError('invalid source for http profile')
                task_tools.clean_profile(entry['profile'])
            result.http_profiles = deepcopy(profiles)
        except (ValueError, TypeError, KeyError, RecursionError):
            result.degraded = True
            result.task.finish("state_restore_rejected", stopped=True)
        return result
