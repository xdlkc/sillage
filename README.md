# llm-blindtest

隐藏模型名称，从回答判断模型家族，再揭晓并统计命中率。使用 pi agent 的现有 provider，无第三方 Python 运行依赖。

这里的“答案”是 pi 配置的路由标签。即使命中率很高，也不能证明中转上游实际运行了该模型。

## 开始

需要 Python 3.11+。默认读取 `~/.pi/agent/models.json` 的 `hub` provider。

```bash
cd ~/Code/llm-blindtest
python3 blindtest.py models
python3 blindtest.py collect runs/demo --per-family --limit-prompts 4 --dry-run
python3 blindtest.py collect runs/demo --per-family --limit-prompts 4
python3 blindtest.py view runs/demo
python3 blindtest.py ballot runs/demo --output runs/demo/guesses.json
```

编辑 `guesses.json`，填写评审者、判断方法和每个匿名模型的家族猜测。不确定就填 `unknown`。`confidence` 可留 `null`；手填置信度不是经过校准的概率。`model` 可选，填具体候选 ID 时必须与家族一致。

```bash
python3 blindtest.py submit runs/demo --file runs/demo/guesses.json
python3 blindtest.py reveal runs/demo
```

`submit` 锁定猜测，之后不能修改。`reveal` 同时生成 `report.json` 和 `report.md`。

## 参数

- `--per-family`：每个家族取配置中的第一个模型。它不代表该家族的最强或最便宜版本。
- `--models ID ID ...`：指定候选。省略两者时采集全部配置模型。
- `--prompts FILE`：自定义题库，格式见 `llm_blindtest/prompts.json`。不应含身份自报、品牌知识问答或私有数据。
- `--repeats N`：每道题重复次数。每次是全新请求，没有会话历史。
- `--temperature FLOAT`：默认不发送，使用各服务端默认值。跨模型默认值可能不同。需要控制变量时显式传入各端都支持的值。
- `--max-tokens N`：默认 2048。部分模型把推理也计入该预算；截断或只有推理、没有最终回答时样本无效。
- `--timeout SECONDS`：单请求超时，默认 90。串行请求；不静默重试，不自动改变参数。
- `--config FILE --provider NAME`：放在子命令前，可切换 pi 配置。

默认题库 8 题，覆盖歧义、多语言、逻辑、格式、代码、未知知识、改写和反例。首次可用 4 题检查工具。要评估泛化，应另建题库和独立场次，不应把首次揭晓结果再当盲测。

## 盲法与记录

1. 生成随机匿名编号，随机分配模型并打乱采样顺序。
2. 采样前保存带随机盐的映射 SHA-256 承诺。
3. `blind.json` 只含候选集合、匿名编号、题目和最终回答。隐藏配置 ID、常见品牌自称、时延、usage、响应模型字段和失败细节。
4. `private/data.json` 保存映射和原始最终回答，仅供揭晓后核查。不保存模型推理内容或认证信息。
5. 猜测必须覆盖所有匿名编号。可以重复猜同一个家族，不能假定一一配对。
6. 锁定后验证匿名答卷、猜测和映射的摘要，再评分。

这是程序约束下的盲测，不是对文件所有者的加密隔离。评审者提前查看 `private/`、网关日志或任何能还原请求身份的信息，会破坏盲法。摘要能发现意外改写，不能防御同时重写所有文件的所有者。文本可能仍含遗漏的身份词，揭晓后应检查泄漏。

`runs/` 被 Git 忽略。凭据只在内存解析；支持 pi 的 `!command`、`$VAR`、`${VAR}` 和字面值约定。命令来自用户已有的本地 pi 配置。工具不改 pi 配置，不复制密钥，不输出网关错误正文。只支持 `openai-completions` API。

## 指标解释

- 家族准确率：正确猜测 / 至少有一个有效回答的端点。`unknown` 算未命中。
- 猜测覆盖率：作出明确猜测的有效端点比例。
- 具体模型命中单独统计；只猜家族时不算具体模型命中。
- 请求成功率与端点覆盖率独立展示。全失败端点不计入准确率，部分失败会降低证据量。
- 报告包含混淆矩阵、均匀随机和多数类基线。Wilson 区间只作描述；共享题目、同家族模型和评审策略可能造成相关性。
- 不做显著性或真实性结论。只有一轮、几个模型的结果不能代表长期识别能力。

采样被中断时已完成请求保存在 `private/`，场次保持 collecting，不允许评分。首版不恢复中断场次；新建目录重跑，避免混入已揭晓数据。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖匿名输出、身份词处理、接口参数、错误与截断、unknown 计分、失败覆盖率、先提交后揭晓、重复提交和记录篡改检测。
