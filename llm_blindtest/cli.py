import argparse
import json
from pathlib import Path
import sys

from .core import (BlindtestError, ballot, collect, load_provider, read_json, reveal,
                   select_models, submit, verify_public, write_json, family)


def main():
    parser = argparse.ArgumentParser(description="pi hub 模型身份盲测；不等同于来源验真")
    parser.add_argument("--config", default="~/.pi/agent/models.json")
    parser.add_argument("--provider", default="hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("models", help="列出 pi 配置中的模型标签，不调用网关")
    p = sub.add_parser("collect", help="匿名采样并保存映射承诺")
    p.add_argument("run", type=Path)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--models", nargs="+")
    g.add_argument("--per-family", action="store_true", help="每个家族取配置中的第一个模型")
    p.add_argument("--prompts", type=Path, default=Path(__file__).with_name("prompts.json"))
    p.add_argument("--limit-prompts", type=int)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--temperature", type=float)
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--dry-run", action="store_true")
    for name in ["view", "ballot", "submit", "reveal", "status"]:
        p = sub.add_parser(name)
        p.add_argument("run", type=Path)
        if name == "ballot":
            p.add_argument("--output", type=Path, required=True)
        if name == "submit":
            p.add_argument("--file", type=Path, required=True)
    a = parser.parse_args()
    try:
        if a.command == "models":
            for m in load_provider(a.config, a.provider)["models"]:
                print(f"{family(m['id']):14} {m['id']}")
        elif a.command == "collect":
            provider = load_provider(a.config, a.provider)
            models = select_models(provider, a.models, a.per_family)
            prompts = read_json(a.prompts)
            if a.limit_prompts is not None:
                if a.limit_prompts < 1:
                    raise BlindtestError("limit-prompts 必须为正数")
                prompts = prompts[:a.limit_prompts]
            if a.repeats < 1 or a.max_tokens < 1 or a.timeout <= 0:
                raise BlindtestError("repeats、max-tokens、timeout 必须为正数")
            if a.temperature is not None and not 0 <= a.temperature <= 2:
                raise BlindtestError("temperature 必须在 0 到 2 之间")
            n = len(models)*len(prompts)*a.repeats
            print(f"模型 {len(models)} · 题目 {len(prompts)} · 请求 {n} · 输出 token 配额上限 {n*a.max_tokens}")
            if not a.dry_run:
                r = collect(a.run, provider, models, prompts, a.repeats, a.max_tokens,
                            a.temperature, a.timeout, progress=lambda s: print(s, flush=True))
                print(f"采样完成：{r['successful_requests']}/{n} 有效。用 view 查看匿名答卷。")
        elif a.command == "view":
            _, p = verify_public(a.run)
            print("候选家族：" + ", ".join(p["candidate_families"]))
            for e in p["endpoints"]:
                print(f"\n## {e['alias']}")
                for s in e["samples"]:
                    print(f"\n[{s['prompt_id']} / {s['repeat']}] {s['prompt']}\n{s.get('answer', s.get('error'))}")
        elif a.command == "ballot":
            if a.output.exists():
                raise BlindtestError("答题文件已存在，拒绝覆盖")
            write_json(a.output, ballot(a.run))
            print(f"已生成 {a.output}")
        elif a.command == "submit":
            r = submit(a.run, read_json(a.file))
            print("猜测已锁定，SHA-256：" + r["submission_digest"])
        elif a.command == "reveal":
            reveal(a.run)
            print((a.run / "report.md").read_text())
        elif a.command == "status":
            print(json.dumps(read_json(a.run / "manifest.json"), ensure_ascii=False, indent=2))
    except (BlindtestError, OSError, ValueError, KeyError, TypeError) as e:
        if isinstance(e, BlindtestError):
            print(f"错误：{e}", file=sys.stderr)
        else:
            print(f"错误：无法处理文件或数据（{type(e).__name__}）", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
