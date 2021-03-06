#!/usr/bin/env python

import argparse
import logging
import pathlib
import pickle
import time

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as func

import molecular_cross_validation as mcv
import molecular_cross_validation.train

from molecular_cross_validation.models.autoencoder import CountAutoencoder
from molecular_cross_validation.train.aggmo import AggMo

import molecular_cross_validation.util as ut


def mse_loss_cpu(y_pred: torch.Tensor, y_true: torch.Tensor) -> torch.Tensor:
    y_pred = y_pred.detach().cpu()

    return func.mse_loss(y_pred, y_true)


def adjusted_mse_loss_cpu(
    y_pred: torch.Tensor, y_true: torch.Tensor, a: torch.Tensor, b: torch.Tensor
) -> torch.Tensor:
    y_pred = y_pred.detach().cpu()

    y_pred = torch.from_numpy(
        ut.convert_expectations(y_pred.numpy(), a.numpy(), b.numpy())
    ).to(torch.float)

    return func.mse_loss(y_pred, y_true)


def poisson_nll_loss_cpu(y_pred: torch.Tensor, y_true: torch.Tensor) -> torch.Tensor:
    y_pred = y_pred.detach().cpu()

    return func.poisson_nll_loss(y_pred, y_true)


def adjusted_poisson_nll_loss_cpu(
    y_pred: torch.Tensor, y_true: torch.Tensor, a: torch.Tensor, b: torch.Tensor
) -> torch.Tensor:
    y_pred = y_pred.detach().cpu()

    return func.poisson_nll_loss(y_pred - torch.log(a) + torch.log(b), y_true)


