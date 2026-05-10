"""Sprint 7.9 Day 5: LoRA fine-tune BAAI/bge-reranker-v2-m3 on FB labels.

Architecture choices (Sprint 7.9 spec):
  - Base: BAAI/bge-reranker-v2-m3 (~568M params, XLM-RoBERTa-large + classification head)
  - LoRA: rank=16, alpha=32, target=['query', 'value'] (attention only — conservative
    starting point; extends to FFN if rank=16 is undertrained)
  - Trainable params: ~1–2% of base = ~6–11M (fits 24GB M4 Pro in BF16 easily)
  - Loss: BCEWithLogitsLoss on relevance score (single scalar logit per query-doc pair)
  - Optimizer: AdamW, lr=2e-4 (standard LoRA range), weight_decay=0.01
  - Schedule: linear warmup (10% steps) + linear decay
  - Batch: 8 with grad-accum=2 → effective batch 16
  - Precision: BF16 mixed on MPS (Apple Silicon)
  - Early stopping: 2 epochs no improvement on val loss, max 5 epochs

Output:
  data/models/reranker_ft_v1/
    adapter_config.json           — peft config
    adapter_model.safetensors     — LoRA weights only (~30–50 MB)
    training_metadata.json        — epoch-by-epoch loss/AUC + final pick
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import torch
from torch.utils.data import DataLoader, Dataset

from peft import LoraConfig, get_peft_model
from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_MODEL = "BAAI/bge-reranker-v2-m3"
TRAIN_PATH = Path("data/training/reranker_ft_v1/train.jsonl")
VAL_PATH = Path("data/training/reranker_ft_v1/val.jsonl")
OUT_DIR = Path("data/models/reranker_ft_v1")

MAX_LENGTH = 512  # BGE-reranker default
DEFAULT_EPOCHS = 5
DEFAULT_BATCH_SIZE = 8
DEFAULT_GRAD_ACCUM = 2
DEFAULT_LR = 2e-4
DEFAULT_WEIGHT_DECAY = 0.01
DEFAULT_WARMUP_FRAC = 0.10
DEFAULT_LORA_R = 16
DEFAULT_LORA_ALPHA = 32
DEFAULT_LORA_DROPOUT = 0.1
DEFAULT_PATIENCE = 2  # early stopping


class _RerankerDataset(Dataset):
    """Maps (query, chunk, label) → tokenized cross-encoder input + scalar label."""

    def __init__(self, jsonl_path: Path, tokenizer):
        self.rows = [json.loads(l) for l in jsonl_path.open()]
        self.tok = tokenizer

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int) -> dict:
        r = self.rows[idx]
        enc = self.tok(
            r["query"],
            r["chunk"],
            max_length=MAX_LENGTH,
            truncation=True,
            padding=False,  # collator pads
            return_tensors=None,
        )
        return {
            "input_ids": enc["input_ids"],
            "attention_mask": enc["attention_mask"],
            "label": float(r["label"]),
            "fb_id": r["fb_id"],
        }


def _collate(batch: list[dict], tokenizer) -> dict:
    """Dynamic padding within batch."""
    input_ids = [b["input_ids"] for b in batch]
    attention_mask = [b["attention_mask"] for b in batch]
    pad_token_id = tokenizer.pad_token_id
    max_len = max(len(x) for x in input_ids)
    padded_ids = torch.full((len(batch), max_len), pad_token_id, dtype=torch.long)
    padded_mask = torch.zeros((len(batch), max_len), dtype=torch.long)
    for i, (ids, mask) in enumerate(zip(input_ids, attention_mask)):
        padded_ids[i, : len(ids)] = torch.tensor(ids, dtype=torch.long)
        padded_mask[i, : len(mask)] = torch.tensor(mask, dtype=torch.long)
    labels = torch.tensor([b["label"] for b in batch], dtype=torch.float32)
    return {"input_ids": padded_ids, "attention_mask": padded_mask, "labels": labels}


def _binary_metrics(logits: torch.Tensor, labels: torch.Tensor) -> dict:
    """Pointwise BCE-style metrics on a batch's logits + labels."""
    probs = torch.sigmoid(logits)
    preds = (probs >= 0.5).float()
    acc = (preds == labels).float().mean().item()
    return {"acc": acc, "mean_pos_prob": probs[labels == 1].mean().item() if (labels == 1).any() else float("nan"),
            "mean_neg_prob": probs[labels == 0].mean().item() if (labels == 0).any() else float("nan")}


