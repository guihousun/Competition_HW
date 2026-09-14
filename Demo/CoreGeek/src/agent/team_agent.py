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
from .task_context import public_task_confirmed, build_context
from .sandbox import parse_command_result
from .tasks import Plan

SCHEMA = "competition-team-agent/1"
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

    def consume(self, request, receipt, raw):
        """Called once by planner ingestion, including when defence pauses work."""
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
            # A successful query can teach a method only against a document
            # which actually names that program. Never cache its output/answer.
            if len(argv) >= 2 and argv[0] in ("python", "python3"):
                for doc in reversed(self.evidence[:-1]):
                    if doc.get("path") and argv[1] in doc["text"]:
                        self.skills.learn(doc, event)
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
        self.task.feedback(payload.get("errors", []), round_no=context.round_no)
        # Explicit stop/wait must not fall through to the legacy answer solver.
        envelope = build_context("task", confirmation.generation, confirmation.source_digest,
                                 task_text=context.phase_task,
                                 answer_contract={"requirement": "遵守题目原文指定的格式、字段与单位"})
        state.ensure_task_context().put(envelope)
        parts = [envelope.prompt_view(4200)]
        evidence_ids = ["task:" + confirmation.generation]
        candidates = self.skills.candidates(context.phase_task)
        if candidates:
            parts.append("同类方法的候选文档，必须重新读取核验：" + json.dumps(candidates, ensure_ascii=False))
        for event in self.evidence[-3:]:
            evidence_ids.append(event["id"])
            view = {k: event[k] for k in ("id", "command", "status", "exit_code", "sha256", "truncated")}
            view["text"] = event["text"][:800]
            view["truncated"] = view["truncated"] or len(event["text"]) > 800
            parts.append("关联工具证据（截断时须缩小查询，不能当完整结果）：" + json.dumps(view, ensure_ascii=False))
            if event.get("path"):
                for candidate in candidates:
                    hint = self.skills.hint(candidate["id"], event)
                    if hint:
                        parts.append("已核验文档的方法提示：" + json.dumps(hint, ensure_ascii=False))
        text = "\n".join(parts)
        if len(text) > 10000:
            self.task.finish("context_budget_exceeded", stopped=True)
            return Plan("wait", purpose="上下文过大，停止本次解题尝试")
        proposal = self.task.decide(text, evidence_ids, active=True, round_no=context.round_no)
        if proposal is None:
            return Plan("wait", purpose="Agent: " + (self.task.stop_reason or self.task.stage))
        if proposal["kind"] == "submit":
            return Plan("submit", answer=proposal["payload"], purpose=proposal["purpose"])
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

    def acknowledge(self, payload, state, response):
        """Only the final arbiter's actual response counts as an operation."""
        round_no = int(payload.get("roundNo") or 0)
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
                "link": deepcopy(self.link), "degraded": self.degraded}

    def summary(self):
        return {"stage": self.task.stage, "generation": self.task.generation,
                "prompts": self.task.prompts, "commands": self.task.commands,
                "answers": self.task.answers, "evidenceCount": len(self.evidence),
                "methodCount": len(self.skills.entries), "stopReason": self.task.stop_reason,
                "degraded": self.degraded}

    @classmethod
    def load(cls, raw, owner):
        result = cls(owner)
        try:
            if (not isinstance(raw, dict) or set(raw) != set(result.dump())
                    or raw["schema"] != SCHEMA or raw["owner"] != owner
                    or type(raw["degraded"]) is not bool
                    or len(json.dumps(raw, ensure_ascii=False)) > 220000):
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
                        or not isinstance(item.get("command"), str) or len(item["command"]) > 2000
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
        except (ValueError, TypeError, KeyError, RecursionError):
            result.degraded = True
            result.task.finish("state_restore_rejected", stopped=True)
        return result
