"""Model with important functions to help model training"""

import os.path as osp
import time
from tempfile import TemporaryDirectory
from typing import Any

import ml_collections
import numpy as np
import torch
import torch_geometric.transforms as T
from absl import logging
from lightning import LightningModule, Trainer
from lightning.pytorch.callbacks import Callback
from ray import train
from ray.train import Checkpoint
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, ReduceLROnPlateau
from torch_geometric.loader import DataLoader
from torch_geometric.transforms import BaseTransform
from torch_geometric.utils import degree

from ..data.graphdataset import Esper, Ramirez, ThermoMLDataset
from ..epcsaft.utils import pure_den_feos, pure_vp_feos
from . import models


def calc_deg(dataset: str, workdir: str) -> torch.Tensor:
    """Calculates deg for `PNAPCSAFT` model."""
    if dataset == "ramirez":
        path = osp.join(workdir, "data/ramirez2022")
        train_dataset = Ramirez(path)
    elif dataset in ("esper", "esper_assoc"):
        path = osp.join(workdir, "data/esper2023")
        train_dataset = Esper(path)
    else:
        raise ValueError(
            f"dataset is either ramirez or thermoml, got >>> {dataset} <<< instead"
        )
    # Compute the maximum in-degree in the training data.
    max_degree = -1
    for data in train_dataset:
        d = degree(data.edge_index[1], num_nodes=data.num_nodes, dtype=torch.long)
        max_degree = max(max_degree, int(d.max()))

    # Compute the in-degree histogram tensor
    deg = torch.zeros(max_degree + 1, dtype=torch.long)
    for data in train_dataset:
        d = degree(data.edge_index[1], num_nodes=data.num_nodes, dtype=torch.long)
        deg += torch.bincount(d, minlength=deg.numel())
    return deg


def create_model(
    config: ml_collections.ConfigDict, deg: torch.Tensor
) -> torch.nn.Module:
    """Creates a model, as specified by the config."""

    pna_params = models.PnaconvsParams(
        propagation_depth=config.propagation_depth,
        pre_layers=config.pre_layers,
        post_layers=config.post_layers,
        deg=deg,
        skip_connections=config.skip_connections,
        self_loops=config.add_self_loops,
    )
    mlp_params = models.ReadoutMLPParams(
        num_mlp_layers=config.num_mlp_layers,
        num_para=config.num_para,
        dropout=config.dropout_rate,
    )

    if config.model == "PNA":

        return models.PNAPCSAFT(
            hidden_dim=config.hidden_dim,
            pna_params=pna_params,
            mlp_params=mlp_params,
        )
    if config.model == "PNAL":
        return models.PNApcsaftL(
            pna_params=pna_params,
            mlp_params=mlp_params,
            config=config,
        )

    raise ValueError(f"Unsupported model: {config.model}.")


def create_optimizer(config: ml_collections.ConfigDict, params):
    """Creates an optimizer, as specified by the config."""
    if config.optimizer == "adam":
        return torch.optim.AdamW(
            params,
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
            amsgrad=True,
            eps=1e-5,
        )
    if config.optimizer == "sgd":
        return torch.optim.SGD(
            params,
            lr=config.learning_rate,
            momentum=config.momentum,
            weight_decay=config.weight_decay,
            nesterov=True,
        )
    raise ValueError(f"Unsupported optimizer: {config.optimizer}.")


def savemodel(model, optimizer, scaler, path, step):
    """To checkpoint model during training."""
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scaler_state_dict": scaler.state_dict(),
            "step": step,
        },
        path,
    )


