import argparse
import json
from pathlib import Path
import sys

from .core import (BlindtestError, ballot, collect, load_provider, read_json, reveal,
                   select_models, submit, verify_public, write_json, family)
from .fingerprint import train_command, predict_command


def main():
    parser = argparse.ArgumentParser(prog="vestige", description="Vestige — pi hub 模型身份盲测；不等同于来源验真")
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
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--trials", type=int, default=1, help="每个模型生成多少组独立匿名答卷")
    p.add_argument("--reference", action="store_true", help="显式采集有标签参考集，不计为盲测")
    p.add_argument("--min-interval", type=float, default=0, help="所有请求开始时间的最小间隔，秒")
    p.add_argument("--fail-fast", type=int, default=0, help="连续失败N次后停止新请求；0为关闭")
    p = sub.add_parser("train", help="从参考集训练文本指纹")
    p.add_argument("reference_run", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("predict", help="仅凭匿名最终文本自动猜测，不读取映射")
    p.add_argument("run", type=Path)
    p.add_argument("--fingerprint", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--min-samples", type=int, default=6)
    p.add_argument("--expect-hash")
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
            if a.repeats < 1 or a.max_tokens < 1 or a.timeout <= 0 or not 1 <= a.workers <= 16 or a.trials < 1 or a.min_interval < 0 or a.fail_fast < 0:
                raise BlindtestError("repeats、max-tokens、timeout 必须为正数")
            if a.temperature is not None and not 0 <= a.temperature <= 2:
                raise BlindtestError("temperature 必须在 0 到 2 之间")
            n = len(models)*len(prompts)*a.repeats*a.trials
            print(f"模型 {len(models)} · 题目 {len(prompts)} · 请求 {n} · 输出 token 配额上限 {n*a.max_tokens}")
            if not a.dry_run:
                r = collect(a.run, provider, models, prompts, a.repeats, a.max_tokens,
                            a.temperature, a.timeout, progress=lambda s: print(s, flush=True),
                            workers=a.workers, trials=a.trials, reference=a.reference,
                            min_interval=a.min_interval, fail_fast=a.fail_fast)
                next_step = "用 train 训练有标签参考集。" if a.reference else "用 view 查看匿名答卷。"
                print(f"采样完成：{r['successful_requests']}/{n} 有效。{next_step}")
        elif a.command == "train":
            print(json.dumps(train_command(a.reference_run, a.output), ensure_ascii=False, indent=2))
        elif a.command == "predict":
            print(json.dumps(predict_command(a.run, a.fingerprint, a.output, a.min_samples, a.expect_hash), ensure_ascii=False, indent=2))
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
