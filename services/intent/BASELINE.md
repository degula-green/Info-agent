# Intent Baselines

## Current: `laya-intent-v6` (intent-v6, 5 + 2 labels)

### Contract

The taxonomy lives in one place:

```
services/agent/app/understanding/intent_contract.json
```

```text
todo.create
knowledge.answer
web.research
compliance.assess
form.complete
non_task
other_task
```

The Agent schema, the Laya provider, `services/intent/train_intent_head.py` and
`services/agent/scripts/build_laya_intent_dataset.py` all read that file.
`services/agent/tests/test_intent_contract.py` proves they agree and that the
retired labels (`document.compare`, `form.prepare`, `form.submit`) are rejected
everywhere. A checkpoint whose `intent_schema_version` / `option_order` does not
match is refused at start-up (`verify_model_contract`, `LAYA_REQUIRE_INTENT_CONTRACT`).

`form.complete` is a single intent: "write the fields of a form and prepare a
preview". Opening the form, reading fields, filling, previewing, waiting for
confirmation and writing the confirmed values are execution steps planned by the
model, not labels. Submitting or filing a filled form to an external platform is
`other_task`.

### Data

| File | Rows | Per class | Notes |
|---|---:|---:|---|
| `services/agent/tests/fixtures/laya_intent_dataset.jsonl` | 3810 | 472–585 | verified synthetic + migrated corpus + human-written |
| `services/agent/tests/fixtures/laya_intent_train_human.jsonl` | 140 | 20 | human-written training rows, topics disjoint from the benchmark |
| `services/agent/tests/fixtures/laya_intent_eval_v6.jsonl` | 210 | 30 | frozen benchmark, 100% hand-written |
| `services/agent/tests/fixtures/understanding_corpus.json` | 70 cases | — | migrated to intent-v6 |

Training rows are generated from the contract prompts, then re-labelled by an
independent verifier pass and dropped when it disagrees, because the generator
and the labeller were the same model. Text is normalized and de-duplicated, and
the frozen benchmark is excluded from the training file (`--exclude-rows`); the
trainer aborts on any leftover overlap, legacy label or unknown label.

Generated text alone was not enough. Seven data iterations that only added
prompt-shaped rows moved the benchmark between 0.895 and 0.929: the model fit
generated phrasing almost perfectly and still missed human phrasing. Adding 140
human-written training rows took it to 0.967. The benchmark and that training
set share an author, so 0.967 is an upper bound on what to expect from real
traffic — the phrasing overlaps in style even though no sentence does. Treat the
next real labelled corpus as the decisive measurement.

An earlier revision of the benchmark contained 105 generated rows. A
verification pass showed that this slice was unreliable (56% agreement with the
model), so the benchmark is now human-written only. The first two checkpoints
trained on unverified synthetic data scored 0.837 and 0.739 top-1 on it.

Hashes (sha256, first 16 hex chars):

```text
laya_intent_dataset.jsonl            c72b1f115ceaea62
laya_intent_train_human.jsonl        c582ae9b88bc5c4b
laya_intent_eval_v6.jsonl            ab7a93c942a9acf2
laya_intent_eval_v6_handwritten.jsonl 39968f1a5a7f0c13
understanding_corpus.json            a0ff2bed81c89b1a
```

### Training

Warm start from the `laya-intent-v5` checkpoint (encoder, head and scorer are
reusable because Laya scores options dynamically). One epoch with the encoder
frozen, then the encoder unfrozen:

```powershell
services/intent/.venv/Scripts/python.exe services/intent/train_intent_head.py `
  --dataset services/agent/tests/fixtures/laya_intent_dataset.jsonl `
  --test-dataset services/agent/tests/fixtures/laya_intent_eval_v6.jsonl `
  --model-dir services/intent/models/laya-intent-v5 `
  --output-dir services/intent/models/laya-intent-v6 `
  --epochs 8 --freeze-encoder-epochs 1 --batch-size 2 --grad-accum 4 `
  --lr-encoder 1e-5 --lr-head 5e-5 --max-len 384 --head-max-len 256 `
  --holdout-ratio 0.2 --train-encoder --device cuda `
  --seed 20260930 --early-stop-patience 3
```

Best epoch: 6 of 8 (0.38 h on an RTX 5060 Laptop, 8 GB). The checkpoint is
written on every improvement, so an interrupted run still leaves a usable model.
`models/laya-intent-v6/rl_agent_config.json` records the schema version, the
option order and the metrics below.

Training is not bit-reproducible: repeated runs with this seed and dataset moved
the internal holdout by roughly a point and the frozen benchmark by more. Pick a
release checkpoint by the internal validation macro-F1, never by the frozen
benchmark.

### Results

Internal stratified holdout (20% of the training pool the trainer held out):

| Metric | Result |
|---|---:|
| top-1 | 0.9790 |
| macro-F1 | 0.9784 |

Frozen benchmark, never used for training (`tests/fixtures/laya_intent_eval_v6_report.json`):

