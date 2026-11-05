#!/usr/bin/env python3
"""Имитация gigacode для локальной проверки: python server.py --cmd "python3 tests/fake_gigacode.py"."""
import sys
import time

args = sys.argv[1:]
if args[:2] != ["--approval-mode", "auto-edit"] or args[2] != "-p" or len(args) != 4:
    sys.stderr.write("bad args: {!r}\n".format(args))
    sys.exit(2)
prompt = args[3]
if "FAIL" in prompt:
    sys.stderr.write("Error: simulated failure\n")
    sys.exit(1)
if "SLOW" in prompt:
    time.sleep(30)

reply = (
    "Получил запрос длиной **{} символов**.\n\n"
    "Последняя строка: `{}`\n\n"
    "```python\nprint('привет, мир')\n```\n\n- пункт один\n- пункт два\n"
).format(len(prompt), prompt.strip().splitlines()[-1][:80])
for word in reply.split(" "):
    sys.stdout.write(word + " ")
    sys.stdout.flush()
    time.sleep(0.02)
