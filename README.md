# Sillage

English | [简体中文](./README.zh-CN.md)

**Sillage** is a zero-dependency Python CLI for text-only model fingerprinting and blind model-family identification across unified LLM gateways. It reuses existing `pi` agent providers (`openai-completions` API) without adding third-party Python dependencies.

- **Text-only fingerprinting**: Classifies anonymous response packets purely from final output text using per-prompt character 2–4-gram TF-IDF cosine similarity weighted by leave-one-out discriminative power—never reading gateway metadata or forcing 1-to-1 candidate pairing.
- **Structured blind protocol**: Generates random anonymous IDs, shuffles sampling order, writes a salted SHA-256 commitment hash before sampling, strips model IDs/brand mentions/latency/usage from `blind.json`, and verifies digest integrity on `reveal`.
- **Honest scope boundary**: Labels correspond to the route IDs configured in `pi`. Even high identification accuracy measures repeatability under the same gateway, candidate set, and prompt parameters—it does not cryptographically prove which upstream model weights an intermediary ran.

## Quick Start

Requires Python 3.11+ and an existing `~/.pi/agent/models.json` (`hub` provider by default).

### Automated Fingerprinting (v0.2)

Collect labeled reference responses, train a fingerprint artifact, collect independent blind trials, and run automated classification:

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

`train` prints the fingerprint SHA-256 hash. For formal verification, record that hash before sampling and pass `--expect-hash <SHA256>` to `predict`. Fingerprint files cannot be overwritten in place; improved iterations must be evaluated on freshly sampled test sets.

### Manual Blind Evaluation

```bash
python3 -m sillage models
python3 -m sillage collect runs/demo --per-family --limit-prompts 4 --dry-run
python3 -m sillage collect runs/demo --per-family --limit-prompts 4
python3 -m sillage view runs/demo
python3 -m sillage ballot runs/demo --output runs/demo/guesses.json
```

Fill in the reviewer, method, and family guesses in `guesses.json` (use `unknown` when unsure; `confidence` may remain `null`; optional `model` must match the guessed family), then lock and reveal:

```bash
python3 -m sillage submit runs/demo --file runs/demo/guesses.json
python3 -m sillage reveal runs/demo
```

`submit` freezes the ballot; `reveal` outputs `report.json` and `report.md`.

## CLI Options

| Flag | Description |
| --- | --- |
| `--per-family` | Pick the first configured model per family (not necessarily the strongest or cheapest). |
| `--models ID ...` | Explicitly choose candidate model IDs. Omitting both flags samples all configured models. |
| `--prompts FILE` | Custom prompt JSON file (schema matches [`sillage/prompts.json`](./sillage/prompts.json)). Avoid self-identification prompts or private data. |
| `--repeats N` | Repetitions per prompt; each request is stateless with no conversation history. |
| `--trials N` | Independent anonymous response packets per model (e.g., 8 models × 3 trials = 24 scoring units). |
| `--reference` | Mark a run as labeled reference data for `train`; excluded from blind scoring. |
| `--temperature FLOAT` | Omitted by default to preserve each endpoint's default sampling behavior. |
| `--max-tokens N` | Default `2048` (`4096` recommended when models count reasoning tokens in the budget). Truncated or reasoning-only replies count as invalid samples. |
| `--timeout SECONDS` | Per-request timeout (default `90`). No silent retries or parameter mutation. |
| `--workers N` | Concurrent worker count (`1–16`, default `1`). |
| `--min-interval SECONDS` | Minimum interval between request starts to pace unstable upstreams. |
| `--fail-fast N` | Stop dispatching new requests after `N` consecutive failures (default `0`, disabled). Unexecuted samples remain in the planned denominator. |
| `--config FILE --provider NAME` | Global options placed before the subcommand to switch `pi` config or provider. |

## Blinding, Integrity & Metrics

- **Blind workflow**: Random anonymous IDs and shuffled order are committed with a salted SHA-256 hash prior to sampling. `blind.json` exposes only candidates, anonymous IDs, prompts, and final text. `private/data.json` stores the mapping and raw replies for post-reveal inspection only—never reasoning traces or credentials.
- **Credential handling**: `runs/` is git-ignored. Credentials are resolved in memory using `pi`'s `!command`, `$VAR`, `${VAR}`, and literal conventions without modifying `pi` configs or printing gateway error bodies.
- **Scoring**:
  - `overall_accuracy`: Correct guesses divided by all planned trial packets (including completely failed or abstained packets).
  - Family accuracy, exact-model hit rate, guess coverage, request success rate, confusion matrix, uniform/majority baselines, and descriptive Wilson intervals are reported in `report.md` / `report.json`.
  - Automated `train` requires at least 2 valid reference replies per model per retained prompt; `predict` abstains unless at least `--min-samples` (default `6`) distinct valid prompts are present.
  - Interrupted runs stay in `collecting` state and cannot be scored; start a fresh output directory to avoid mixing revealed data.

## Architecture

```text
sillage/
├── pyproject.toml                   # Python package metadata (Python 3.11+, stdlib only)
├── blindtest.py                     # Direct script entry point
├── sillage/
│   ├── __init__.py                  # Package exports
│   ├── __main__.py                  # `python3 -m sillage` entry point
│   ├── cli.py                       # Subcommands: models, collect, view, ballot, train, predict, submit, reveal
│   ├── core.py                      # Provider loading, paced sampling, blinding, commitments & scoring
│   ├── fingerprint.py               # Character 2-4-gram TF-IDF trainer, LOO weighting & predictor
│   ├── prompts.json                 # Default 8-prompt manual blind-test set
│   └── fingerprint_prompts.json     # Fixed probe set for automated text fingerprinting
└── tests/
    ├── test_fingerprint.py          # Fingerprinting isolation, digest checks & abstention tests
    ├── test_pacing.py               # Concurrency, min-interval pacing & fail-fast tests
    └── test_workflow.py             # End-to-end blinding, submission, tamper detection & scoring tests
```

## Verification

```bash
python3 -m unittest discover -s tests -v
```

Tests cover anonymous output stripping, brand-mention redaction, paced/concurrent sampling, `fail-fast`, `unknown` scoring, frozen digest verification, tamper detection, and reference/blind isolation.
