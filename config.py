"""配置管理 — 命令行参数 + 环境变量"""

import argparse
import os


class Config:
    def __init__(self):
        parser = argparse.ArgumentParser(description="实战答辩评分系统")
        parser.add_argument("--db", default="sqlite", choices=["sqlite", "mysql"],
                          help="数据库类型 (default: sqlite)")
        parser.add_argument("--port", type=int, default=8888,
                          help="服务端口 (default: 8888)")
        parser.add_argument("--host", default="0.0.0.0",
                          help="绑定地址 (default: 0.0.0.0)")

        # MySQL 相关（仅 --db mysql 时需要）
        parser.add_argument("--mysql-host", default="localhost")
        parser.add_argument("--mysql-port", type=int, default=3306)
        parser.add_argument("--mysql-user", default="root")
        parser.add_argument("--mysql-password", default="")
        parser.add_argument("--mysql-db", default="evaluation")

        args = parser.parse_args()

        self.DB_TYPE = args.db
        self.PORT = args.port
        self.HOST = args.host

        # SQLite
        self.SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evaluation.db")

        # MySQL
        self.MYSQL_HOST = args.mysql_host
        self.MYSQL_PORT = args.mysql_port
        self.MYSQL_USER = args.mysql_user
        self.MYSQL_PASSWORD = args.mysql_password
        self.MYSQL_DATABASE = args.mysql_db

        # 管理员默认凭据
        self.ADMIN_USERNAME = "admin"
        self.ADMIN_PASSWORD = "admin123"

        # Session 密钥
        self.SECRET_KEY = "evaluation-secret-key-change-in-production"


config = Config()
