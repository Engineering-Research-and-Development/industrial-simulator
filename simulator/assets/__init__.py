from .base import Asset
from .conveyor import Conveyor
from .producer import Producer
from .sink import Sink
from .splitter import Splitter
from .transformer import Transformer

ASSET_KINDS: dict[str, type[Asset]] = {
    "producer": Producer,
    "transformer": Transformer,
    "splitter": Splitter,
    "sink": Sink,
}

__all__ = ["Asset", "Conveyor", "Producer", "Sink", "Splitter", "Transformer", "ASSET_KINDS"]
