import torch
import torch.nn.functional as F
from torch.nn import Linear, ModuleList, ReLU, Sequential
from torch_geometric.nn import PNAConv, global_add_pool, LayerNorm
from torch_geometric.data import Data


class PNAPCSAFT(torch.nn.Module):
    def __init__(
        self,
        hidden_dim: int,
        propagation_depth: int,
        pre_layers: int,
        post_layers: int,
        num_mlp_layers: int,
        num_para: int,
        deg: torch.Tensor,
        layer_norm: bool,
        dtype: torch.FloatType,
        device: str,
    ):
        super().__init__()

        aggregators = ["mean", "min", "max", "std"]
        scalers = ["identity", "amplification", "attenuation"]
        self.num_mlp_layers = num_mlp_layers
        self.dtype = dtype
        self.device = device
        self.convs = ModuleList()
        self.batch_norms = ModuleList()

        if layer_norm:
            layer_fn = torch.nn.LayerNorm
        else:
            layer_fn = torch.nn.Identity

        conv = PNAConv(
            in_channels=9,
            out_channels=hidden_dim,
            aggregators=aggregators,
            scalers=scalers,
            deg=deg,
            edge_dim=3,
            towers=4,
            pre_layers=pre_layers,
            post_layers=post_layers,
            divide_input=False,
        )
        self.convs.append(conv)
        self.batch_norms.append(LayerNorm(hidden_dim))

        for _ in range(propagation_depth - 1):
            conv = PNAConv(
                in_channels=hidden_dim,
                out_channels=hidden_dim,
                aggregators=aggregators,
                scalers=scalers,
                deg=deg,
                edge_dim=3,
                towers=4,
                pre_layers=pre_layers,
                post_layers=post_layers,
                divide_input=False,
            )
            self.convs.append(conv)
            self.batch_norms.append(LayerNorm(hidden_dim))

        self.mlp = Sequential(
            Linear(hidden_dim, hidden_dim),
            layer_fn(hidden_dim),
            ReLU(),
            Linear(hidden_dim, hidden_dim),
            layer_fn(hidden_dim),
            ReLU(),
        )
        self.ouput = Sequential(
            Linear(hidden_dim, hidden_dim // 2),
            ReLU(),
            Linear(hidden_dim // 2, hidden_dim // 4),
            ReLU(),
            Linear(hidden_dim // 4, num_para),
        )

    def forward(
        self,
        data: Data,
    ):
        x, edge_index, edge_attr, batch = (
            data.x.to(self.dtype),
            data.edge_index.to(torch.int64),
            data.edge_attr.to(self.dtype),
            data.batch,
        )

        for conv, batch_norm in zip(self.convs, self.batch_norms):
            x = F.relu(batch_norm(conv(x, edge_index, edge_attr)))

        x = global_add_pool(x, batch)
        for _ in range(self.num_mlp_layers - 1):
            x = self.mlp(x)
        x = self.ouput(x)
        return x.abs()


class PNAThermo(torch.nn.Module):
    def __init__(
        self,
        hidden_dim: int,
        propagation_depth: int,
        pre_layers: int,
        post_layers: int,
        num_mlp_layers: int,
        num_para: int,
        deg: torch.Tensor,
        layer_norm: bool,
        dtype: torch.FloatType,
        device: str,
    ):
        super().__init__()

        aggregators = ["mean", "min", "max", "std"]
        scalers = ["identity", "amplification", "attenuation"]
        self.num_mlp_layers = num_mlp_layers
        self.dtype = dtype
        self.device = device
        self.convs = ModuleList()
        self.batch_norms = ModuleList()

        if layer_norm:
            layer_fn = torch.nn.LayerNorm
        else:
            layer_fn = torch.nn.Identity

        conv = PNAConv(
            in_channels=9,
            out_channels=hidden_dim,
            aggregators=aggregators,
            scalers=scalers,
            deg=deg,
            edge_dim=3,
            towers=4,
            pre_layers=pre_layers,
            post_layers=post_layers,
            divide_input=False,
        )
        self.convs.append(conv)
        self.batch_norms.append(LayerNorm(hidden_dim))

        for _ in range(propagation_depth - 1):
            conv = PNAConv(
                in_channels=hidden_dim,
                out_channels=hidden_dim,
                aggregators=aggregators,
                scalers=scalers,
                deg=deg,
                edge_dim=3,
                towers=4,
                pre_layers=pre_layers,
                post_layers=post_layers,
                divide_input=False,
            )
            self.convs.append(conv)
            self.batch_norms.append(LayerNorm(hidden_dim))

        self.mlp = Sequential(
            Linear(hidden_dim, hidden_dim),
            layer_fn(hidden_dim),
            ReLU(),
            Linear(hidden_dim, hidden_dim),
            layer_fn(hidden_dim),
            ReLU(),
        )
        self.ouput = Sequential(
            Linear(hidden_dim, hidden_dim // 2),
            ReLU(),
            Linear(hidden_dim // 2, hidden_dim // 4),
            ReLU(),
            Linear(hidden_dim // 4, num_para),
        )
        self.statefn = Sequential(
            Linear(5, hidden_dim),
            ReLU(),
        )

    def forward(
        self,
        data: Data,
        state: torch.tensor,
    ):
        x, edge_index, edge_attr, batch = (
            data.x.to(self.dtype),
            data.edge_index.to(torch.int64),
            data.edge_attr.to(self.dtype),
            data.batch,
        )

        for conv, batch_norm in zip(self.convs, self.batch_norms):
            x = F.relu(batch_norm(conv(x, edge_index, edge_attr)))

        x = global_add_pool(x, batch)
        x = self.statefn(state) * x
        for _ in range(self.num_mlp_layers - 1):
            x = self.mlp(x)
        x = self.ouput(x)
        return x.abs()