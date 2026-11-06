from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


class BlindtestError(Exception):
    pass


SYSTEM = "你是一个有帮助的助手。直接回答用户问题，不做自我介绍，不说明自己的模型名称或开发商。"
BRANDS = ["OpenAI", "ChatGPT", "GPT", "Anthropic", "Claude", "Google", "Gemini",
          "DeepSeek", "深度求索", "GLM", "智谱", "Zhipu", "Z.ai", "Kimi", "Moonshot",
          "月之暗面", "MiniMax", "稀宇", "Meta", "Llama", "Muse", "InclusionAI",
          "Ling", "蚂蚁", "Xiaomi", "MiMo", "小米", "Qwen", "通义千问", "阿里巴巴",
          "Sonnet", "Opus", "Haiku", "K3.1", "K2.5", "M3.1"]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def encode(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def digest(data):
    return hashlib.sha256(encode(data)).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encode(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(run):
    with (Path(run) / ".lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlindtestError("该场次正在被另一个进程操作") from None
        yield


def resolve(value):
    """Match pi's command/template convention. Never print credential commands."""
    if not isinstance(value, str):
        raise BlindtestError("凭据配置必须是字符串")
    if value.startswith("!"):
        try:
            result = subprocess.run(value[1:], shell=True, capture_output=True,
                                    text=True, timeout=10, check=True)
        except (subprocess.SubprocessError, OSError):
            raise BlindtestError("无法执行 pi 已配置的凭据读取命令") from None
        return result.stdout.strip()
    def subst(m):
        if m.group(0) == "$$":
            return "$"
        if m.group(0) == "$!":
            return "!"
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise BlindtestError(f"缺少环境变量 {name}")
        return os.environ[name]
    return re.sub(r"\$\$|\$!|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", subst, value)


def family(model_id):
    parts = model_id.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] in {"zenmux", "openrouter"} else parts[0]


def load_provider(config, name="hub"):
    try:
        provider = read_json(Path(config).expanduser())["providers"][name]
    except (KeyError, OSError, ValueError):
        raise BlindtestError("无法读取指定 pi provider 配置") from None
    if provider.get("api") != "openai-completions":
        raise BlindtestError("首版仅支持 pi 的 openai-completions provider")
    if not provider.get("models"):
        raise BlindtestError("provider 未配置模型")
    return provider


def redact(text, model_ids=()):
    terms = BRANDS + list(model_ids)
    for term in sorted(set(terms), key=len, reverse=True):
        pattern = re.escape(term)
        if term.isascii():
            pattern = r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z])"
        text = re.sub(pattern, "[身份词已隐藏]", text, flags=re.I)
    return text