def mape(parameters: np.ndarray, rho: np.ndarray, vp: np.ndarray, mean: bool = True):
    """
    Calculates mean absolute percentage error
    of ePC-SAFT predicted density and vapor pressurre
    relative to experimental data.

    """
    parameters = np.abs(parameters)
    if parameters.size < 8:
        zeros = np.zeros(
            5,
        )
        parameters = np.concatenate([parameters, zeros], axis=0)
    pred_mape = [0.0]
    if ~np.all(rho == np.zeros_like(rho)):
        pred_mape = []
        for state in rho:
            den = pure_den_feos(parameters, state)
            mape_den = np.abs((state[-1] - den) / state[-1])
            if mape_den > 1:  # against algorithm fail
                continue
            pred_mape += [mape_den]

    den = np.asarray(pred_mape)
    if mean:
        den = den.mean()

    pred_mape = [0.0]
    if ~np.all(vp == np.zeros_like(vp)):
        pred_mape = []
        for state in vp:
            try:
                vp_pred = pure_vp_feos(parameters, state)
                mape_vp = np.abs((state[-1] - vp_pred) / state[-1])
            except (AssertionError, RuntimeError):
                continue
            if mape_vp > 1:  # against algorithm fail
                continue
            pred_mape += [mape_vp]

    vp = np.asarray(pred_mape)
    if mean:
        vp = vp.mean()

    return den, vp


def rhovp_data(parameters: np.ndarray, rho: np.ndarray, vp: np.ndarray):
    """Calculates density and vapor pressure with ePC-SAFT"""
    parameters = np.abs(parameters)
    den = []
    if ~np.all(rho == np.zeros_like(rho)):
        for state in rho:
            den += [pure_den_feos(parameters, state)]
    den = np.asarray(den)

    vpl = []
    if ~np.all(vp == np.zeros_like(vp)):
        for state in vp:
            try:
                vpl += [pure_vp_feos(parameters, state)]
            except (AssertionError, RuntimeError):
                continue
    vp = np.asarray(vpl)

    return den, vp


def create_schedulers(config, optimizer):
    "Creates lr schedulers."

    class Noop:
        """Dummy noop scheduler"""

        def step(self, *args, **kwargs):
            """Scheduler step"""

        def __getattr__(self, _):
            return self.step

    if config.change_sch:
        scheduler = Noop()
        scheduler2 = ReduceLROnPlateau(
            optimizer,
            mode="min",
            patience=config.patience,
            verbose=True,
            cooldown=config.patience,
            min_lr=1e-15,
            eps=1e-15,
        )
    else:
        scheduler = CosineAnnealingWarmRestarts(optimizer, config.warmup_steps)
        scheduler2 = Noop()
    return scheduler, scheduler2


# pylint: disable= R0913,R0917
def load_checkpoint(config, workdir, model, optimizer, scaler, device):
    "Loads saved model checkpoints."
    ckp_path = osp.join(workdir, "train/checkpoints/last_checkpoint.pth")
    initial_step = 1
    if osp.exists(ckp_path):
        checkpoint = torch.load(ckp_path, map_location=device)
        model.load_state_dict(checkpoint["model_state_dict"])
        if not config.change_opt:
            optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scaler.load_state_dict(checkpoint["scaler_state_dict"])
        step = checkpoint["step"]
        initial_step = int(step) + 1
        del checkpoint
    return ckp_path, initial_step


def build_datasets_loaders(config, workdir, dataset):
    "Builds train and test dataset loader."
    train_dataset = build_train_dataset(workdir, dataset)
    train_loader = DataLoader(train_dataset, batch_size=config.batch_size, shuffle=True)

    test_loader, para_data = build_test_dataset(workdir, train_dataset)
    return train_loader, test_loader, para_data


# pylint: disable=R0903
class TransformParameters(BaseTransform):
    "To add parameters to test dataset."

    def __init__(self, para_data: dict) -> None:
        self.para_data = para_data

    def forward(self, data: Any) -> Any:
        if data.InChI in self.para_data:
            data.para, data.assoc, data.munanb = self.para_data[data.InChI]
        else:
            data.para, data.assoc, data.munanb = (
                torch.zeros(3),
                torch.zeros(2),
                torch.zeros(3),
            )
        return data


def build_test_dataset(workdir, train_dataset, transform=None):
    "Builds test dataset."

    para_data = {}
    if isinstance(train_dataset, Esper):
        for graph in train_dataset:
            inchi, para, assoc, munanb = (
                graph.InChI,
                graph.para,
                graph.assoc,
                graph.munanb,
            )
            para_data[inchi] = (para, assoc, munanb)
    if transform:
        transform = T.Compose([TransformParameters(para_data), transform])
    else:
        transform = TransformParameters(para_data)
    tml_dataset = ThermoMLDataset(
        osp.join(workdir, "data/thermoml"), transform=transform
    )
    test_idx = []
    val_idx = []
    # separate test and val dataset
    for idx, graph in enumerate(tml_dataset):
        if graph.InChI in para_data:
            val_idx.append(idx)
        else:
            test_idx.append(idx)
    test_dataset = tml_dataset[test_idx]
    val_dataset = tml_dataset[val_idx]
    return val_dataset, test_dataset


