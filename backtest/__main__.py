import argparse
from .server import serve

parser = argparse.ArgumentParser(description="OKX USDT 永续策略回测工具")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--cache", help="行情及结果缓存路径（默认仓库的 .backtest-cache）")
args = parser.parse_args()
serve(args.port, args.cache)
