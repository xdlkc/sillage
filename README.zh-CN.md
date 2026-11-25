# Sillage

[English](./README.md) | 简体中文

**Sillage** 是零第三方依赖的 Python CLI 工具，面向统一大模型网关提供纯文本输出指纹与模型家族盲测识别。它复用本机 `pi` Agent 的既有 provider 配置（支持 `openai-completions` API），无需额外安装 Python 第三方库。

- **纯文本指纹识别**：仅基于最终回复文本，按题目构建字符 2–4 元组 TF-IDF 向量，计算与参考回答的余弦相似度，并按参考集留一法区分度加权汇总——不读取网关元数据，也不强行要求候选一一配对。
- **结构化盲法协议**：采样前生成随机匿名编号、打乱顺序并写入加盐 SHA-256 承诺摘要；`blind.json` 隐藏模型 ID、常见品牌自称、时延、usage 与失败细节；`reveal` 阶段校验摘要完整性。
- **诚实的边界声明**：这里的“答案”对应 `pi` 中配置的路由标签。高命中率检验的是同一网关、候选集合、探针与采样参数下的可重复识别能力，不能作为中转上游实际运行某模型权重的密码学证明。

## 快速开始

需要 Python 3.11+，默认读取 `~/.pi/agent/models.json` 中的 `hub` provider。

### 自动识别（v0.2）

先采集有标签参考回答并训练指纹，再对独立调用生成的匿名答卷执行自动分类与揭晓：

```bash
python3 -m sillage collect runs/reference --reference --per-family \
  --prompts sillage/fingerprint_prompts.json --repeats 3 \
  --workers 4 --min-interval 2 --fail-fast 5 --max-tokens 4096 --timeout 90
python3 -m sillage train runs/reference --output runs/fingerprint.json
python3 -m sillage collect runs/test --per-family \
  --prompts sillage/fingerprint_prompts.json --trials 3 \
  --workers 4 --min-interval 2 --fail-fast 5 --max-tokens 4096 --timeout 90
python3 -m sillage predict runs/test --fingerprint runs/fingerprint.json \
  --min-samples 6 --output runs/test/guesses.json
python3 -m sillage submit runs/test --file runs/test/guesses.json
python3 -m sillage reveal runs/test
```

`train` 会输出指纹文件的 SHA-256 摘要。正式验收时应在采样前记录该值，并在 `predict` 中传入 `--expect-hash <SHA256>`。指纹文件不可原地覆盖；迭代改进后必须在全新采样的验收集上重测。

### 人工盲测

```bash
python3 -m sillage models
python3 -m sillage collect runs/demo --per-family --limit-prompts 4 --dry-run
python3 -m sillage collect runs/demo --per-family --limit-prompts 4
python3 -m sillage view runs/demo
python3 -m sillage ballot runs/demo --output runs/demo/guesses.json
```

编辑 `guesses.json`，填写评审者、判断方法与每个匿名模型的家族猜测（不确定填 `unknown`；`confidence` 可留 `null`；可选的 `model` 字段须与家族一致），随后锁定并揭晓：

```bash
python3 -m sillage submit runs/demo --file runs/demo/guesses.json
python3 -m sillage reveal runs/demo
```

`submit` 锁定猜测且不可修改；`reveal` 同时生成 `report.json` 与 `report.md`。

## 参数说明

| 参数 | 说明 |
| --- | --- |
| `--per-family` | 每个家族取配置中的第一个模型（不代表该家族最强或最便宜版本）。 |
| `--models ID ...` | 显式指定候选模型 ID；省略两者时采集全部配置模型。 |
| `--prompts FILE` | 自定义题库，格式见 [`sillage/prompts.json`](./sillage/prompts.json)，不应含身份自报或私有数据。 |
| `--repeats N` | 每道题重复次数；每次为全新无状态请求，不含会话历史。 |
| `--trials N` | 每个模型生成 `N` 组独立匿名答卷（例如 8 个模型 × 3 组 = 24 个计分单位）。 |
| `--reference` | 明确标记为有标签参考样本供 `train` 使用，不计入盲测成绩。 |
| `--temperature FLOAT` | 默认不发送，保留各服务端默认采样行为。 |
| `--max-tokens N` | 默认 `2048`（若模型将推理计入预算建议 `4096`）；截断或仅有推理无正文时样本无效。 |
| `--timeout SECONDS` | 单请求超时秒数（默认 `90`），不静默重试，不自动修改参数。 |
| `--workers N` | 并发请求数（默认 `1`，范围 `1–16`）。 |
| `--min-interval SECONDS` | 控制所有请求启动的最小间隔，适合上游波动时节流。 |
| `--fail-fast N` | 连续失败 `N` 次后停止派发新请求（默认 `0` 为关闭），未执行样本仍保留在计划分母中。 |
| `--config FILE --provider NAME` | 放在子命令前，用于切换 `pi` 配置文件或 provider 名称。 |

## 盲法、完整性与指标

- **盲法与记录**：采样前生成随机匿名编号、打乱采样顺序并保存加盐 SHA-256 承诺。`blind.json` 仅含候选集合、匿名编号、题目和最终回答；`private/data.json` 保存映射和原始回答仅供揭晓后核查，不保存推理过程或凭据。
- **凭据安全**：`runs/` 已被 Git 忽略。凭据仅在内存中按 `pi` 的 `!command`、`$VAR`、`${VAR}` 与字面值约定解析，不修改 `pi` 配置，不复制密钥，不输出网关错误正文。
- **评分指标**：
  - `overall_accuracy`：正确猜测数 / 全部计划答卷数（全失败或弃答的答卷均保留在分母中）。
  - 报告同时展示家族准确率、具体模型命中率、猜测覆盖率、请求成功率、混淆矩阵、均匀随机 / 多数类基线及描述性 Wilson 区间。
  - `train` 要求每个模型每道保留探针至少 2 份有效参考回答；`predict` 默认至少需要 `--min-samples`（默认 `6`）道不同有效探针，否则弃答。
  - 采样中断后场次保持 `collecting` 状态且不可评分；新建目录重跑以避免混入已揭晓数据。

## 目录结构

```text
sillage/
├── pyproject.toml                   # Python 包配置（Python 3.11+，纯标准库）
├── blindtest.py                     # 脚本直跑入口
├── sillage/
│   ├── __init__.py                  # 包导出
│   ├── __main__.py                  # `python3 -m sillage` 入口
│   ├── cli.py                       # 子命令：models、collect、view、ballot、train、predict、submit、reveal
│   ├── core.py                      # Provider 加载、节流采样、盲法承诺与评分报告
│   ├── fingerprint.py               # 字符 2-4 元组 TF-IDF 训练、留一法加权与预测
│   ├── prompts.json                 # 默认 8 题人工盲测题库
│   └── fingerprint_prompts.json     # 自动文本指纹固定探针集
└── tests/
    ├── test_fingerprint.py          # 指纹隔离、摘要校验与弃答测试
    ├── test_pacing.py               # 并发、最小间隔节流与 fail-fast 测试
    └── test_workflow.py             # 盲法、提交、篡改检测与评分端到端测试
```

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖匿名输出、身份词脱敏、节流并发、`fail-fast`、`unknown` 计分、冻结摘要校验、记录篡改检测以及参考集与盲测集隔离。
