"""Machine-learning layer: modular models plus the registry that trains,
stores and serves them. Nothing outside this package imports a model class
directly - the ensemble asks the registry for a prediction and gets an
honest "not available" whenever there isn't a validated model to serve."""
from ml.registry import predict, train_symbol, model_status, available_backends  # noqa: F401
