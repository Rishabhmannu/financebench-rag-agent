"""Sprint 7.17 Phase 2: LoRA fine-tune cross-encoder/ms-marco-MiniLM-L-6-v2 on FB labels.

Parallel to Sprint 7.9 reranker LoRA FT but on a different base model:
  - Reranker FT: BAAI/bge-reranker-v2-m3 (568M, XLM-RoBERTa-large)
  - Grader FT:   cross-encoder/ms-marco-MiniLM-L-6-v2 (22.7M, MiniLM-L6, MS-MARCO
                 binary classifier)

Risk per "When Fine-Tuning Fails" (arXiv 2506.18535): MS-MARCO-pretrained base
can be saturated for MS-MARCO-distribution tasks. Mitigation: FB is OOD
(financial documents vs MS MARCO web passages), and we run 3 negative-sampling
strategies (random / hard / mixed) to find the regime where transfer works.

Output: data/models/grader_ft_v1_{strategy}_r{rank}/
  adapter_config.json           — PEFT config
  adapter_model.safetensors     — LoRA weights only (~5-15 MB on MiniLM)
  training_metadata.json        — epoch-by-epoch loss/acc + final pick
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

BASE_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DATA_ROOT = Path("data/training/grader_ft_v1")
OUT_ROOT = Path("data/models/grader_ft_v1")

MAX_LENGTH = 512
DEFAULT_EPOCHS = 5
DEFAULT_BATCH_SIZE = 16  # MiniLM is small, can use larger batches
DEFAULT_GRAD_ACCUM = 1
DEFAULT_LR = 2e-4
DEFAULT_WEIGHT_DECAY = 0.01
DEFAULT_WARMUP_FRAC = 0.10
DEFAULT_LORA_R = 8
DEFAULT_LORA_ALPHA = 16
DEFAULT_LORA_DROPOUT = 0.1
DEFAULT_PATIENCE = 2


class _PairDataset(Dataset):
    def __init__(self, jsonl_path: Path, tokenizer):
        self.rows = [json.loads(l) for l in jsonl_path.open()]
        self.tok = tokenizer

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        r = self.rows[idx]
        enc = self.tok(r["query"], r["chunk"], max_length=MAX_LENGTH,
                       truncation=True, padding=False, return_tensors=None)
        return {"input_ids": enc["input_ids"],
                "attention_mask": enc["attention_mask"],
                "label": float(r["label"]),
                "fb_id": r["fb_id"]}


def _collate(batch, tokenizer):
    input_ids = [b["input_ids"] for b in batch]
    attention_mask = [b["attention_mask"] for b in batch]
    pad = tokenizer.pad_token_id
    max_len = max(len(x) for x in input_ids)
    pid = torch.full((len(batch), max_len), pad, dtype=torch.long)
    pmk = torch.zeros((len(batch), max_len), dtype=torch.long)
    for i, (ids, mk) in enumerate(zip(input_ids, attention_mask)):
        pid[i, :len(ids)] = torch.tensor(ids, dtype=torch.long)
        pmk[i, :len(mk)] = torch.tensor(mk, dtype=torch.long)
    labels = torch.tensor([b["label"] for b in batch], dtype=torch.float32)
    return {"input_ids": pid, "attention_mask": pmk, "labels": labels}


def _binary_metrics(logits, labels):
    probs = torch.sigmoid(logits)
    preds = (probs >= 0.5).float()
    acc = (preds == labels).float().mean().item()
    pos_mask = labels == 1
    neg_mask = labels == 0
    return {
        "acc": acc,
        "pos_recall": preds[pos_mask].mean().item() if pos_mask.any() else float("nan"),
        "neg_recall": (1 - preds[neg_mask]).mean().item() if neg_mask.any() else float("nan"),
        "mean_pos_prob": probs[pos_mask].mean().item() if pos_mask.any() else float("nan"),
        "mean_neg_prob": probs[neg_mask].mean().item() if neg_mask.any() else float("nan"),
    }


def _eval(model, loader, device, dtype):
    model.eval()
    total_loss = 0.0
    total_count = 0
    all_logits, all_labels = [], []
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="sum")
    with torch.no_grad():
        for batch in loader:
            iids = batch["input_ids"].to(device)
            mk = batch["attention_mask"].to(device)
            lbl = batch["labels"].to(device)
            with torch.autocast(device_type=device.type, dtype=dtype):
                out = model(input_ids=iids, attention_mask=mk)
                logits = out.logits.squeeze(-1)
                loss = loss_fn(logits, lbl)
            total_loss += loss.item()
            total_count += lbl.size(0)
            all_logits.append(logits.detach().float().cpu())
            all_labels.append(lbl.detach().float().cpu())
    lc = torch.cat(all_logits)
    lbc = torch.cat(all_labels)
    m = _binary_metrics(lc, lbc)
    m["val_loss"] = total_loss / max(total_count, 1)
    return m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["random", "hard", "mixed"], required=True)
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

    strategy = args.strategy
    train_path = DATA_ROOT / strategy / "train.jsonl"
    val_path = DATA_ROOT / strategy / "val.jsonl"
    out_dir = OUT_ROOT.with_name(f"{OUT_ROOT.name}_{strategy}_r{args.lora_r}")

    print("=" * 90)
    print(f"Sprint 7.17 Phase 2 — grader LoRA FT  [strategy={strategy}, rank={args.lora_r}]")
    print("=" * 90)
    print(f"  base:         {BASE_MODEL}")
    print(f"  train:        {train_path}")
    print(f"  val:          {val_path}")
    print(f"  output:       {out_dir}")
    print(f"  epochs:       {args.epochs}  patience={args.patience}")
    print(f"  batch_size:   {args.batch_size}  grad_accum={args.grad_accum}")
    print(f"  LoRA:         r={args.lora_r}, alpha={args.lora_alpha}, dropout={args.lora_dropout}")
    print(f"  lr:           {args.lr}  device={args.device}\n")

    torch.manual_seed(args.seed)
    if not train_path.exists() or not val_path.exists():
        print(f"ABORT: training data missing at {train_path.parent}")
        return 1

    device = torch.device(args.device)
    dtype = torch.bfloat16 if args.device != "cpu" else torch.float32

    print(f"Loading tokenizer + base model ({BASE_MODEL})...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=1)
    model = model.to(device)

    # MiniLM uses BERT-style modules: 'query', 'value', 'key'
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

    train_ds = _PairDataset(train_path, tokenizer)
    val_ds = _PairDataset(val_path, tokenizer)
    print(f"Loaded {len(train_ds)} train / {len(val_ds)} val rows\n")

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                               collate_fn=lambda b: _collate(b, tokenizer))
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                             collate_fn=lambda b: _collate(b, tokenizer))

    n_steps_per_epoch = math.ceil(len(train_loader) / args.grad_accum)
    total_steps = n_steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * DEFAULT_WARMUP_FRAC)

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                    lr=args.lr, weight_decay=DEFAULT_WEIGHT_DECAY)
    scheduler = get_linear_schedule_with_warmup(optimizer, warmup_steps, total_steps)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    history = []
    best_val_loss = float("inf")
    epochs_no_improve = 0
    out_dir.mkdir(parents=True, exist_ok=True)

    for epoch in range(args.epochs):
        print(f"--- Epoch {epoch + 1}/{args.epochs} ---")
        t0 = time.time()
        model.train()
        running_loss = 0.0
        n_train_seen = 0

        for step, batch in enumerate(train_loader):
            iids = batch["input_ids"].to(device)
            mk = batch["attention_mask"].to(device)
            lbl = batch["labels"].to(device)
            with torch.autocast(device_type=device.type, dtype=dtype):
                out = model(input_ids=iids, attention_mask=mk)
                logits = out.logits.squeeze(-1)
                loss = loss_fn(logits, lbl) / args.grad_accum
            loss.backward()
            running_loss += loss.item() * args.grad_accum * lbl.size(0)
            n_train_seen += lbl.size(0)
            if (step + 1) % args.grad_accum == 0 or (step + 1) == len(train_loader):
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)

        train_loss = running_loss / max(n_train_seen, 1)
        vm = _eval(model, val_loader, device, dtype)
        elapsed = time.time() - t0
        log = {
            "epoch": epoch + 1, "train_loss": train_loss,
            "val_loss": vm["val_loss"], "val_acc": vm["acc"],
            "val_pos_recall": vm["pos_recall"], "val_neg_recall": vm["neg_recall"],
            "val_mean_pos_prob": vm["mean_pos_prob"], "val_mean_neg_prob": vm["mean_neg_prob"],
            "elapsed_s": elapsed,
        }
        history.append(log)
        print(f"  train_loss={train_loss:.4f}  val_loss={vm['val_loss']:.4f}  "
              f"val_acc={vm['acc']:.3f}  pos_recall={vm['pos_recall']:.3f}  "
              f"neg_recall={vm['neg_recall']:.3f}  ({elapsed:.0f}s)")

        if vm["val_loss"] < best_val_loss:
            best_val_loss = vm["val_loss"]
            epochs_no_improve = 0
            print(f"  new best val_loss — saving adapter")
            model.save_pretrained(out_dir)
            log["saved"] = True
        else:
            epochs_no_improve += 1
            log["saved"] = False
            print(f"  no improvement ({epochs_no_improve}/{args.patience} until early stop)")
        if epochs_no_improve >= args.patience:
            print(f"\nEarly stopping at epoch {epoch + 1}")
            break

    metadata = {
        "base_model": BASE_MODEL, "strategy": strategy,
        "lora_r": args.lora_r, "lora_alpha": args.lora_alpha, "lora_dropout": args.lora_dropout,
        "target_modules": ["query", "value"],
        "epochs_run": len(history),
        "best_epoch": min(range(1, len(history) + 1), key=lambda i: history[i - 1]["val_loss"]),
        "best_val_loss": best_val_loss,
        "history": history, "args": vars(args),
    }
    (out_dir / "training_metadata.json").write_text(json.dumps(metadata, indent=2))

    print()
    print("=" * 90)
    print(f"  best val_loss: {best_val_loss:.4f} at epoch {metadata['best_epoch']}")
    print(f"  adapter:       {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
