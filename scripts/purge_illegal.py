#!/usr/bin/env python3
"""One-time purge of illegal/CSAM content. Safe to run multiple times."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from crawler.models import purge_illegal_pages

if __name__ == '__main__':
    print("Starting illegal content purge...")
    deleted = purge_illegal_pages()
    print(f"Done. Removed {deleted} pages from index.")
