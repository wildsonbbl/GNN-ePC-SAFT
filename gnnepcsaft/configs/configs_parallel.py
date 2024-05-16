"""Module to get configs for model hyperparameters in parallel training"""


def get_configs() -> dict:
    """Get the hyperparameter configurations."""
    configs = [
        {
            "propagation_depth": 3,
            "hidden_dim": 64,
            "num_mlp_layers": 0,
            "pre_layers": 2,
            "post_layers": 1,
            "skip_connections": True,
            "add_self_loops": True,
        },
        {
            "propagation_depth": 3,
            "hidden_dim": 64,
            "num_mlp_layers": 0,
            "pre_layers": 2,
            "post_layers": 1,
            "skip_connections": True,
            "add_self_loops": True,
        },
    ]
    return configs
