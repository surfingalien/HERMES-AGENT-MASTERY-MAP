"""Batch processing (#99): run many prompts in parallel and save trajectories.

Input: JSONL with ``{"prompt": "...", "id": "optional"}`` per line (or a .txt
with one prompt per line). Output directory gets ``results.jsonl`` (answers +
usage), ``trajectories.jsonl`` (ShareGPT-style conversations, handy for
evals and fine-tuning data) and ``summary.json``.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path


def _load(path: Path) -> list[dict]:
    items = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        if path.suffix == ".jsonl":
            obj = json.loads(line)
            items.append({"id": str(obj.get("id", i)), "prompt": obj["prompt"]})
        else:
            items.append({"id": str(i), "prompt": line})
    return items


def to_sharegpt(messages: list[dict]) -> list[dict]:
    role_map = {"system": "system", "user": "human", "assistant": "gpt", "tool": "tool"}
    out = []
    for m in messages:
        value = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
        if m.get("tool_calls"):
            value = (value or "") + "\n" + "\n".join(
                f"<tool_call>{json.dumps({'name': tc['function']['name'], 'arguments': tc['function']['arguments']})}</tool_call>"
                for tc in m["tool_calls"])
        out.append({"from": role_map.get(m.get("role"), m.get("role")), "value": value or ""})
    return out


def run_batch(cfg: dict, input_path: str, output_dir: str, workers: int = 4, toolsets: list[str] | None = None,
              model: str | None = None, agent_factory=None) -> dict:
    if agent_factory is None:
        from hermes.agent import AIAgent

        agent_factory = AIAgent
    items = _load(Path(input_path))
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def work(item: dict) -> dict:
        t0 = time.time()
        try:
            agent = agent_factory(config=cfg, platform="batch", toolsets=toolsets or ["hermes-cron"], model=model,
                                  approval_mode="deny-dangerous", skip_memory=True)
            res = agent.run_conversation(item["prompt"])
            agent.close()
            return {"id": item["id"], "prompt": item["prompt"], "response": res["final_response"], "ok": not res.get("error"),
                    "turns": res["turns"], "tool_calls": res["tool_calls"], "input_tokens": res["usage"].input_tokens,
                    "output_tokens": res["usage"].output_tokens, "cost_usd": round(res["cost_usd"], 6),
                    "seconds": round(time.time() - t0, 2), "conversations": to_sharegpt(res["messages"])}
        except Exception as exc:
            return {"id": item["id"], "prompt": item["prompt"], "response": "", "ok": False, "error": str(exc), "conversations": []}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, items))
    with open(out / "results.jsonl", "w") as r, open(out / "trajectories.jsonl", "w") as t:
        for res in results:
            conv = res.pop("conversations", [])
            r.write(json.dumps(res) + "\n")
            t.write(json.dumps({"id": res["id"], "conversations": conv}) + "\n")
    summary = {"items": len(results), "ok": sum(r["ok"] for r in results), "seconds": round(time.time() - started, 2),
               "input_tokens": sum(r.get("input_tokens", 0) for r in results), "output_tokens": sum(r.get("output_tokens", 0) for r in results),
               "cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 6), "output_dir": str(out)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
