#!/usr/bin/env python3
"""可信登记命令行入口。直接运行 ``python3 run_cli.py --help`` 查看用法。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from task_domain_002.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
