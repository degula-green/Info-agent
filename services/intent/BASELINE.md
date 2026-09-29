# Stock Multilingual Baseline

Measured on 2026-09-29 against
`convaiinnovations/laya-multilingual@e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`.

## Corpus

- 63 labelled cases from `services/agent/tests/fixtures/understanding_corpus.json`
- 40 cases assert a concrete intent
- 62 cases assert `is_task`

## Result

After switching the model-facing option descriptions to concise English text:

| Metric | Result |
|---|---:|
| Intent top-1 | 24 / 40 |
| `is_task` | 42 / 62 |
| Mean `answer_confidence` | 0.578 |
| Direct-path coverage at `0.80` | 19% |
| Direct-path precision at `0.80` | 91.7% |

The original Chinese criteria produced only 1 / 40 intent top-1. English
criteria are required for the stock multilingual checkpoint, not merely an
optimisation.

An alternative one-request `noul` design, with one yes/no question per intent,
scored only 5 / 12 on a representative subset and produced very low
probabilities. It is not used.

## Decision

The stock checkpoint is not accurate or confident enough to be the main
classifier for this product taxonomy. It can serve as a conservative fast path
behind shadow mode, with the LLM handling almost everything else. A
domain-specific checkpoint must be trained before the Laya path becomes the
main classifier.

## Head-only Fine-Tuning Attempts

Two local head-only runs used 274 synthetic rows plus the real corpus as
validation:

| Run | Learning rate | Best validation accuracy |
|---|---:|---:|
| `laya-intent-v1` | `8e-4` | 0.500 |
| `laya-intent-v2` | `1e-4` | 0.468 |

Neither checkpoint beat the stock model's 0.600 intent top-1 on the same
corpus, so neither is enabled. The next viable step is a larger, more diverse
real-labelled dataset and encoder-aware fine-tuning, not another threshold
change.

## Active Domain Checkpoint

The active local checkpoint is `models/laya-intent-v5`. It was trained from the
stock multilingual checkpoint with the encoder unfrozen for three epochs on:

- 274 synthetic rows
- 90 rows from the first independent evaluation set
- 62 real corpus rows

The combined data was split into train and 20% stratified holdout. The internal
holdout reached 0.929 accuracy.

An additional 90-row evaluation set was generated after v5 training and was
never used for training:

| Metric | Result |
|---|---:|
| Intent top-1 | 84 / 90 |
| Top-1 accuracy | 93.3% |
| Direct-path coverage at `0.90` | 96.7% |
| Direct-path precision at `0.90` | 95.4% |

The Agent configuration now uses `AGENT_LAYAYA_MIN_CONFIDENCE=0.90` and the
sidecar uses `LAYA_MODEL_PATH` pointing at `models/laya-intent-v5`.

`models/` is ignored because the checkpoint is large. Publish
`models/laya-intent-v5` to Hugging Face or shared artifact storage before
deploying the sidecar on another machine.

## Reproduction

```powershell
uv sync --project services/intent

services/agent/.venv/Scripts/python.exe services/agent/scripts/build_laya_intent_dataset.py `
  --per-class 30

services/agent/.venv/Scripts/python.exe services/agent/scripts/build_laya_intent_dataset.py `
  --per-class 10 --no-corpus `
  --output services/agent/tests/fixtures/laya_intent_eval.jsonl

services/intent/.venv/Scripts/python.exe services/intent/train_intent_head.py `
  --dataset services/agent/tests/fixtures/laya_intent_dataset.jsonl `
  --dataset services/agent/tests/fixtures/laya_intent_eval.jsonl `
  --output-dir services/intent/models/laya-intent-v5 `
  --epochs 3 --batch-size 2 --grad-accum 4 `
  --lr-encoder 0.00002 --lr-head 0.0001 `
  --max-len 384 --head-max-len 256 `
  --holdout-ratio 0.2 --train-encoder --device cuda
```

Generate a fresh `laya_intent_eval2.jsonl` after training when re-running the
independent evaluation.