def main():
    parser = argparse.ArgumentParser()

    run_group = parser.add_argument_group("run", description="Per-run parameters")
    run_group.add_argument("--seed", type=int, required=True)
    run_group.add_argument(
        "--data_split", type=float, default=0.9, help="Split for self-supervision"
    )
    run_group.add_argument("--gpu", type=int, required=True)

    data_group = parser.add_argument_group(
        "data", description="Input and output parameters"
    )
    data_group.add_argument("--dataset", type=pathlib.Path, required=True)
    data_group.add_argument("--output_dir", type=pathlib.Path, required=True)

    model_group = parser.add_argument_group("model", description="Model parameters")

    loss_group = model_group.add_mutually_exclusive_group(required=True)
    loss_group.add_argument(
        "--mse",
        action="store_const",
        const="mse",
        dest="loss",
        help="mean squared error",
    )
    loss_group.add_argument(
        "--pois",
        action="store_const",
        const="pois",
        dest="loss",
        help="poisson likelihood",
    )

    model_group.add_argument(
        "--layers",
        nargs="+",
        type=int,
        metavar="L",
        default=[128],
        help="Layers in the input/output networks",
    )
    model_group.add_argument(
        "--max_bottleneck",
        type=int,
        default=7,
        metavar="B",
        help="max bottleneck (log2)",
    )
    model_group.add_argument(
        "--learning_rate", type=float, default=0.1, metavar="LR", help="learning rate"
    )
    model_group.add_argument(
        "--dropout", type=float, default=0.0, metavar="P", help="dropout probability"
    )

    args = parser.parse_args()

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())

    logger.info(f"torch version {torch.__version__}")

    dataset_name = args.dataset.parent.name
    output_file = (
        args.output_dir / f"{dataset_name}_autoencoder_{args.loss}_{args.seed}.pickle"
    )

    logger.info(f"writing output to {output_file}")

    seed = sum(map(ord, f"biohub_{args.seed}"))
    random_state = np.random.RandomState(seed)

    device = torch.device(f"cuda:{args.gpu}")

    torch.backends.cudnn.deterministic = True
    torch.manual_seed(seed)

    with open(args.dataset, "rb") as f:
        true_means, true_counts, umis = pickle.load(f)

    n_features = umis.shape[-1]

    bottlenecks = [2 ** i for i in range(args.max_bottleneck + 1)]
    bottlenecks.extend(3 * b // 2 for b in bottlenecks[1:-1])
    bottlenecks.sort()

    logger.info(f"testing bottlenecks {bottlenecks}")

    if max(bottlenecks) > max(args.layers):
        raise ValueError("Max bottleneck width is larger than your network layers")

    rec_loss = np.empty(len(bottlenecks), dtype=float)
    mcv_loss = np.empty_like(rec_loss)
    gt0_loss = np.empty_like(rec_loss)
    gt1_loss = np.empty_like(rec_loss)

    data_split, data_split_complement, overlap = ut.overlap_correction(
        args.data_split, umis.sum(1, keepdims=True) / true_counts
    )

    if args.loss == "mse":
        exp_means = ut.expected_sqrt(true_means * umis.sum(1, keepdims=True))
        exp_split_means = ut.expected_sqrt(
            true_means * data_split_complement * umis.sum(1, keepdims=True)
        )

        exp_means = torch.from_numpy(exp_means).to(torch.float)
        exp_split_means = torch.from_numpy(exp_split_means).to(torch.float)

        loss_fn = nn.MSELoss()
        normalization = "sqrt"
        input_t = nn.Identity()
        eval0_fn = mse_loss_cpu
        eval1_fn = adjusted_mse_loss_cpu
    else:
        assert args.loss == "pois"
        exp_means = true_means * umis.sum(1, keepdims=True)
        exp_split_means = data_split_complement * exp_means

        exp_means = torch.from_numpy(exp_means).to(torch.float)
        exp_split_means = torch.from_numpy(exp_split_means).to(torch.float)

        loss_fn = nn.PoissonNLLLoss()
        normalization = "log1p"
        input_t = torch.log1p
        eval0_fn = poisson_nll_loss_cpu
        eval1_fn = adjusted_poisson_nll_loss_cpu

    model_factory = lambda bottleneck: CountAutoencoder(
        n_input=n_features,
        n_latent=bottleneck,
        layers=args.layers,
        use_cuda=True,
        dropout_rate=args.dropout,
    )

    optimizer_factory = lambda m: AggMo(
        m.parameters(), lr=args.learning_rate, betas=[0.0, 0.9, 0.99], weight_decay=1e-7
    )

    scheduler_kw = {"t_max": 256, "eta_min": args.learning_rate / 100.0, "factor": 1.0}

    train_losses = []
    val_losses = []

    full_train_losses = []
    full_val_losses = []

    with torch.cuda.device(device):
        umis_X, umis_Y = ut.split_molecules(umis, data_split, overlap, random_state)

        if args.loss == "mse":
            umis = np.sqrt(umis)
            umis_X = np.sqrt(umis_X)
            umis_Y = np.sqrt(umis_Y)

        umis = torch.from_numpy(umis).to(torch.float).to(device)
        umis_X = torch.from_numpy(umis_X).to(torch.float).to(device)
        umis_Y = torch.from_numpy(umis_Y).to(torch.float)
        data_split = torch.from_numpy(
            np.broadcast_to(data_split, (umis.shape[0], 1))
        ).to(torch.float)
        data_split_complement = torch.from_numpy(
            np.broadcast_to(data_split_complement, (umis.shape[0], 1))
        ).to(torch.float)

        sample_indices = random_state.permutation(umis.size(0))
        n_train = int(0.875 * umis.size(0))

        train_dl, val_dl = mcv.train.split_dataset(
            umis_X,
            umis_Y,
            exp_split_means,
            data_split,
            data_split_complement,
            batch_size=len(sample_indices),
            indices=sample_indices,
            n_train=n_train,
        )

        full_train_dl, full_val_dl = mcv.train.split_dataset(
            umis,
            exp_means,
            batch_size=len(sample_indices),
            indices=sample_indices,
            n_train=n_train,
        )

        t0 = time.time()

        for j, b in enumerate(bottlenecks):
            logger.info(f"testing bottleneck width {b}")
            model = model_factory(b)
            optimizer = optimizer_factory(model)

            train_loss, val_loss = mcv.train.train_until_plateau(
                model,
                loss_fn,
                optimizer,
                train_dl,
                val_dl,
                input_t=input_t,
                min_cycles=3,
                threshold=0.001,
                scheduler_kw=scheduler_kw,
            )
            train_losses.append(train_loss)
            val_losses.append(val_loss)

            rec_loss[j] = train_loss[-1]
            mcv_loss[j] = mcv.train.evaluate_epoch(
                model, eval1_fn, train_dl, input_t, eval_i=[1, 3, 4]
            )
            gt1_loss[j] = mcv.train.evaluate_epoch(
                model, eval1_fn, train_dl, input_t, eval_i=[2, 3, 4]
            )

            model = model_factory(b)
            optimizer = optimizer_factory(model)

            full_train_loss, full_val_loss = mcv.train.train_until_plateau(
                model,
                loss_fn,
                optimizer,
                full_train_dl,
                full_val_dl,
                input_t=input_t,
                min_cycles=3,
                threshold=0.001,
                scheduler_kw=scheduler_kw,
            )

            full_train_losses.append(full_train_loss)
            full_val_losses.append(full_val_loss)

            logger.debug(f"finished {b} after {time.time() - t0} seconds")

            gt0_loss[j] = eval0_fn(model(input_t(umis)), exp_means)

    results = {
        "dataset": dataset_name,
        "method": "autoencoder",
        "loss": args.loss,
        "normalization": normalization,
        "param_range": bottlenecks,
        "rec_loss": rec_loss,
        "mcv_loss": mcv_loss,
        "gt0_loss": gt0_loss,
        "gt1_loss": gt1_loss,
        "train_losses": train_losses,
        "val_losses": val_losses,
        "full_train_losses": full_train_losses,
        "full_val_losses": full_val_losses,
    }

    with open(output_file, "wb") as out:
        pickle.dump(results, out)


if __name__ == "__main__":
    main()
