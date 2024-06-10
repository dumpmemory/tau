# Copyright (c) Meta Platforms, Inc. and affiliates

# Minimum effort to run this example:
# $ torchrun --nproc-per-node 4 pippy_deberta.py

import argparse
import os

import torch
import torch.distributed as dist
from torch.distributed.pipelining import pipeline, ScheduleGPipe, SplitPoint

from transformers import DebertaModel, DebertaConfig

from hf_utils import generate_inputs_for_model, get_number_of_params


def run(args):
    # Model configs
    config = DebertaConfig()
    print("Using device:", args.device)

    # Create model
    model_class = DebertaModel
    model_name = "DebertaModel"
    deberta = model_class(config)
    deberta.to(args.device)
    deberta.eval()
    if args.rank == 0:
        print(deberta.config)
        print(f"Total number of params = {get_number_of_params(deberta) // 10 ** 6}M")
        print(deberta)

    # Example microbatch inputs
    mb_inputs = generate_inputs_for_model(
        model_class, deberta, model_name, args.batch_size // args.chunks, args.device)

    # Split points
    layers_per_rank = deberta.config.num_hidden_layers // args.world_size
    split_spec = {
        f"encoder.layer.{i * layers_per_rank}": SplitPoint.BEGINNING
        for i in range(1, args.world_size)
    }

    # Create pipeline
    pipe = pipeline(
        deberta,
        mb_args=(),
        mb_kwargs=mb_inputs,
        split_spec=split_spec,
    )

    assert pipe.num_stages == args.world_size, f"nstages = {pipe.num_stages} nranks = {args.world_size}"
    smod = pipe.get_stage_module(args.rank)
    print(f"Pipeline stage {args.rank} {get_number_of_params(smod) // 10 ** 6}M params")

    # Create schedule runtime
    stage = pipe.build_stage(
        args.rank,
        device=args.device,
    )

    # Attach to a schedule
    schedule = ScheduleGPipe(stage, args.chunks)

    # Full batch inputs as in single-worker case
    inputs = generate_inputs_for_model(
        model_class, deberta, model_name, args.batch_size, args.device)

    # Run
    if args.rank == 0:
        schedule.step(**inputs)
    else:
        out = schedule.step()

    dist.barrier()
    dist.destroy_process_group()
    print(f"Rank {args.rank} completes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--world_size', type=int, default=int(os.getenv("WORLD_SIZE", 4)))
    parser.add_argument('--rank', type=int, default=int(os.getenv("RANK", -1)))
    parser.add_argument('--master_addr', type=str, default=os.getenv('MASTER_ADDR', 'localhost'))
    parser.add_argument('--master_port', type=str, default=os.getenv('MASTER_PORT', '29500'))
    parser.add_argument('--schedule', type=str, default="FillDrain")
    parser.add_argument('--cuda', type=int, default=int(torch.cuda.is_available()))
    parser.add_argument("--chunks", type=int, default=4)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--batches', type=int, default=1)

    args = parser.parse_args()

    if args.cuda:
        dev_id = args.rank % torch.cuda.device_count()
        args.device = torch.device(f"cuda:{dev_id}")
    else:
        args.device = torch.device("cpu")

    # Init process group
    backend = "nccl" if args.cuda else "gloo"
    dist.init_process_group(
        backend=backend,
        rank=args.rank,
        world_size=args.world_size,
    )

    run(args)