class Client:
    def __init__(self, provider, timeout=90):
        self.provider = provider
        self.timeout = timeout
        self.key = resolve(provider.get("apiKey", ""))

    def complete(self, model, prompt, max_tokens, temperature):
        api = model.get("api") or self.provider["api"]
        if api != "openai-completions":
            return {"ok": False, "error": "unsupported_api"}
        url = (model.get("baseUrl") or self.provider["baseUrl"]).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        for k, v in {**self.provider.get("headers", {}), **model.get("headers", {})}.items():
            headers[k] = resolve(v)
        key = resolve(model["apiKey"]) if "apiKey" in model else self.key
        if model.get("authHeader", self.provider.get("authHeader", True)) and key:
            headers["Authorization"] = "Bearer " + key
        field = model.get("compat", {}).get("maxTokensField", "max_tokens")
        if field not in {"max_tokens", "max_completion_tokens"}:
            raise BlindtestError("不支持该 maxTokensField")
        payload = {"model": model["id"], "stream": False,
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                   field: max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        start = time.monotonic()
        # No silent parameter fallback or automatic retries: each is a different experiment.
        try:
            req = urllib.request.Request(url, data=encode(payload), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                data = json.load(res)
            choice = data["choices"][0]
            content = choice["message"].get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            finish = choice.get("finish_reason")
            result = {"ok": isinstance(content, str) and bool(content.strip()) and finish == "stop",
                      "text": content if isinstance(content, str) else "",
                      "finish_reason": finish, "usage": data.get("usage", {}),
                      "reported_model": data.get("model"), "latency_seconds": round(time.monotonic() - start, 3)}
            if not result["ok"]:
                result["error"] = "truncated" if finish == "length" else "incomplete_or_empty"
            return result
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"http_{e.code}"}
        except (TimeoutError, urllib.error.URLError, OSError):
            return {"ok": False, "error": "network_or_timeout"}
        except (ValueError, KeyError, IndexError, TypeError):
            return {"ok": False, "error": "invalid_response"}


def select_models(provider, ids=None, per_family=False):
    models = provider["models"]
    if ids:
        by_id = {m["id"]: m for m in models}
        if len(set(ids)) != len(ids) or any(x not in by_id for x in ids):
            raise BlindtestError("模型列表有重复或存在未配置的模型 ID")
        return [by_id[x] for x in ids]
    if per_family:
        selected = {}
        for m in models:
            selected.setdefault(family(m["id"]), m)
        return list(selected.values())
    return list(models)


def collect(run, provider, models, prompts, repeats=1, max_tokens=2048, temperature=None,
            timeout=90, progress=print, client_factory=Client, workers=1, trials=1, reference=False,
            min_interval=0, fail_fast=0):
    run = Path(run)
    if not models or not prompts or repeats < 1 or max_tokens < 1 or workers < 1 or trials < 1 or min_interval < 0 or fail_fast < 0:
        raise BlindtestError("模型、题目不能为空，重复次数和 token 上限必须为正数")
    if len({p["id"] for p in prompts}) != len(prompts):
        raise BlindtestError("题目 ID 必须唯一")
    if run.exists():
        raise BlindtestError("场次目录已存在；请换一个新目录，避免覆盖盲测记录")
    run.mkdir(parents=True, mode=0o700)
    (run / "private").mkdir(mode=0o700)
    rng = secrets.SystemRandom()
    shuffled = list(models) * trials
    rng.shuffle(shuffled)
    mapping = {f"M-{secrets.token_hex(8).upper()}": m for m in shuffled}
    # Mapping committed before generation; no API credentials in it.
    assignments = {a: {"model": m["id"], "family": family(m["id"])} for a, m in mapping.items()}
    private = {"salt": secrets.token_hex(32), "assignments": assignments, "results": []}
    commitment = digest({"salt": private["salt"], "assignments": assignments})
    manifest = {"version": 1, "state": "collecting", "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "mapping_commitment": commitment, "endpoint_count": len(mapping), "prompt_count": len(prompts),
                "repeats": repeats, "max_tokens": max_tokens, "temperature": temperature,
                "trials": trials, "workers": workers, "purpose": "reference" if reference else "blind",
                "min_interval": min_interval, "fail_fast": fail_fast,
                "system_prompt": SYSTEM, "truth_source": "pi gateway route labels; not independently verified model identity",
                "planned_requests": len(mapping) * len(prompts) * repeats}
    write_json(run / "manifest.json", manifest)
    write_json(run / "private" / "data.json", private)
    jobs = [(a, p, r) for a in mapping for p in prompts for r in range(1, repeats + 1)]
    rng.shuffle(jobs)
    public = {"candidate_families": sorted({family(m["id"]) for m in models}),
              "candidate_models": sorted({m["id"] for m in models}), "endpoints": []}
    grouped = {a: [] for a in mapping}
    with locked(run):
        client = client_factory(provider, timeout)
        gate = threading.Lock()
        abort = threading.Event()
        next_start = 0.0
        consecutive_errors = 0
        def execute(job):
            nonlocal next_start
            alias, prompt, repeat = job
            with gate:
                if abort.is_set():
                    return job, {"ok": False, "error": "stopped_after_errors", "attempted": False}
                delay = next_start-time.monotonic()
                if delay > 0:
                    abort.wait(delay)
                if abort.is_set():
                    return job, {"ok": False, "error": "stopped_after_errors", "attempted": False}
                next_start = time.monotonic()+min_interval
            return job, {**client.complete(mapping[alias], prompt["text"], max_tokens, temperature), "attempted": True}
        def results():
            if workers == 1:
                yield from map(execute, jobs)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(execute, job) for job in jobs]
                    for future in as_completed(futures):
                        yield future.result()
        for i, ((alias, prompt, repeat), result) in enumerate(results(), 1):
            if result.get("attempted", True):
                consecutive_errors = 0 if result["ok"] else consecutive_errors + 1
                if fail_fast and consecutive_errors >= fail_fast and not abort.is_set():
                    abort.set()
                    progress(f"连续失败达到 {fail_fast} 次，停止新请求；未执行样本仍计入计划分母。")
            private["results"].append({"alias": alias, "prompt_id": prompt["id"], "repeat": repeat, **result})
            write_json(run / "private" / "data.json", private)
            sample = {"prompt_id": prompt["id"], "prompt": prompt["text"], "repeat": repeat, "ok": result["ok"]}
            if result["ok"]:
                sample["answer"] = redact(result["text"], public["candidate_models"])
            else:
                sample["error"] = "该样本不可用于判断（失败、截断或空回答）"
            grouped[alias].append(sample)
            progress(f"采样 {i}/{len(jobs)} · {'有效' if result['ok'] else '无效'}")
        for a in sorted(mapping):
            rng.shuffle(grouped[a])
            public["endpoints"].append({"alias": a, "samples": grouped[a]})
        write_json(run / "blind.json", public)
        manifest.update(state="collected", public_digest=digest(public),
                        successful_requests=sum(r["ok"] for r in private["results"]),
                        attempted_requests=sum(r.get("attempted", True) for r in private["results"]),
                        stopped_after_errors=abort.is_set())
        if reference:
            examples = [{"model": assignments[e["alias"]]["model"],
                         "family": assignments[e["alias"]]["family"],
                         "prompt_id": s["prompt_id"], "prompt": s["prompt"], "answer": s["answer"]}
                        for e in public["endpoints"] for s in e["samples"] if s["ok"]]
            dataset = {"kind": "labeled_reference", "examples": examples,
                       "settings": {k: manifest[k] for k in ["max_tokens", "temperature", "system_prompt"]},
                       "candidate_models": public["candidate_models"]}
            write_json(run / "reference.json", dataset)
            manifest.update(state="reference", reference_digest=digest(dataset))
        write_json(run / "manifest.json", manifest)
    return manifest


