"""Fine-tune `answerdotai/ModernBERT-base` as an actual sequence classifier
for correction detection -- the real "zero marginal cost" ModernBERT
approach, as opposed to the frozen-embedding + centroid/kNN approach tried
in modernbert_variants.py.

## Why this is a fundamentally different (and more promising) attempt

`nomic-ai/modernbert-embed-base` (used by Variants D/E/F) is ModernBERT-base
further trained for GENERAL-PURPOSE retrieval/embedding similarity -- its
vector space is optimized so semantically-similar text lands close together
for search, not so that "is this a correction" specifically falls out as a
separable direction. Centroid/kNN classification on top of it is a frozen,
zero-shot hack: it never taught the model anything about THIS task.
Bootstrapping 10x more training data into that centroid computation
(modernbert_bootstrap.py) barely moved the needle (F1 0.19->0.23 on
held-out fresh data) precisely because more data can't fix an embedding
space that was never shaped for this decision boundary in the first place.

Fine-tuning `answerdotai/ModernBERT-base` (the base LM checkpoint, NOT the
embed variant) with `AutoModelForSequenceClassification` actually adjusts
the model's weights via backprop on OUR labeled examples for OUR binary
task -- this is the standard, intended way to use ModernBERT for
classification (see the model's HF card and the AnswerDotAI GLUE
fine-tuning example). It should be strictly more capable of learning the
task-specific boundary than a frozen general-purpose embedding ever could.
Inference after training is still zero marginal LLM cost -- forward pass
through a local 149M-param model.

## Data split discipline (train / val / test, no leakage)

  - TRAIN: ground_truth.seed_examples() (33, hand-labeled) + the fresh
    bootstrap TRAIN split (225, Haiku-labeled) = 258 examples. Backprop
    happens only on this set.
  - VAL (checkpoint/epoch selection only, never reported as the final
    number): the fresh HOLDOUT split (75, Haiku-labeled) from
    modernbert_bootstrap.py. Used for `load_best_model_at_end` so we don't
    just pick whatever epoch happens to overfit train hardest.
  - TEST (untouched until the very end, real ground truth, not a Haiku
    proxy): ground_truth.eval_examples() (19, hand-labeled). This is the
    number that actually matters -- it's independent of Haiku's own error
    rate, unlike every other check in this spike so far.

Run with the hindsight venv (needs torch + transformers, both already
present for the embedding variants):
    ~/.hindsight/venv/bin/python3 spike/modernbert_finetune.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

sys.path.insert(0, os.path.dirname(__file__))
from ground_truth import eval_examples, seed_examples  # noqa: E402
from modernbert_bootstrap import get_or_create_split  # noqa: E402

BASE_MODEL = os.environ.get("MODERNBERT_FINETUNE_BASE", "answerdotai/ModernBERT-base")
MODEL_DIR = os.path.expanduser(
    "~/.hindsight/modernbert-spike-cache/finetuned-classifier"
    + ("-large" if "large" in BASE_MODEL.lower() else "")
)


class TextClsDataset(Dataset):
    def __init__(self, texts: list[str], labels: list[bool], tokenizer, max_length: int = 256):
        self.encodings = tokenizer(texts, truncation=True, padding=True, max_length=max_length)
        self.labels = [int(x) for x in labels]

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx):
        item = {k: torch.tensor(v[idx]) for k, v in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[idx])
        return item


class WeightedTrainer(Trainer):
    """Applies class weights (inverse frequency) to the loss so the rare
    positive ("correction") class doesn't get drowned out by the majority
    negative class -- real correction traffic is ~8% positive."""

    def __init__(self, *args, class_weights: torch.Tensor, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        loss_fct = torch.nn.CrossEntropyLoss(weight=self.class_weights.to(logits.device))
        loss = loss_fct(logits.view(-1, 2), labels.view(-1))
        return (loss, outputs) if return_outputs else loss


def _metrics(eval_pred) -> dict:
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=1)
    tp = int(((preds == 1) & (labels == 1)).sum())
    fp = int(((preds == 1) & (labels == 0)).sum())
    fn = int(((preds == 0) & (labels == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def build_train_val() -> tuple[list[str], list[bool], list[str], list[bool]]:
    seed = seed_examples()
    split = get_or_create_split()
    train_texts = [e.text for e in seed] + [t for t, _ in split["train"]]
    train_labels = [e.is_correction for e in seed] + [c for _, c in split["train"]]
    val_texts = [t for t, _ in split["holdout"]]
    val_labels = [c for _, c in split["holdout"]]
    return train_texts, train_labels, val_texts, val_labels


def train(epochs: int = 8, lr: float = 2e-5, batch_size: int = 8) -> None:
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Device: {device}")

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=2)

    train_texts, train_labels, val_texts, val_labels = build_train_val()
    n_pos = sum(train_labels)
    n_neg = len(train_labels) - n_pos
    print(f"Train: {len(train_labels)} ({n_pos} pos / {n_neg} neg)  Val (holdout): {len(val_labels)}")

    class_weights = torch.tensor([1.0, n_neg / n_pos], dtype=torch.float32)
    print(f"Class weights (neg, pos): {class_weights.tolist()}")

    train_ds = TextClsDataset(train_texts, train_labels, tokenizer)
    val_ds = TextClsDataset(val_texts, val_labels, tokenizer)

    warmup_steps = max(1, round(0.1 * (len(train_texts) / batch_size) * epochs))
    args = TrainingArguments(
        output_dir="/tmp/modernbert_finetune_run",
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=16,
        learning_rate=lr,
        weight_decay=0.01,
        warmup_steps=warmup_steps,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        logging_strategy="epoch",
        report_to=[],
    )

    trainer = WeightedTrainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=_metrics,
        class_weights=class_weights,
    )
    trainer.train()

    print("\nFinal validation (fresh holdout, Haiku-as-reference) metrics:")
    print(trainer.evaluate())

    os.makedirs(MODEL_DIR, exist_ok=True)
    trainer.save_model(MODEL_DIR)
    tokenizer.save_pretrained(MODEL_DIR)
    print(f"\nSaved fine-tuned classifier to {MODEL_DIR}")


def load_classifier():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    model.eval()
    return tokenizer, model


# Chosen 2026-08-25 by sweeping thresholds against the fresh HOLDOUT split
# only (Haiku-as-reference, n=168) -- best holdout F1=0.50 at 0.15 (default
# 0.5 gave F1=0.40). Applying this holdout-selected threshold to the
# untouched hand-labeled TEST set (never used for threshold selection)
# improved it from precision=1.00/recall=0.73/f1=0.85 (at 0.5) to
# precision=1.00/recall=0.80/f1=0.89 (at 0.15) -- still zero false
# positives. Do not re-tune this by peeking at the hand-labeled eval set
# directly; re-derive it from a holdout split if the training data changes.
DEFAULT_THRESHOLD = 0.15


def classify(text: str, tokenizer, model, threshold: float = DEFAULT_THRESHOLD) -> tuple[bool, float]:
    """Returns (is_correction, confidence). `confidence` is always the raw
    P(correction) softmax score, regardless of the predicted label, so
    callers can see how close a prediction was to the threshold."""
    inputs = tokenizer(text, truncation=True, max_length=256, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inputs).logits
    probs = torch.softmax(logits, dim=1)[0]
    confidence = float(probs[1])
    is_correction = confidence >= threshold
    return is_correction, confidence


if __name__ == "__main__":
    train()
