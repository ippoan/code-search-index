"""判断層 (prototype): decide which search hits are worth injecting.

Kept out of `indexer/` on purpose — the daily index job installs only
requirements.txt, and nothing here runs in it. See ranker/README.md.
"""