def verify_public(run):
    run = Path(run)
    manifest = read_json(run / "manifest.json")
    if manifest["state"] not in {"collected", "submitted", "revealed"}:
        raise BlindtestError("场次尚未完成采样；可检查 private 中的已保存结果")
    public = read_json(run / "blind.json")
    if digest(public) != manifest["public_digest"]:
        raise BlindtestError("匿名答卷已修改，无法继续评分")
    return manifest, public


def ballot(run):
    _, public = verify_public(run)
    return {"reviewer": "", "method": "", "guesses": [
        {"alias": e["alias"], "family": "unknown", "model": None, "confidence": None, "reason": ""}
        for e in public["endpoints"]]}


def submit(run, guesses):
    run = Path(run)
    with locked(run):
        manifest, public = verify_public(run)
        if manifest["state"] != "collected" or (run / "submission.json").exists():
            raise BlindtestError("猜测已经锁定，不能改写或在揭晓后重新提交")
        expected = {e["alias"] for e in public["endpoints"]}
        rows = guesses.get("guesses", [])
        if len(rows) != len(expected) or {g.get("alias") for g in rows} != expected:
            raise BlindtestError("每个匿名模型必须且只能提交一条猜测")
        for g in rows:
            if g.get("family") not in public["candidate_families"] + ["unknown"]:
                raise BlindtestError("family 必须是候选家族或 unknown")
            if g.get("model") not in public["candidate_models"] + [None, "unknown"]:
                raise BlindtestError("model 必须是候选 ID、null 或 unknown")
            if g.get("model") not in {None, "unknown"} and family(g["model"]) != g["family"]:
                raise BlindtestError("模型猜测与家族猜测不一致")
            c = g.get("confidence")
            if c is not None and (isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or not 0 <= c <= 1):
                raise BlindtestError("confidence 必须为 null 或 0 到 1 的有限数值")
        if not guesses.get("reviewer") or not guesses.get("method"):
            raise BlindtestError("请填写 reviewer 和 method")
        write_json(run / "submission.json", guesses)
        manifest.update(state="submitted", submission_digest=digest(guesses),
                        submitted_at=dt.datetime.now(dt.timezone.utc).isoformat())
        write_json(run / "manifest.json", manifest)
    return manifest


