#!/usr/bin/env bash
# 运行全部测试（标准库 unittest，-I 隔离模式，sys.path 显式注入项目根）。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -I -c "import sys, unittest; \
sys.path.insert(0, '.'); \
loader = unittest.TestLoader(); \
suite = loader.discover('tests', top_level_dir='.'); \
result = unittest.TextTestRunner(verbosity=2).run(suite); \
sys.exit(0 if result.wasSuccessful() else 1)"