| Intent | Recall | Precision |
|---|---:|---:|
| `todo.create` | 0.933 | 1.000 |
| `knowledge.answer` | 0.967 | 1.000 |
| `web.research` | 0.967 | 0.967 |
| `compliance.assess` | 1.000 | 1.000 |
| `form.complete` | 1.000 | 0.909 |
| `non_task` | 1.000 | 1.000 |
| `other_task` | 0.900 | 0.900 |

| Metric | Result | Gate | Status |
|---|---:|---:|---|
| overall top-1 | 0.9667 | ≥ 0.95 | met |
| macro-F1 | 0.9668 | ≥ 0.95 | met |
| per-business-intent recall | 0.933–1.000 | ≥ 0.90 | met |
| `compliance.assess` recall | 1.000 | ≥ 0.95 | met |
| `non_task` / `other_task` recall | 1.000 / 0.900 | ≥ 0.90 | met |

Every published threshold is met, with 7 errors out of 210 rows. The remaining
misses are the fill-versus-submit edge (`other_task` recall 0.900, `form.complete`
precision 0.909): "帮我把这张已填好的表单递交到平台" still occasionally reads
as `form.complete`. `form.complete` recall is perfect, so the residual risk is a
misrouted submission rather than a missed form request.

The internal holdout is no longer a useful signal on its own — it is generated
by the same prompts — so the frozen human benchmark is the number that counts.

### Threshold calibration

Two sweeps exist, and they disagree, which is itself the finding.

Internal validation split (762 rows, the same split the trainer held out —
`calibrate_intent_thresholds.py`, the split the migration document asks for):

```text
min_confidence 0.50, min_margin 0.05  ->  coverage 1.000, precision 0.978
```

Frozen benchmark (210 hand-written rows, reported only as a sanity check):

```text
min_confidence 0.50, min_margin 0.05  ->  coverage 1.000, precision 0.967
```

Both splits now agree, so no gate is needed to reach the target precision: every
sweep point clears 0.95 precision and the widest one (0.50 / 0.05) is the
recommendation. The shipped configuration keeps `AGENT_LAYAYA_MIN_CONFIDENCE=0.90`
/ `AGENT_LAYAYA_MIN_MARGIN=0.15` as a floor — it only removes very low-confidence
answers — and the LLM fallback stays in place for those.

### Evaluation

```powershell
./scripts/start-intent.ps1 -ModelPath services/intent/models/laya-intent-v6 -RequireIntentContract
services/agent/.venv/Scripts/python.exe services/agent/scripts/eval_laya_intents.py `
  --json services/agent/tests/fixtures/laya_intent_eval_v6_report.json --gate
services/intent/.venv/Scripts/python.exe services/intent/calibrate_intent_thresholds.py `
  --model-dir services/intent/models/laya-intent-v6 `
  --dataset services/agent/tests/fixtures/laya_intent_dataset.jsonl `
  --json services/agent/tests/fixtures/laya_intent_v6_thresholds.json
```

`--gate` exits non-zero unless top-1, macro-F1, every business-intent recall and
`compliance.assess` recall clear the published thresholds.

### Release and rollback

`services/intent/.env` points `LAYA_MODEL_PATH` at `models/laya-intent-v6` and
sets `LAYA_REQUIRE_INTENT_CONTRACT=1`; `services/agent/.env` sets
`AGENT_LAYAYA_MODEL_PATH` so the Agent also verifies the checkpoint at start-up.
Release and rollback are atomic: the Agent contract, the prompt, the Laya
criteria and the checkpoint must move together, and rolling back means
restoring `laya-intent-v5` plus the v5 thresholds. Never serve v5 with the
intent-v6 criteria — the option list would no longer line up with its head.

`services/intent/models` is git-ignored. Publish `models/laya-intent-v6` to
Hugging Face or shared artifact storage before deploying the sidecar elsewhere.

### Open work

- The 0.967 benchmark score is optimistic: the benchmark and the human-written
  training rows share an author. Re-run the acceptance check on real labelled
  traffic before widening the Laya fast path, and add real traffic to the
  training pool; generated rows have largely stopped paying off.
- Tighten the fill-versus-submit edge (`other_task` recall 0.900,
  `form.complete` precision 0.909). A misrouted submission is the residual risk:
  "确认无误后提交申请表" and "帮我把这件事加到明天待办" still land on
  `form.complete`.
- Publish `models/laya-intent-v6` to shared artifact storage; `services/intent/models`
  is git-ignored and the checkpoint is local only.
- Keep the LLM fallback authoritative for low-confidence answers.

## History

### Stock `laya-multilingual` (2026-09-29)

Measured with English option descriptions against the 63-case corpus:
intent top-1 24/40, `is_task` 42/62, direct-path coverage at 0.80 was 19%.
The stock checkpoint is not accurate enough to be the main classifier.

### `laya-intent-v5` (nine labels)

Warm-started from the stock checkpoint with the encoder unfrozen for three
epochs on 274 synthetic rows, 90 rows from an evaluation set and 62 corpus rows.
Internal holdout accuracy 0.929, held-out synthetic set 84/90. It was the
served checkpoint until the intent-v6 migration and is kept unchanged for
rollback.
