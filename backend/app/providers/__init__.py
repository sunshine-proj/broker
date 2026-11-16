"""Provider framework and adapters.

Importing this package imports every adapter module, which registers the
adapter with the factory.
"""

from app.providers import coingecko, twelve_data  # noqa: F401