def _eval(model, loader, device, dtype) -> dict:
    """Compute val loss + accuracy across the entire val set."""
    model.eval()
    total_loss = 0.0
    total_count = 0
    all_logits: list[torch.Tensor] = []
    all_labels: list[torch.Tensor] = []
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="sum")

    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["labels"].to(device)
            with torch.autocast(device_type=device.type, dtype=dtype):
                outputs = model(input_ids=input_ids, attention_mask=attention_mask)
                logits = outputs.logits.squeeze(-1)
                loss = loss_fn(logits, labels)
            total_loss += loss.item()
            total_count += labels.size(0)
            all_logits.append(logits.detach().float().cpu())
            all_labels.append(labels.detach().float().cpu())

    logits_cat = torch.cat(all_logits)
    labels_cat = torch.cat(all_labels)
    metrics = _binary_metrics(logits_cat, labels_cat)
    metrics["val_loss"] = total_loss / max(total_count, 1)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint 7.9 Day 5 — reranker LoRA fine-tune")
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--grad-accum", type=int, default=DEFAULT_GRAD_ACCUM)
    parser.add_argument("--lr", type=float, default=DEFAULT_LR)
    parser.add_argument("--lora-r", type=int, default=DEFAULT_LORA_R)
    parser.add_argument("--lora-alpha", type=int, default=DEFAULT_LORA_ALPHA)
    parser.add_argument("--lora-dropout", type=float, default=DEFAULT_LORA_DROPOUT)
    parser.add_argument("--patience", type=int, default=DEFAULT_PATIENCE)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["mps", "cpu", "cuda"], default="mps")
    args = parser.parse_args()

    print("=" * 90)
    print("Sprint 7.9 Day 5 — BGE reranker LoRA fine-tune")
    print("=" * 90)
    print(f"  base:           {BASE_MODEL}")
    print(f"  train:          {TRAIN_PATH}")
    print(f"  val:            {VAL_PATH}")
    print(f"  output:         {OUT_DIR}")
    print(f"  epochs:         {args.epochs}  (early-stop patience: {args.patience})")
    print(f"  batch_size:     {args.batch_size}  (grad_accum: {args.grad_accum} → effective {args.batch_size * args.grad_accum})")
    print(f"  lr:             {args.lr}")
    print(f"  LoRA:           r={args.lora_r}, alpha={args.lora_alpha}, dropout={args.lora_dropout}")
    print(f"  device:         {args.device}\n")

    torch.manual_seed(args.seed)

    if not TRAIN_PATH.exists() or not VAL_PATH.exists():
        print(f"ABORT: training data missing — run scripts/build_reranker_training_data.py first")
        return 1

    device = torch.device(args.device)
    # MPS supports BF16 mixed precision via torch.autocast
    dtype = torch.bfloat16 if args.device != "cpu" else torch.float32

    print("Loading tokenizer + base model...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=1)
    model = model.to(device)

    # Apply LoRA — XLM-RoBERTa attention modules are named query / key / value
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="SEQ_CLS",
        target_modules=["query", "value"],
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    print()

    train_ds = _RerankerDataset(TRAIN_PATH, tokenizer)
    val_ds = _RerankerDataset(VAL_PATH, tokenizer)
    print(f"Loaded {len(train_ds)} train / {len(val_ds)} val rows\n")

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=lambda b: _collate(b, tokenizer),
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        collate_fn=lambda b: _collate(b, tokenizer),
    )

    # Optimizer + schedule
    n_steps_per_epoch = math.ceil(len(train_loader) / args.grad_accum)
    total_steps = n_steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * DEFAULT_WARMUP_FRAC)

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=args.lr,
        weight_decay=DEFAULT_WEIGHT_DECAY,
    )
    scheduler = get_linear_schedule_with_warmup(optimizer, warmup_steps, total_steps)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    history: list[dict] = []
    best_val_loss = float("inf")
    epochs_no_improve = 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for epoch in range(args.epochs):
        print(f"--- Epoch {epoch + 1}/{args.epochs} ---")
        t0 = time.time()
        model.train()
        running_loss = 0.0
        n_train_seen = 0

        for step, batch in enumerate(train_loader):
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["labels"].to(device)
            with torch.autocast(device_type=device.type, dtype=dtype):
                outputs = model(input_ids=input_ids, attention_mask=attention_mask)
                logits = outputs.logits.squeeze(-1)
                loss = loss_fn(logits, labels) / args.grad_accum

            loss.backward()
            running_loss += loss.item() * args.grad_accum * labels.size(0)
            n_train_seen += labels.size(0)

            if (step + 1) % args.grad_accum == 0 or (step + 1) == len(train_loader):
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)

        train_loss = running_loss / max(n_train_seen, 1)
        val_metrics = _eval(model, val_loader, device, dtype)
        elapsed = time.time() - t0

        log = {
            "epoch": epoch + 1,
            "train_loss": train_loss,
            "val_loss": val_metrics["val_loss"],
            "val_acc": val_metrics["acc"],
            "val_mean_pos_prob": val_metrics["mean_pos_prob"],
            "val_mean_neg_prob": val_metrics["mean_neg_prob"],
            "elapsed_s": elapsed,
        }
        history.append(log)
        print(f"  train_loss={train_loss:.4f}  val_loss={val_metrics['val_loss']:.4f}  "
              f"val_acc={val_metrics['acc']:.3f}  pos_prob={val_metrics['mean_pos_prob']:.3f}  "
              f"neg_prob={val_metrics['mean_neg_prob']:.3f}  ({elapsed:.0f}s)")

        improved = val_metrics["val_loss"] < best_val_loss
        if improved:
            best_val_loss = val_metrics["val_loss"]
            epochs_no_improve = 0
            print(f"  ✅ new best val_loss — saving adapter")
            model.save_pretrained(OUT_DIR)
            log["saved"] = True
        else:
            epochs_no_improve += 1
            log["saved"] = False
            print(f"  no improvement ({epochs_no_improve}/{args.patience} until early stop)")

        if epochs_no_improve >= args.patience:
            print(f"\n⚠ Early stopping triggered at epoch {epoch + 1}")
            break

    # Save training metadata
    metadata = {
        "base_model": BASE_MODEL,
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "lora_dropout": args.lora_dropout,
        "target_modules": ["query", "value"],
        "epochs_run": len(history),
        "best_epoch": min(range(1, len(history) + 1), key=lambda i: history[i - 1]["val_loss"]),
        "best_val_loss": best_val_loss,
        "history": history,
        "args": vars(args),
    }
    (OUT_DIR / "training_metadata.json").write_text(json.dumps(metadata, indent=2))

    print()
    print("=" * 90)
    print("Done")
    print("=" * 90)
    print(f"  best val_loss: {best_val_loss:.4f} at epoch {metadata['best_epoch']}")
    print(f"  adapter:       {OUT_DIR}/")
    print(f"  metadata:      {OUT_DIR}/training_metadata.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
