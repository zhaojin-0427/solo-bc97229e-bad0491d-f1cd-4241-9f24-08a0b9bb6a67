"""全局配置：全部可通过环境变量覆盖，默认满足题面约束。"""
import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"环境变量 {name} 必须为正整数，收到: {raw!r}")
    return value


class Config:
    # 逻辑网格尺寸上限（行、列）。逻辑网格不预分配，仅作为越界校验边界。
    GRID_ROWS = _int_env("ELEV_GRID_ROWS", 100_000)
    GRID_COLS = _int_env("ELEV_GRID_COLS", 100_000)

    # 存储块固定 64×64（题面要求）。
    BLOCK_SIZE = 64

    # 单次上传 / 单个窗口允许的最大行列数。
    MAX_UPLOAD_ROWS = _int_env("ELEV_MAX_UPLOAD_ROWS", 256)
    MAX_UPLOAD_COLS = _int_env("ELEV_MAX_UPLOAD_COLS", 256)
    MAX_WINDOW_ROWS = _int_env("ELEV_MAX_WINDOW_ROWS", 256)
    MAX_WINDOW_COLS = _int_env("ELEV_MAX_WINDOW_COLS", 256)

    # HTTP 请求体上限（默认 16 MiB，256×256 整数 CSV 足够）。
    MAX_BODY_BYTES = _int_env("ELEV_MAX_BODY_BYTES", 16 * 1024 * 1024)

    # SQLite 写锁等待（毫秒），并发上传在此排队。
    BUSY_TIMEOUT_MS = _int_env("ELEV_BUSY_TIMEOUT_MS", 30_000)

    # 数据目录：SQLite 索引 + 64×64 块文件 + 临时文件。
    DATA_DIR = os.environ.get("ELEV_DATA_DIR", "/data")

    @property
    def db_path(self) -> str:
        return os.path.join(self.DATA_DIR, "elevation.sqlite3")

    @property
    def blocks_dir(self) -> str:
        return os.path.join(self.DATA_DIR, "blocks")

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.DATA_DIR, "tmp")

    def ensure_dirs(self) -> None:
        os.makedirs(self.DATA_DIR, exist_ok=True)
        os.makedirs(self.blocks_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)


CONFIG = Config()
