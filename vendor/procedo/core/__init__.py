"""procedo.core — shared project / bundle / pack / equipment foundation.

This package sits *below* both the main app (``procedo.bench``) and the
workflow editor (``procedo.studio``) so they can share the same on-disk
project format, bundle registry, config and pack-data extraction without an
``editor → main`` import edge (which would be a cycle).  Nothing here imports
``procedo.bench``; core reaches the editor repo only lazily, inside a ``try``
(the engine registry, ``procedo.engine_client``), never at module load.
"""

from pathlib import Path

__version__ = "0.1.0"


def get_config_dir() -> Path:
    """Return the project-root ``app_config/`` directory.

    Layout (relative to project root)::

        app_config/
            config.json          <- unified config (profiles + patterns)
            templates/
                reports/
                    report_template.html
                exports/
                    word_export_template.docx

    The path is resolved from this file:
    ``src/procedo/core/__init__.py`` -> up 4 levels -> ``app_config/``.
    """
    return Path(__file__).resolve().parent.parent.parent.parent / "app_config"
