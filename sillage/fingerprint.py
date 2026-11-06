"""Text-only, prompt-conditioned reference matching; no gateway metadata features."""
from collections import Counter, defaultdict
import math
from pathlib import Path
import re
import unicodedata

from .core import BlindtestError, digest, family, read_json, verify_public, write_json

ALGORITHM = "prompt-conditioned-char-tfidf-v1"


def features(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return Counter(text[i:i+n] for n in (2, 3, 4) for i in range(max(0, len(text)-n+1)))


def vector(text, idf):
    values = {k: (1 + math.log(v)) * idf[k] for k, v in features(text).items() if k in idf}
    norm = math.sqrt(sum(v*v for v in values.values()))
    return {k: v/norm for k, v in values.items()} if norm else {}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v*b.get(k, 0) for k, v in a.items())


def load_reference(run):
    run = Path(run)
    m = read_json(run / "manifest.json")
    if m.get("state") != "reference":
        raise BlindtestError("训练只接受显式 --reference 采集的场次，不能读取未揭晓的盲测映射")
    d = read_json(run / "reference.json")
    if digest(d) != m.get("reference_digest"):
        raise BlindtestError("参考样本摘要不一致")
    return d


def fit(reference):
    examples = reference["examples"]
    models = sorted(reference["candidate_models"])
    if len(models) < 2:
        raise BlindtestError("指纹训练至少需要两个候选模型")
    prompts = {}
    grouped = defaultdict(lambda: defaultdict(list))
    for e in examples:
        pid = e["prompt_id"]
        if pid in prompts and prompts[pid] != e["prompt"]:
            raise BlindtestError("同一 prompt_id 对应不同题目")
        prompts[pid] = e["prompt"]
        grouped[pid][e["model"]].append(e["answer"])
    # Keep only complete reference probes, independently of any validation outcomes.
    retained = {p: g for p, g in grouped.items() if all(len(g.get(m, [])) >= 2 for m in models)}
    if len(retained) < 3:
        raise BlindtestError("至少3道完整参考探针，每道每个模型至少2份有效回答；请增加参考采样")
    probes = {}
    for pid, by_model in sorted(retained.items()):
        documents = [text for rows in by_model.values() for text in rows]
        df = Counter(k for text in documents for k in features(text).keys())
        idf = {k: math.log((1+len(documents))/(1+count)) + 1 for k, count in df.items()}
        vectors = {m: [vector(t, idf) for t in by_model[m]] for m in models}
        margins, hits = [], []
        for model in models:
            for i, v in enumerate(vectors[model]):
                same = max(cosine(v, other) for j, other in enumerate(vectors[model]) if j != i)
                rival = max(cosine(v, other) for m in models if m != model for other in vectors[m])
                margins.append(same-rival)
                hits.append(same > rival)
        # Fixed formula. Weight uses only leave-one-out reference separation.
        weight = max(0.1, min(1.0, sum(margins)/len(margins) + 0.25))
        probes[pid] = {"prompt": prompts[pid], "answers": dict(by_model), "idf": idf,
                       "weight": weight, "reference_loo_accuracy": sum(hits)/len(hits)}
    return {"algorithm": ALGORITHM, "source_digest": digest(reference), "models": models,
            "settings": reference["settings"], "probes": probes,
            "dropped_prompt_ids": sorted(set(grouped)-set(retained)),
            "limitations": "固定候选与固定探针；只用最终文本。参考LOO是开发诊断，不是盲测成绩。"}


def classify(public, fingerprint, min_samples=6):
    if fingerprint["algorithm"] != ALGORITHM:
        raise BlindtestError("未知指纹算法版本")
    if min_samples < 1 or min_samples > len(fingerprint["probes"]):
        raise BlindtestError("min-samples 超出指纹探针数量")
    models = fingerprint["models"]
    if set(public["candidate_models"]) != set(models):
        raise BlindtestError("盲测候选集合与参考库不同；请重新训练参考库")
    vectors = {pid: {m: [vector(t, p["idf"]) for t in p["answers"][m]] for m in models}
               for pid, p in fingerprint["probes"].items()}
    guesses, diagnostics = [], {}
    for endpoint in public["endpoints"]:
        per_probe = defaultdict(list)
        for sample in endpoint["samples"]:
            pid = sample["prompt_id"]
            if pid not in fingerprint["probes"]:
                continue
            probe = fingerprint["probes"][pid]
            if sample["prompt"] != probe["prompt"]:
                raise BlindtestError("探针正文与参考库不同；禁止仅凭相同ID匹配")
            if sample["ok"]:
                v = vector(sample["answer"], probe["idf"])
                per_probe[pid].append({m: max(cosine(v, ref) for ref in vectors[pid][m]) for m in models})
        scores = {m: 0.0 for m in models}
        total_weight = sum(fingerprint["probes"][p]["weight"] for p in per_probe)
        for pid, repeats in per_probe.items():
            for m in models:
                scores[m] += fingerprint["probes"][pid]["weight"] * sum(r[m] for r in repeats)/len(repeats)
        scores = {m: v/total_weight if total_weight else 0 for m, v in scores.items()}
        ranking = sorted(scores, key=lambda m: (-scores[m], m))
        enough = len(per_probe) >= min_samples
        winner = ranking[0] if enough else None
        guesses.append({"alias": endpoint["alias"], "family": family(winner) if winner else "unknown",
                        "model": winner, "confidence": None,
                        "reason": f"匹配 {len(per_probe)} 道有效探针；文本相似度分数不等于正确概率。"})
        diagnostics[endpoint["alias"]] = {"valid_probes": len(per_probe), "scores": scores,
                                         "margin": scores[ranking[0]]-scores[ranking[1]]}
    return {"reviewer": ALGORITHM, "method": "按探针匹配字符2–4元组TF-IDF余弦相似度；参考集留一法加权；逐答卷独立分类，无一一配对约束。",
            "classifier_digest": digest(fingerprint), "min_samples": min_samples,
            "guesses": guesses, "diagnostics": diagnostics}


def train_command(reference_run, output):
    if Path(output).exists():
        raise BlindtestError("指纹文件已存在，拒绝覆盖；请显式使用新版本路径")
    model = fit(load_reference(reference_run))
    write_json(output, model)
    return {"digest": digest(model), "probes": len(model["probes"]),
            "dropped": model["dropped_prompt_ids"],
            "reference_loo": {k: p["reference_loo_accuracy"] for k, p in model["probes"].items()}}


def predict_command(run, fingerprint_path, output, min_samples=6, expected_hash=None):
    if Path(output).exists():
        raise BlindtestError("猜测文件已存在，拒绝覆盖")
    manifest, public = verify_public(run)
    if manifest["state"] != "collected":
        raise BlindtestError("只对未提交、未揭晓的完整场次预测")
    fingerprint = read_json(fingerprint_path)
    if expected_hash and digest(fingerprint) != expected_hash:
        raise BlindtestError("指纹与预先锁定的摘要不同")
    for key, value in fingerprint["settings"].items():
        if manifest.get(key) != value:
            raise BlindtestError(f"采样参数 {key} 与参考集不一致")
    result = classify(public, fingerprint, min_samples)
    write_json(output, result)
    return {"predictions": len(result["guesses"]), "classifier_digest": result["classifier_digest"]}
