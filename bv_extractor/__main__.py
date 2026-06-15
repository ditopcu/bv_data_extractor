"""Package entry point: launch the desktop GUI wizard.

    python -m bv_extractor

The command-line interface remains available at:

    python -m bv_extractor.cli <pdf> -o <output_dir>
"""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
