"""kratheos-food-data: rebuilds one aggregated food database from open sources.

The package downloads open food-composition databases (Open Food Facts, USDA
FoodData Central, Ciqual, the Swiss Food Composition Database, ...), converts
each of them to one common schema with a per-source adapter, and writes an
aggregated database with a manifest. See the README for the whole picture.
"""

__version__ = "0.1.0"
