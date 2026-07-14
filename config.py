"""配置管理 — 命令行参数 + 环境变量（便于 Docker 部署）

优先级：命令行参数 > 环境变量 > 默认值。
数据（SQLite 库 + 会话文件）默认放在项目目录；设置 EVAL_DATA_DIR 可整体
重定向到挂载卷，从而让容器重建 / 升级都不丢成绩。
"""

import argparse
import os


def _env(key: str, default: str) -> str:
    """读取环境变量；空串视为未设置。"""
    v = os.environ.get(key)
    return v if v not in (None, "") else default


class Config:
    def __init__(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # 数据默认放项目下 data/ 子目录，让「python main.py」与 Docker（挂载 ./data:/data）
        # 共用同一份成绩库与会话；EVAL_DATA_DIR 可覆盖。
        data_dir = _env("EVAL_DATA_DIR", os.path.join(base_dir, "data"))

        parser = argparse.ArgumentParser(description="实战答辩评分系统")
        parser.add_argument("--db", default=_env("EVAL_DB", "sqlite"), choices=["sqlite", "mysql"],
                          help="数据库类型 (env EVAL_DB, default: sqlite)")
        parser.add_argument("--port", type=int, default=int(_env("EVAL_PORT", "8888")),
                          help="服务端口 (env EVAL_PORT, default: 8888)")
        parser.add_argument("--host", default=_env("EVAL_HOST", "0.0.0.0"),
                          help="绑定地址 (env EVAL_HOST, default: 0.0.0.0)")

        # MySQL 相关（仅 --db mysql 时需要）
        parser.add_argument("--mysql-host", default=_env("EVAL_MYSQL_HOST", "localhost"))
        parser.add_argument("--mysql-port", type=int, default=int(_env("EVAL_MYSQL_PORT", "3306")))
        parser.add_argument("--mysql-user", default=_env("EVAL_MYSQL_USER", "root"))
        parser.add_argument("--mysql-password", default=_env("EVAL_MYSQL_PASSWORD", ""))
        parser.add_argument("--mysql-db", default=_env("EVAL_MYSQL_DB", "evaluation"))

        # 忽略未知参数（如在 pytest / uvicorn 等宿主下 import 时的多余 argv）
        args, _ = parser.parse_known_args()

        self.DB_TYPE = args.db
        self.PORT = args.port
        self.HOST = args.host

        # 数据落盘位置（可整体挂载卷）
        os.makedirs(data_dir, exist_ok=True)
        self.DATA_DIR = data_dir
        self.SQLITE_PATH = _env("EVAL_DB_PATH", os.path.join(data_dir, "evaluation.db"))
        self.SESSION_DIR = _env("EVAL_SESSION_DIR", os.path.join(data_dir, ".sessions"))

        # MySQL
        self.MYSQL_HOST = args.mysql_host
        self.MYSQL_PORT = args.mysql_port
        self.MYSQL_USER = args.mysql_user
        self.MYSQL_PASSWORD = args.mysql_password
        self.MYSQL_DATABASE = args.mysql_db

        # 管理员默认凭据 & Session 密钥（生产环境请用环境变量覆盖）
        self.ADMIN_USERNAME = _env("EVAL_ADMIN_USER", "admin")
        self.ADMIN_PASSWORD = _env("EVAL_ADMIN_PASSWORD", "admin123")
        self.SECRET_KEY = _env("EVAL_SECRET_KEY", "evaluation-secret-key-change-in-production")


config = Config()
