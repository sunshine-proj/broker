"""Provider framework and adapters.

Importing this package imports every adapter module, which registers the
adapter with the factory.
"""

from app.providers import (  # noqa: F401
    binance,
    coingecko,
    ecb,
    fred,
    nyfed,
    twelve_data,
    us_treasury,
)
