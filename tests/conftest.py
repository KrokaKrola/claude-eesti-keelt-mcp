"""Tests don't write to the working log.

server.py opens data/server.log on import, and test runs left lines with
foreign paths and a made-up topic «12. Poes» there — mixed in with real
sessions. The variable is set before the server's first import: conftest
loads before the test modules.
"""
import os

os.environ["ANKI_VOCAB_LOG_FILE"] = ""
os.environ["ANKI_VOCAB_JOURNAL"] = ""
