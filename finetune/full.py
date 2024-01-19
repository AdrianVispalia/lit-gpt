# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.

import os
import sys
import time
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import lightning as L
import torch
from lightning.fabric.loggers import CSVLogger
from lightning.fabric.strategies import FSDPStrategy
from lightning.fabric.utilities import ThroughputMonitor, measure_flops
from lightning.pytorch.loggers import WandbLogger
from torchmetrics.aggregation import RunningMean

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from generate.base import generate
from lit_gpt.model import GPT, Block, Config
from lit_gpt.tokenizer import Tokenizer
from lit_gpt.utils import (
    check_valid_checkpoint_dir,
    chunked_cross_entropy,
    get_default_supported_precision,
    load_checkpoint,
    num_parameters,
)
from scripts.prepare_alpaca import generate_prompt

eval_interval = 30000000000000
save_interval = 1000
eval_iters = 100
eval_max_new_tokens = 100
log_interval = 1
devices = 2 # torch.cuda.device_count()

# Hyperparameters
learning_rate = 1e-5
batch_size = 64 / devices
micro_batch_size = 4
gradient_accumulation_iters = int(batch_size // micro_batch_size)
assert gradient_accumulation_iters > 0
max_seq_length = None  # assign value to truncate
epoch_size = 1468252  # train dataset size
num_epochs = 1
max_iters = num_epochs * (epoch_size // micro_batch_size) // devices
weight_decay = 0.00
warmup_steps = int(0.1 * 1 * (epoch_size // micro_batch_size) // devices // gradient_accumulation_iters)

hparams = {k: v for k, v in locals().items() if isinstance(v, (int, float, str)) and not k.startswith("_")}


def setup(
    data_dir: Path = Path("data/ultrachat3"),
    checkpoint_dir: Path = Path("checkpoints/lit-tiny-llama/lit-tiny-llama-3.0T"),
    out_dir: Path = Path("out/full/lit-tiny-llama-finetuned"),
) -> None:

    fabric_devices = devices
    if fabric_devices > 1:
        strategy = FSDPStrategy(
            auto_wrap_policy={Block},
            activation_checkpointing_policy={Block},
            state_dict_type="full",
            limit_all_gathers=True,
            cpu_offload=False,
        )
    else:
        strategy = "auto"

    logger = WandbLogger(project="tinyllama-finetune", name="lit-tiny-llama-1.1b")
    fabric = L.Fabric(devices=fabric_devices, strategy=strategy, precision="bf16-true", loggers=logger)
    fabric.print(hparams)
    fabric.launch(main, data_dir, checkpoint_dir, out_dir)


def main(fabric: L.Fabric, data_dir: Path, checkpoint_dir: Path, out_dir: Path) -> None:
    check_valid_checkpoint_dir(checkpoint_dir)

    fabric.seed_everything(1337)  # same seed for every process to init model (FSDP)

    if fabric.global_rank == 0:
        os.makedirs(out_dir, exist_ok=True)

    fabric.print("Loading dataset ...")
    train_data = torch.load(data_dir / "train.bin", mmap=True)
    val_data = torch.load(data_dir / "test.bin", mmap=True)

    config = Config.from_name(name="tiny-llama-1.1b")
    checkpoint_path = checkpoint_dir / "lit_model.pth"
    fabric.print(f"Loading model {str(checkpoint_path)!r} with {config.__dict__}")
    with fabric.init_module(empty_init=(devices > 1)):
        model = GPT(config)

    fabric.print(f"Number of trainable parameters: {num_parameters(model, requires_grad=True):,}")

    model = fabric.setup_module(model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    optimizer = fabric.setup_optimizers(optimizer)

    load_checkpoint(fabric, model, checkpoint_path)

    fabric.seed_everything(1337 + fabric.global_rank)

    train_time = time.perf_counter()
    train(fabric, model, optimizer, train_data, val_data, checkpoint_dir, out_dir)
    fabric.print(f"Training time: {(time.perf_counter()-train_time):.2f}s")
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB")

    # Save the final checkpoint at the end of training
    save_path = out_dir / "lit_model_finetuned.pth"
    save_checkpoint(fabric, model, save_path)


def train(
    fabric: L.Fabric,
    model: GPT,
    optimizer: torch.optim.Optimizer,
    train_data: List[Dict],
    val_data: List[Dict],
    checkpoint_dir: Path,
    out_dir: Path,
) -> None:
    tokenizer = Tokenizer(checkpoint_dir)
    # longest_seq_length, longest_seq_ix = get_longest_seq_length(train_data)
    # model.max_seq_length = min(longest_seq_length, max_seq_length or float("inf"))
    # fabric.print(
    #     f"The longest sequence length in the train data is {longest_seq_length}, the model's maximum sequence length is"
    #     f" {model.max_seq_length} and context length is {model.config.block_size}"
    # )
    longest_seq_ix = None
    longest_seq_length = model.max_seq_length

    # validate(fabric, model, val_data, tokenizer, max_iters=2)  # sanity check

    throughput = ThroughputMonitor(fabric, window_size=5)

    with torch.device("meta"):
        meta_model = GPT(model.config)
        x = torch.randint(0, 1, (micro_batch_size, meta_model.config.block_size))
        model_fwd = lambda: meta_model(x)
        model_loss = lambda y: chunked_cross_entropy(y, x, chunk_size=0)
        measured_flops = measure_flops(meta_model, model_fwd, model_loss)
        fabric.print(f"Measured TFLOPs: {measured_flops * fabric.world_size / 1e12:.2f}")
        del meta_model, x

    running_loss = RunningMean(window=gradient_accumulation_iters, sync_on_compute=False).to(fabric.device)
    step_count = 0
    total_lengths = 0
    total_t0 = time.perf_counter()

    for iter_num in range(1, max_iters + 1):
        if step_count <= warmup_steps:
            # linear warmup
            lr = learning_rate * step_count / warmup_steps
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr

        iter_t0 = time.perf_counter()

        input_ids, targets = get_batch(fabric, train_data, longest_seq_ix if iter_num == 1 else None)
        # input_ids = input_ids[:, 1:]
        # targets = targets[:, 1:]
        input_ids = input_ids[:, :-1]
        targets = targets[:, 1:]

        is_accumulating = iter_num % gradient_accumulation_iters != 0
        with fabric.no_backward_sync(model, enabled=is_accumulating):
            logits = model(input_ids)
            # fabric.print("input", input_ids[0].tolist())
            # fabric.print("target", targets[0].tolist())
            loss = chunked_cross_entropy(logits, targets)
            # loss = chunked_cross_entropy(logits, targets, chunk_size=0)
            fabric.backward(loss / gradient_accumulation_iters)

        running_loss.update(loss.detach())

        if not is_accumulating:
            optimizer.step()
            fabric.clip_gradients(model, optimizer, max_norm=1.0)
            optimizer.zero_grad()
            step_count += 1

        total_lengths += input_ids.numel()
        if iter_num % log_interval == 0:
            loss = running_loss.compute().item()  # expensive device-to-host synchronization
            t1 = time.perf_counter()
            throughput.update(
                time=(t1 - total_t0),
                flops=(measured_flops * log_interval),
                batches=iter_num,
                samples=(iter_num * micro_batch_size),
                lengths=(iter_num * micro_batch_size * model.config.block_size),
            )
            metrics = {
                "loss": loss,
                "iter": iter_num,
                "step": step_count,
                "iter_time": t1 - iter_t0,
                "tokens": iter_num * micro_batch_size * model.config.block_size,
                "total_tokens": iter_num * micro_batch_size * model.config.block_size * fabric.world_size,
                "learning_rate": lr,
            }

            fabric.print(
                f"iter {iter_num} step {step_count}: loss {loss:.4f}, iter time:"
                f" {(t1 - iter_t0) * 1000:.2f}ms{' (optimizer.step)' if not is_accumulating else ''}"
            )

            throughput_metrics = throughput.compute()
            metrics.update(throughput_metrics)
            fabric.log_dict(metrics, step=iter_num)

        if not is_accumulating and step_count % eval_interval == 0:
            t0 = time.perf_counter()
            val_loss = validate(fabric, model, val_data, tokenizer, max_iters=eval_iters)
            t1 = time.perf_counter() - t0
            fabric.print(f"step {iter_num}: val loss {val_loss.item():.4f}, val time: {t1 * 1000:.2f}ms")
            metrics = {"val_loss": val_loss, "val_ppl": math.exp(val_loss)}
            fabric.log_dict(metrics, step=iter_num)
            fabric.barrier()
        if not is_accumulating and step_count % save_interval == 0:
            checkpoint_path = out_dir / f"iter-{iter_num:06d}-ckpt.pth"
            save_checkpoint(fabric, model, checkpoint_path)


# FSDP has issues with `inference_mode`
@torch.no_grad()
def validate(fabric: L.Fabric, model: GPT, val_data: List[Dict], tokenizer: Tokenizer, max_iters: int) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    losses = torch.zeros(max_iters)
    for k in range(max_iters):
        input_ids, targets = get_batch(fabric, val_data)
        logits = model(input_ids)
        losses[k] = chunked_cross_entropy(logits[..., :-1, :], targets[..., 1:], chunk_size=0)
    val_loss = losses.mean()

    # produce an example:
    instruction = "Recommend a movie for me to watch during the weekend and explain the reason."
    fabric.print(instruction)
    sample = {"instruction": instruction, "input": ""}
    prompt = generate_prompt(sample)
    encoded = tokenizer.encode(prompt, device=fabric.device)
    with fabric.init_tensor():
        # do not set `max_seq_length=max_returned_token` because memory is not a concern here
        model.set_kv_cache(batch_size=1)
    output = generate(
        model, encoded, max_returned_tokens=len(encoded) + eval_max_new_tokens, temperature=0.8, eos_id=tokenizer.eos_id
    )
    model.clear_kv_cache()
    output = tokenizer.decode(output)
    fabric.print(output)

    model.train()
    return val_loss


def get_batch(
    fabric: L.Fabric, data: List[Dict], longest_seq_ix: Optional[int] = None
) -> Tuple[torch.Tensor, torch.Tensor]:
    ix = torch.randint(len(data), (micro_batch_size,))
    if longest_seq_ix is not None:
        # force the longest sample at the beginning so potential OOMs happen right away
        ix[0] = longest_seq_ix

    input_ids = [data[i]["input_ids"].type(torch.int64) for i in ix]
    labels = [data[i]["labels"].type(torch.int64) for i in ix]

    # this could be `longest_seq_length` to have a fixed size for all batches
    max_len = max(len(s) for s in input_ids)

    def pad_right(x, pad_id):
        # pad right based on the longest sequence
        n = max_len - len(x)
        return torch.cat((x, torch.full((n,), pad_id, dtype=x.dtype)))

    x = torch.stack([pad_right(x, pad_id=0) for x in input_ids])
    y = torch.stack([pad_right(x, pad_id=-1) for x in labels])

    # Truncate if needed
    if max_seq_length:
        x = x[:, :max_seq_length]
        y = y[:, :max_seq_length]

    if fabric.device.type == "cuda" and x.device.type == "cpu":
        x, y = fabric.to_device((x.pin_memory(), y.pin_memory()))
    else:
        x, y = fabric.to_device((x, y))
    return x, y


def get_longest_seq_length(data: List[Dict]) -> Tuple[int, int]:
    # find out the minimum max_seq_length required during fine-tuning (saves memory!)
    lengths = [len(d["input_ids"]) for d in data]
    longest_seq_length = max(lengths)
    longest_seq_ix = lengths.index(longest_seq_length)
    return longest_seq_length, longest_seq_ix


def save_checkpoint(fabric: L.Fabric, model: torch.nn.Module, file_path: Path) -> None:
    fabric.print(f"Saving weights to {str(file_path)!r}")
    fabric.save(file_path, {"model": model})


if __name__ == "__main__":
    torch.set_float32_matmul_precision("high")

    from jsonargparse import CLI

    CLI(setup)