def wilson(k, n):
    if not n:
        return None
    z = 1.96
    center = (k / n + z*z / (2*n)) / (1 + z*z/n)
    half = z * math.sqrt((k/n)*(1-k/n)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return [max(0, center-half), min(1, center+half)]


def reveal(run):
    run = Path(run)
    with locked(run):
        manifest, public = verify_public(run)
        if manifest["state"] not in {"submitted", "revealed"}:
            raise BlindtestError("先提交并锁定猜测，再揭晓")
        guesses = read_json(run / "submission.json")
        if digest(guesses) != manifest["submission_digest"]:
            raise BlindtestError("已锁定的猜测被修改")
        private = read_json(run / "private" / "data.json")
        if digest({"salt": private["salt"], "assignments": private["assignments"]}) != manifest["mapping_commitment"]:
            raise BlindtestError("模型映射与采样前承诺不一致")
        by_alias = {g["alias"]: g for g in guesses["guesses"]}
        rows, confusion, counts = [], {}, {}
        for e in public["endpoints"]:
            a = e["alias"]
            truth, guess = private["assignments"][a], by_alias[a]
            valid = sum(s["ok"] for s in e["samples"])
            eligible = valid > 0
            correct = eligible and guess["family"] == truth["family"]
            rows.append({"alias": a, **truth, "guess_family": guess["family"], "guess_model": guess.get("model"),
                         "valid_samples": valid, "total_samples": len(e["samples"]), "eligible": eligible,
                         "family_correct": correct, "model_correct": eligible and guess.get("model") == truth["model"]})
            if eligible:
                counts[truth["family"]] = counts.get(truth["family"], 0) + 1
                confusion.setdefault(truth["family"], {})
                confusion[truth["family"]][guess["family"]] = confusion[truth["family"]].get(guess["family"], 0) + 1
        n = sum(r["eligible"] for r in rows)
        correct = sum(r["family_correct"] for r in rows)
        answered = sum(r["eligible"] and r["guess_family"] != "unknown" for r in rows)
        report = {"truth_source": manifest["truth_source"], "reviewer": guesses["reviewer"], "method": guesses["method"],
                  "planned_requests": manifest["planned_requests"], "successful_requests": manifest["successful_requests"],
                  "eligible_endpoints": n, "total_endpoints": len(rows), "family_correct": correct,
                  "family_accuracy": correct/n if n else None, "guessed_endpoints": answered,
                  "overall_accuracy": correct/len(rows) if rows else None,
                  "guess_coverage": answered/n if n else None, "accuracy_when_guessed": correct/answered if answered else None,
                  "uniform_random_baseline": 1/len(public["candidate_families"]),
                  "majority_baseline": max(counts.values())/n if n else None,
                  "descriptive_wilson_95": wilson(correct, n),
                  "model_correct": sum(r["model_correct"] for r in rows), "confusion": confusion, "rows": rows,
                  "limitation": "小样本探索；Wilson 区间仅作描述，同家族/同题目可能相关。网关标签不等于真实上游身份。"}
        write_json(run / "report.json", report)
        manifest.update(state="revealed")
        write_json(run / "manifest.json", manifest)
        (run / "report.md").write_text(report_markdown(report), encoding="utf-8")
        return report


def report_markdown(r):
    acc = f"{r['family_accuracy']:.1%}" if r["family_accuracy"] is not None else "不可评分"
    lines = ["# 模型身份盲测", "", f"家族命中：{r['family_correct']}/{r['eligible_endpoints']}（{acc}）。",
             f"全部计划答卷命中率：{r['family_correct']}/{r['total_endpoints']}（{r.get('overall_accuracy', 0):.1%}），无效或弃答也计入分母。",
             f"有效请求：{r['successful_requests']}/{r['planned_requests']}；可评分端点：{r['eligible_endpoints']}/{r['total_endpoints']}。",
             f"均匀随机猜测基线：{r['uniform_random_baseline']:.1%}。", "", r["limitation"], "",
             "| 匿名编号 | 网关模型标签 | 猜测家族 | 命中 | 有效样本 |", "|---|---|---|---|---|"]
    for row in r["rows"]:
        verdict = ("是" if row["family_correct"] else "否") if row["eligible"] else "未评分"
        lines.append(f"| {row['alias']} | {row['model']} | {row['guess_family']} | {verdict} | {row['valid_samples']}/{row['total_samples']} |")
    lines += ["", f"评审者：{r['reviewer']}", "", f"判断方式：{r['method']}", "",
              "本结果验证对配置标签的识别能力，不构成模型来源证明。unknown 按未命中计入总体准确率。"]
    return "\n".join(lines) + "\n"