def build_train_dataset(workdir, dataset, transform=None):
    "Builds train dataset."
    if dataset == "ramirez":
        path = osp.join(workdir, "data/ramirez2022")
        train_dataset = Ramirez(path, transform=transform)
    elif dataset in ("esper", "esper_assoc"):
        path = osp.join(workdir, "data/esper2023")
        train_dataset = Esper(path, transform=transform)
    else:
        raise ValueError(
            f"dataset is either ramirez, esper or esper_assoc, got >>> {dataset} <<< instead"
        )

    return train_dataset


def input_artifacts(workdir: str, dataset: str, model="last_checkpoint"):
    "Creates input wandb artifacts"
    # pylint: disable=C0415
    import wandb

    if dataset == "ramirez":
        ramirez_path = workdir + "/data/ramirez2022"
        ramirez_art = wandb.Artifact(name="ramirez", type="dataset")
        ramirez_art.add_dir(local_path=ramirez_path, name="ramirez2022")
        wandb.use_artifact(ramirez_art)
    if dataset == "thermoml":
        thermoml_path = workdir + "/data/thermoml"
        thermoml_art = wandb.Artifact(name="thermoml", type="dataset")
        thermoml_art.add_dir(local_path=thermoml_path, name="thermoml")
        wandb.use_artifact(thermoml_art)
    model_path = workdir + f"/train/checkpoints/{model}.pth"
    model_art = wandb.Artifact(name="model", type="model")
    if osp.exists(model_path):
        model_art.add_file(local_path=model_path, name="last_checkpoint.pth")
        wandb.use_artifact(model_art)


def output_artifacts(workdir: str):
    "Creates output wandb artifacts"
    # pylint: disable=C0415
    import wandb

    model_path = workdir + "/train/checkpoints/last_checkpoint.pth"
    model_art = wandb.Artifact(name="model", type="model")
    if osp.exists(model_path):
        model_art.add_file(local_path=model_path, name="last_checkpoint.pth")
        wandb.log_artifact(model_art)


class EpochTimer(Callback):
    "Elapsed time counter."

    start_time: float

    def on_train_epoch_start(
        self, trainer: Trainer, pl_module: LightningModule
    ) -> None:
        self.start_time = time.time()

    def on_train_epoch_end(self, trainer: Trainer, pl_module: LightningModule) -> None:
        end_time = time.time()

        elapsed_time = end_time - self.start_time
        logging.log_first_n(
            logging.INFO, "Elapsed time %.4f min.", 20, elapsed_time / 60
        )


# taking vp data off for performance boost
# pylint: disable=R0903
class VpOff(BaseTransform):
    "take vp data off thermoml dataset"

    def forward(self, data: Any) -> Any:

        data.vp = torch.zeros(1, 5)
        return data


class CustomRayTrainReportCallback(Callback):
    "Custom ray tuner checkpoint."

    def on_validation_end(self, trainer, pl_module):

        with TemporaryDirectory() as tmpdir:
            # Fetch metrics
            metrics = trainer.callback_metrics
            metrics = {k: v.item() for k, v in metrics.items()}

            # Add customized metrics
            metrics["epoch"] = trainer.current_epoch
            metrics["step"] = trainer.global_step

            checkpoint = None
            global_rank = train.get_context().get_world_rank()
            trial_id = train.get_context().get_trial_id()
            if global_rank == 0:
                # Save model checkpoint file to tmpdir
                ckpt_path = osp.join(tmpdir, f"{trial_id}.pt")
                trainer.save_checkpoint(ckpt_path, weights_only=False)

                checkpoint = Checkpoint.from_directory(tmpdir)

            # Report to train session
            train.report(metrics=metrics, checkpoint=checkpoint)
