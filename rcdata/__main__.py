"""``python -m RevenueChaser.rcdata backup`` (see ``rcdata/backup.py``). The builder is ``rcdata.build``."""
import sys

from .backup import main

sys.exit(main())
