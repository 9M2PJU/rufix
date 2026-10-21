#!/usr/bin/env python3
"""Run Rufix straight from the source tree."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rufix.app import main  # noqa: E402

sys.exit(main())
