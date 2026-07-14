"""数据库抽象层 — 统一 SQLite / MySQL 接口"""

import csv
import hashlib
import io
import sqlite3
from typing import Any

from config import config


# ============================================================
# 默认评分标准数据
# ============================================================
DEFAULT_CRITERIA = [
    {
        "label": "需求分析 + 技术架构 + 可行性分析",
        "sort_order": 1,
        "options": [
            {"label": "缺失明显或明显应付", "description": "", "score": 1, "sort_order": 1},
            {"label": "有但不够完整或略粗糙", "description": "", "score": 2, "sort_order": 2},
            {"label": "三项完整、清晰、合理", "description": "", "score": 3, "sort_order": 3},
        ],
    },
    {
        "label": "团队分工与协作",
        "sort_order": 2,
        "options": [
            {"label": "分工模糊或明显一人完成", "description": "", "score": 1, "sort_order": 1},
            {"label": "有分工，但配合一般", "description": "", "score": 2, "sort_order": 2},
            {"label": "分工明确，协作顺畅，答辩体现各成员角色", "description": "", "score": 3, "sort_order": 3},
        ],
    },
    {
        "label": "PPT质量 + 讲解表现",
        "sort_order": 3,
        "options": [
            {"label": "PPT混乱或讲解卡顿严重", "description": "", "score": 1, "sort_order": 1},
            {"label": "中规中矩，能讲清楚", "description": "", "score": 2, "sort_order": 2},
            {"label": "结构清晰，重点突出，讲解流畅自然", "description": "", "score": 3, "sort_order": 3},
        ],
    },
    {
        "label": "问题发现与解决",
        "sort_order": 4,
        "options": [
            {"label": "未提及或问题太假", "description": "", "score": 1, "sort_order": 1},
            {"label": "有问题但解决过程不清晰", "description": "", "score": 2, "sort_order": 2},
            {"label": "明确提出真实问题 + 解决思路/过程", "description": "", "score": 3, "sort_order": 3},
        ],
    },
    {
        "label": "项目完成度",
        "sort_order": 5,
        "options": [
            {"label": "功能残缺或演示中途失败", "description": "", "score": 1, "sort_order": 1},
            {"label": "核心功能完成，有少量 bug 或未完成边缘功能", "description": "", "score": 2, "sort_order": 2},
            {"label": "功能完整，无明显 bug，可演示全流程", "description": "", "score": 3, "sort_order": 3},
        ],
    },
    {
        "label": "情感分（团队认同 / 态度 / 氛围）",
        "sort_order": 6,
        "options": [
            {"label": "缺乏热情或明显划水", "description": "", "score": 0, "sort_order": 0},
            {"label": "中规中矩，不反感", "description": "", "score": 1, "sort_order": 1},
            {"label": "有感染力，让人愿意支持", "description": "", "score": 3, "sort_order": 3},
        ],
    },
]


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _validate_selections(criteria: list[dict], selections: list[dict]):
    """按本班评分表（get_criteria 的结果）校验一次提交。

    返回 ``(status, total, details)``：
      - ``status``：``"ok"`` / ``"invalid_option"``（选项不属于本班维度，或同一维度重复提交）
        / ``"incomplete"``（未覆盖全部维度）。
      - ``total``：服务端按数据库分值算出的总分。
      - ``details``：``[(criterion_id, option_id, score, criterion_label), ...]``，分值与维度名均来自数据库。

    前端传来的 score 一概不采信，杜绝畸形 / 伪造 / 自动重放的提交污染成绩。
    """
    if not criteria:
        return "invalid_option", 0, []
    valid = {cr["id"]: {o["id"]: (o["score"], cr["label"]) for o in cr["options"]}
             for cr in criteria}
    seen, total, details = set(), 0, []
    for sel in selections or []:
        cid = sel.get("criterion_id")
        oid = sel.get("option_id")
        if cid not in valid or oid not in valid[cid] or cid in seen:
            return "invalid_option", 0, []
        seen.add(cid)
        score, label = valid[cid][oid]
        total += score
        details.append((cid, oid, score, label))
    if seen != set(valid.keys()):
        return "incomplete", 0, []
    return "ok", total, details


def _rows_to_csv(criteria: list[dict], rows) -> str:
    """把评分明细行聚合为规范 CSV 文本。

    入参 rows 每行含：scorer_name, scorer_group, target_group, total_score,
    comment, criterion_id, criterion_score（同一份评分会因 JOIN 展开成多行）。
    用 csv 模块输出，逗号/引号/换行一律正确转义，杜绝撑列串行；
    行分隔符为 \\r\\n，配合导出时的 UTF-8 BOM，Excel(Windows) 可直接正确打开。
    """
    scorer_rows: dict[tuple, dict] = {}
    for row in rows:
        key = (row["scorer_name"], row["scorer_group"], row["target_group"])
        if key not in scorer_rows:
            scorer_rows[key] = {
                "total_score": row["total_score"],
                "comment": row["comment"] or "",
                "criteria_scores": {},
            }
        if row["criterion_id"] is not None:
            scorer_rows[key]["criteria_scores"][row["criterion_id"]] = row["criterion_score"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["评分人", "评分人所在组", "被评组号"]
                    + [f"{cr['label']}(分)" for cr in criteria]
                    + ["总分", "评语"])
    for key, data in scorer_rows.items():
        writer.writerow([key[0], key[1], key[2]]
                        + [data["criteria_scores"].get(cr["id"], "") for cr in criteria]
                        + [data["total_score"], data["comment"]])
    return buf.getvalue()


# ============================================================
# SQLite 实现
# ============================================================
class SQLiteDB:
    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        return self

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    # ---------- 初始化 ----------
    def init_db(self):
        conn = self._get()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(50) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL
            );

            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(50) NOT NULL,
                group_number INTEGER NOT NULL,
                class_name VARCHAR(100) DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label VARCHAR(100) NOT NULL,
                sort_order INTEGER DEFAULT 0,
                class_name VARCHAR(100) DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS criteria_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criterion_id INTEGER NOT NULL,
                label VARCHAR(100) NOT NULL,
                description TEXT DEFAULT '',
                score INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0,
                FOREIGN KEY (criterion_id) REFERENCES criteria(id)
            );

            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scorer_name VARCHAR(50) NOT NULL,
                scorer_group INTEGER NOT NULL,
                scorer_class VARCHAR(100) DEFAULT '',
                target_group INTEGER NOT NULL,
                total_score INTEGER NOT NULL,
                comment TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scorer_name, target_group)
            );

            CREATE TABLE IF NOT EXISTS score_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score_id INTEGER NOT NULL,
                criterion_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                score INTEGER NOT NULL,
                criterion_label VARCHAR(100) DEFAULT '',
                FOREIGN KEY (score_id) REFERENCES scores(id)
            );

            CREATE TABLE IF NOT EXISTS criteria_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                skey VARCHAR(50) PRIMARY KEY,
                sval TEXT DEFAULT ''
            );
        """)

        # 回填：把已存在于学生表里的班级名登记到 classes 表（兼容旧库）
        conn.execute(
            "INSERT OR IGNORE INTO classes (name) "
            "SELECT DISTINCT class_name FROM students WHERE class_name != ''"
        )
        conn.commit()

        # 初始化默认管理员
        cur = conn.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                (config.ADMIN_USERNAME, _hash_password(config.ADMIN_PASSWORD)),
            )
            conn.commit()

        # 初始化默认评分标准（全局默认，class_name=''）
        cur = conn.execute("SELECT COUNT(*) FROM criteria")
        if cur.fetchone()[0] == 0:
            for cr in DEFAULT_CRITERIA:
                conn.execute(
                    "INSERT INTO criteria (label, sort_order, class_name) VALUES (?, ?, '')",
                    (cr["label"], cr["sort_order"]),
                )
                criterion_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for opt in cr["options"]:
                    conn.execute(
                        "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (criterion_id, opt["label"], opt["description"], opt["score"], opt["score"]),
                    )
            conn.commit()

        # 初始化默认评分模板
        cur = conn.execute("SELECT COUNT(*) FROM criteria_templates")
        if cur.fetchone()[0] == 0:
            import json
            criteria_data = []
            cur_c = conn.execute("SELECT id, label, sort_order FROM criteria WHERE class_name='' ORDER BY sort_order")
            for cr_row in cur_c:
                opts = []
                cur_o = conn.execute(
                    "SELECT label, description, score, sort_order FROM criteria_options WHERE criterion_id=? ORDER BY sort_order",
                    (cr_row["id"],),
                )
                for o in cur_o:
                    opts.append({"label": o["label"], "description": o["description"], "score": o["score"], "sort_order": o["sort_order"]})
                criteria_data.append({"label": cr_row["label"], "sort_order": cr_row["sort_order"], "options": opts})
            conn.execute(
                "INSERT INTO criteria_templates (name, data) VALUES (?, ?)",
                ("答辩评分标准", json.dumps(criteria_data, ensure_ascii=False)),
            )
            conn.commit()

        # 初始化测试班级和学生
        cur = conn.execute("SELECT COUNT(*) FROM students WHERE class_name = '测试班'")
        if cur.fetchone()[0] == 0:
            conn.execute("INSERT OR IGNORE INTO classes (name) VALUES ('测试班')")
            test_students = [
                ("张三", 1, "测试班"), ("李四", 1, "测试班"), ("王五", 2, "测试班"),
                ("赵六", 2, "测试班"), ("孙七", 3, "测试班"), ("周八", 3, "测试班"),
                ("吴九", 4, "测试班"), ("郑十", 4, "测试班"), ("陈十一", 5, "测试班"),
                ("林十二", 5, "测试班"), ("黄十三", 6, "测试班"), ("何十四", 6, "测试班"),
                ("刘十五", 7, "测试班"), ("杨十六", 7, "测试班"), ("朱十七", 8, "测试班"),
                ("马十八", 8, "测试班"),
            ]
            for name, group, cls in test_students:
                conn.execute(
                    "INSERT INTO students (name, group_number, class_name) VALUES (?, ?, ?)",
                    (name, group, cls),
                )
            conn.commit()
            # 为测试班复制一份独立的评分表（从全局默认）
            self.clone_criteria("", "测试班")

    # ---------- 学生 ----------
    def get_students(self, class_name: str = "") -> list[dict]:
        if class_name:
            rows = self._get().execute(
                "SELECT id, name, group_number, class_name FROM students WHERE class_name = ? ORDER BY group_number, name",
                (class_name,),
            ).fetchall()
        else:
            rows = self._get().execute(
                "SELECT id, name, group_number, class_name FROM students ORDER BY group_number, name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_classes(self) -> list[str]:
        """返回所有班级：classes 表登记的 ∪ 学生表里出现的（去重、按名称排序）。"""
        conn = self._get()
        registered = conn.execute("SELECT name FROM classes").fetchall()
        from_students = conn.execute(
            "SELECT DISTINCT class_name FROM students WHERE class_name != ''"
        ).fetchall()
        names = {r["name"] for r in registered} | {r["class_name"] for r in from_students}
        return sorted(names)

    def create_class(self, name: str, template_id: int | None = None):
        """登记一个班级（空班级也能存在），并为其生成一套独立的评分表。

        template_id 给定则用该模板生成；否则复制全局默认评分表。
        已存在维度的班级不会被覆盖（避免重建班级时清空已有评分表）。
        """
        name = (name or "").strip()
        if not name:
            return
        conn = self._get()
        conn.execute("INSERT OR IGNORE INTO classes (name) VALUES (?)", (name,))
        conn.commit()
        # 已有评分表则不动
        has = conn.execute(
            "SELECT COUNT(*) FROM criteria WHERE class_name = ?", (name,)
        ).fetchone()[0]
        if has:
            return
        if template_id:
            self.save_criteria(self.load_template(template_id), name)
        else:
            self.clone_criteria("", name)

    def get_class_students_count(self) -> dict:
        """返回 {class_name: count}"""
        rows = self._get().execute(
            "SELECT class_name, COUNT(*) as cnt FROM students GROUP BY class_name"
        ).fetchall()
        return {r["class_name"]: r["cnt"] for r in rows}

    def import_students(self, data: list[dict], class_name: str = "") -> tuple[int, int]:
        """导入学生列表，返回 (新增数, 跳过数)。按 姓名+班级 去重。"""
        conn = self._get()
        if class_name:
            self.create_class(class_name)
        added = 0
        skipped = 0
        for item in data:
            name = str(item.get("name", "")).strip()
            group = int(item.get("group", 0))
            if not name or group <= 0:
                continue
            existing = conn.execute(
                "SELECT COUNT(*) FROM students WHERE name = ? AND class_name = ?",
                (name, class_name),
            ).fetchone()[0]
            if existing:
                skipped += 1
            else:
                conn.execute(
                    "INSERT INTO students (name, group_number, class_name) VALUES (?, ?, ?)",
                    (name, group, class_name),
                )
                added += 1
        conn.commit()
        return added, skipped

    def add_student(self, name: str, group: int, class_name: str = ""):
        self._get().execute(
            "INSERT INTO students (name, group_number, class_name) VALUES (?, ?, ?)",
            (name, group, class_name),
        )
        self._get().commit()

    def delete_student(self, student_id: int):
        self._get().execute("DELETE FROM students WHERE id = ?", (student_id,))
        self._get().commit()

    def delete_class(self, class_name: str):
        """删除整个班级的所有数据（班级登记、学生、评分、评分明细、该班评分表）。"""
        conn = self._get()
        # 先删该班级学生所提交评分的明细，再删评分，避免孤儿
        conn.execute(
            "DELETE FROM score_details WHERE score_id IN "
            "(SELECT id FROM scores WHERE scorer_class = ?)",
            (class_name,),
        )
        conn.execute("DELETE FROM scores WHERE scorer_class = ?", (class_name,))
        conn.execute("DELETE FROM students WHERE class_name = ?", (class_name,))
        # 删该班专属评分表（维度+选项）
        for r in conn.execute("SELECT id FROM criteria WHERE class_name = ?", (class_name,)).fetchall():
            conn.execute("DELETE FROM criteria_options WHERE criterion_id = ?", (r["id"],))
        conn.execute("DELETE FROM criteria WHERE class_name = ?", (class_name,))
        conn.execute("DELETE FROM classes WHERE name = ?", (class_name,))
        conn.commit()

    # ---------- 评分标准 ----------
    def get_criteria(self, class_name: str = "") -> list[dict]:
        """返回某班级的评分标准（嵌套 options）。该班无专属标准时回退到全局默认（class_name=''）。"""
        conn = self._get()
        rows = conn.execute(
            "SELECT id, label, sort_order FROM criteria WHERE class_name = ? ORDER BY sort_order, id",
            (class_name,),
        ).fetchall()
        if class_name and not rows:
            rows = conn.execute(
                "SELECT id, label, sort_order FROM criteria WHERE class_name = '' ORDER BY sort_order, id"
            ).fetchall()
        result = []
        for row in rows:
            options = conn.execute(
                "SELECT id, label, description, score, sort_order "
                "FROM criteria_options WHERE criterion_id = ? ORDER BY sort_order, id",
                (row["id"],),
            ).fetchall()
            result.append({
                "id": row["id"],
                "label": row["label"],
                "sort_order": row["sort_order"],
                "options": [dict(o) for o in options],
            })
        return result

    def clone_criteria(self, src_class: str, dst_class: str):
        """把 src_class 的维度+选项复制为 dst_class 的一份（dst 已有则先清空）。"""
        if not dst_class:
            return
        conn = self._get()
        # 清掉目标班级已有的维度（含选项），保证幂等
        old = conn.execute("SELECT id FROM criteria WHERE class_name = ?", (dst_class,)).fetchall()
        for r in old:
            conn.execute("DELETE FROM criteria_options WHERE criterion_id = ?", (r["id"],))
        conn.execute("DELETE FROM criteria WHERE class_name = ?", (dst_class,))
        # 复制源班级维度
        src = conn.execute(
            "SELECT id, label, sort_order FROM criteria WHERE class_name = ? ORDER BY sort_order, id",
            (src_class,),
        ).fetchall()
        for cr in src:
            conn.execute(
                "INSERT INTO criteria (label, sort_order, class_name) VALUES (?, ?, ?)",
                (cr["label"], cr["sort_order"], dst_class),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            opts = conn.execute(
                "SELECT label, description, score, sort_order FROM criteria_options WHERE criterion_id = ? ORDER BY sort_order, id",
                (cr["id"],),
            ).fetchall()
            for o in opts:
                conn.execute(
                    "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_id, o["label"], o["description"], o["score"], o["sort_order"]),
                )
        conn.commit()

    def save_criteria(self, criteria: list[dict], class_name: str = ""):
        """保存某班级的评分标准。

        关键：尽量**保留已有维度的 id**（先按 id、再按 label 匹配并原地更新），
        只对真正新增的维度 INSERT；增删改都**限定在该班级范围内**，不影响其它班级。
        """
        conn = self._get()
        existing = {r["id"]: r["label"]
                    for r in conn.execute(
                        "SELECT id, label FROM criteria WHERE class_name = ?", (class_name,)
                    ).fetchall()}
        label_to_id = {}
        for cid, lab in existing.items():
            label_to_id.setdefault(lab, cid)
        kept_ids = set()

        for cr in criteria:
            cid = cr.get("id")
            # id 不可用时，尝试按 label 复用该班已有维度（兼容"加载模板后保存"）
            if not (cid and cid in existing):
                cid = label_to_id.get(cr["label"])
            if cid and cid in existing and cid not in kept_ids:
                conn.execute(
                    "UPDATE criteria SET label = ?, sort_order = ? WHERE id = ?",
                    (cr["label"], cr.get("sort_order", 0), cid),
                )
            else:
                conn.execute(
                    "INSERT INTO criteria (label, sort_order, class_name) VALUES (?, ?, ?)",
                    (cr["label"], cr.get("sort_order", 0), class_name),
                )
                cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            kept_ids.add(cid)

            # 选项整体重建（选项 id 不参与历史明细展示，分值存于 score_details.score）
            conn.execute("DELETE FROM criteria_options WHERE criterion_id = ?", (cid,))
            for opt in cr.get("options", []):
                conn.execute(
                    "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cid, opt["label"], opt.get("description", ""),
                     opt["score"], opt.get("sort_order", opt["score"])),
                )

        # 删除本次未提交的旧维度（仅限该班级）
        for old_id in set(existing) - kept_ids:
            conn.execute("DELETE FROM criteria_options WHERE criterion_id = ?", (old_id,))
            conn.execute("DELETE FROM criteria WHERE id = ?", (old_id,))

        conn.commit()

    def delete_criterion(self, criterion_id: int):
        conn = self._get()
        conn.execute("DELETE FROM criteria_options WHERE criterion_id = ?", (criterion_id,))
        conn.execute("DELETE FROM criteria WHERE id = ?", (criterion_id,))
        conn.commit()

    # ---------- 评分标准模板 ----------
    def save_template(self, name: str, criteria: list[dict]):
        """保存评分标准为模板"""
        import json
        conn = self._get()
        conn.execute(
            "INSERT INTO criteria_templates (name, data) VALUES (?, ?)",
            (name, json.dumps(criteria, ensure_ascii=False)),
        )
        conn.commit()

    def get_templates(self) -> list[dict]:
        """获取所有模板"""
        conn = self._get()
        rows = conn.execute(
            "SELECT id, name, created_at FROM criteria_templates ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def load_template(self, template_id: int) -> list[dict]:
        """加载模板数据"""
        import json
        conn = self._get()
        cur = conn.execute(
            "SELECT data FROM criteria_templates WHERE id = ?", (template_id,)
        )
        row = cur.fetchone()
        if row:
            return json.loads(row["data"])
        return []

    def delete_template(self, template_id: int):
        conn = self._get()
        conn.execute("DELETE FROM criteria_templates WHERE id = ?", (template_id,))
        conn.commit()

    # ---------- 评分 ----------
    def check_scored(self, name: str, target_group: int) -> bool:
        cur = self._get().execute(
            "SELECT COUNT(*) FROM scores WHERE scorer_name = ? AND target_group = ?",
            (name, target_group),
        )
        return cur.fetchone()[0] > 0

    def get_my_scores(self, name: str) -> list[dict]:
        rows = self._get().execute(
            "SELECT s.target_group, s.total_score, s.comment, s.created_at, s.scorer_class "
            "FROM scores s WHERE s.scorer_name = ? ORDER BY s.target_group",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def submit_score(self, name: str, scorer_group: int, target_group: int,
                     selections: list[dict], comment: str, scorer_class: str = "") -> int | str:
        conn = self._get()
        if self.check_scored(name, target_group):
            return "already_scored"

        # 以本班评分表为准做服务端校验：分值与维度名一律取自数据库，
        # 前端传来的 score 一概不采信，杜绝畸形 / 伪造 / 自动重放的提交。
        status, total, details = _validate_selections(self.get_criteria(scorer_class), selections)
        if status != "ok":
            return status  # "invalid_option" / "incomplete"

        cur = conn.execute(
            "INSERT INTO scores (scorer_name, scorer_group, scorer_class, target_group, total_score, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, scorer_group, scorer_class, target_group, total, comment),
        )
        score_id = cur.lastrowid

        for cid, oid, score, label in details:
            conn.execute(
                "INSERT INTO score_details (score_id, criterion_id, option_id, score, criterion_label) "
                "VALUES (?, ?, ?, ?, ?)",
                (score_id, cid, oid, score, label),
            )

        conn.commit()
        return score_id

    def delete_score(self, score_id: int):
        """重置（删除）单条评分及其明细——供后台纠正异常 / 误评分。"""
        conn = self._get()
        conn.execute("DELETE FROM score_details WHERE score_id = ?", (score_id,))
        conn.execute("DELETE FROM scores WHERE id = ?", (score_id,))
        conn.commit()

    def clear_class_scores(self, class_name: str) -> int:
        """清空某班级的全部评分及明细（保留学生名单与评分标准）。返回删除条数。
        常用于换新评分标准后清场重评。空班级名不处理，避免误删。"""
        if not class_name:
            return 0
        conn = self._get()
        conn.execute(
            "DELETE FROM score_details WHERE score_id IN (SELECT id FROM scores WHERE scorer_class = ?)",
            (class_name,),
        )
        cur = conn.execute("DELETE FROM scores WHERE scorer_class = ?", (class_name,))
        conn.commit()
        return cur.rowcount

    # ---------- 结果汇总 ----------
    def get_results(self, class_name: str = "") -> dict:
        conn = self._get()
        criteria = self.get_criteria(class_name)

        if class_name:
            group_count = conn.execute(
                "SELECT COALESCE(MAX(group_number), 0) FROM students WHERE class_name = ?",
                (class_name,),
            ).fetchone()[0]
        else:
            group_count = conn.execute(
                "SELECT COALESCE(MAX(group_number), 0) FROM students"
            ).fetchone()[0]

        results = {}
        for g in range(1, group_count + 1):
            if class_name:
                rows = conn.execute(
                    "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                    "sd.criterion_id, sd.score AS criterion_score, "
                    "sd.criterion_label AS criterion_label "
                    "FROM scores s "
                    "LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "WHERE s.target_group = ? AND s.scorer_class = ? "
                    "ORDER BY s.id, sd.criterion_id",
                    (g, class_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                    "sd.criterion_id, sd.score AS criterion_score, "
                    "sd.criterion_label AS criterion_label "
                    "FROM scores s "
                    "LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "WHERE s.target_group = ? "
                    "ORDER BY s.id, sd.criterion_id",
                    (g,),
                ).fetchall()

            scorer_map: dict[str, dict] = {}
            for row in rows:
                sn = row["scorer_name"]
                if sn not in scorer_map:
                    scorer_map[sn] = {
                        "scorer_name": sn,
                        "scorer_group": row["scorer_group"],
                        "scorer_class": row["scorer_class"],
                        "total_score": row["total_score"],
                        "comment": row["comment"],
                        "created_at": row["created_at"],
                        "criteria_scores": [],
                    }
                if row["criterion_id"] is not None:
                    scorer_map[sn]["criteria_scores"].append({
                        "criterion_id": row["criterion_id"],
                        "criterion_label": row["criterion_label"],
                        "score": row["criterion_score"],
                    })

            results[g] = {
                "group_number": g,
                "score_count": len(scorer_map),
                "scores": list(scorer_map.values()),
            }

        ranked = []
        for g, data in results.items():
            if data["score_count"] > 0:
                avg_total = sum(s["total_score"] for s in data["scores"]) / data["score_count"]
            else:
                avg_total = 0
            ranked.append({
                "group_number": g,
                "score_count": data["score_count"],
                "avg_total": round(avg_total, 2),
            })
        ranked.sort(key=lambda x: (-x["avg_total"], x["group_number"]))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        return {
            "group_count": group_count,
            "criteria": criteria,
            "ranked": ranked,
            "groups": results,
        }

    def get_group_detail(self, group_number: int, class_name: str = "") -> dict:
        conn = self._get()
        criteria = self.get_criteria(class_name)
        if class_name:
            rows = conn.execute(
                "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                "sd.criterion_id, sd.score AS criterion_score, "
                "sd.criterion_label AS criterion_label "
                "FROM scores s "
                "LEFT JOIN score_details sd ON sd.score_id = s.id "
                "WHERE s.target_group = ? AND s.scorer_class = ? "
                "ORDER BY s.id, sd.criterion_id",
                (group_number, class_name),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                "sd.criterion_id, sd.score AS criterion_score, "
                "sd.criterion_label AS criterion_label "
                "FROM scores s "
                "LEFT JOIN score_details sd ON sd.score_id = s.id "
                "WHERE s.target_group = ? "
                "ORDER BY s.id, sd.criterion_id",
                (group_number,),
            ).fetchall()

        scorer_map: dict[int, dict] = {}
        for row in rows:
            sid = row["id"]
            if sid not in scorer_map:
                scorer_map[sid] = {
                    "id": sid,
                    "scorer_name": row["scorer_name"],
                    "scorer_group": row["scorer_group"],
                    "scorer_class": row["scorer_class"],
                    "total_score": row["total_score"],
                    "comment": row["comment"],
                    "created_at": row["created_at"],
                    "criteria_scores": [],
                }
            if row["criterion_id"] is not None:
                scorer_map[sid]["criteria_scores"].append({
                    "criterion_id": row["criterion_id"],
                    "criterion_label": row["criterion_label"],
                    "score": row["criterion_score"],
                })

        return {
            "group_number": group_number,
            "criteria": criteria,
            "scores": list(scorer_map.values()),
        }

    # ---------- 管理员认证 ----------
    def verify_admin(self, username: str, password: str) -> bool:
        conn = self._get()
        cur = conn.execute(
            "SELECT password_hash FROM admins WHERE username = ?",
            (username,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return row[0] == _hash_password(password)

    def change_password(self, username: str, new_hash: str):
        conn = self._get()
        conn.execute("UPDATE admins SET password_hash = ? WHERE username = ?", (new_hash, username))
        conn.commit()

    # ---------- 全局设置（如：学生评分页当前班级）----------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._get().execute("SELECT sval FROM settings WHERE skey = ?", (key,)).fetchone()
        return row["sval"] if row else default

    def set_setting(self, key: str, value: str):
        conn = self._get()
        conn.execute("INSERT OR REPLACE INTO settings (skey, sval) VALUES (?, ?)", (key, value))
        conn.commit()

    # ---------- CSV 导出 ----------
    def export_csv(self, class_name: str = "") -> str:
        conn = self._get()
        criteria = self.get_criteria(class_name)
        if not criteria:
            return ""

        if class_name:
            rows = conn.execute(
                "SELECT s.scorer_name, s.scorer_group, s.target_group, s.total_score, s.comment, "
                "sd.criterion_id, sd.score AS criterion_score "
                "FROM scores s LEFT JOIN score_details sd ON sd.score_id = s.id "
                "WHERE s.scorer_class = ? "
                "ORDER BY s.target_group, s.scorer_name",
                (class_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT s.scorer_name, s.scorer_group, s.target_group, s.total_score, s.comment, "
                "sd.criterion_id, sd.score AS criterion_score "
                "FROM scores s LEFT JOIN score_details sd ON sd.score_id = s.id "
                "ORDER BY s.target_group, s.scorer_name"
            ).fetchall()

        return _rows_to_csv(criteria, rows)


# ============================================================
# MySQL 实现（接口相同）
# ============================================================
class MySQLDB:
    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        import pymysql
        self.conn = pymysql.connect(
            host=host, port=port, user=user,
            password=password, database=database,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        )
        self.conn.autocommit(True)

    def connect(self):
        return self

    def close(self):
        self.conn.close()

    def _get(self):
        return self.conn.cursor()

    def init_db(self):
        with self._get() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(50) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS students (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(50) NOT NULL,
                    group_number INT NOT NULL,
                    class_name VARCHAR(100) DEFAULT ''
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS criteria (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    label VARCHAR(100) NOT NULL,
                    sort_order INT DEFAULT 0,
                    class_name VARCHAR(100) DEFAULT ''
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS criteria_options (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    criterion_id INT NOT NULL,
                    label VARCHAR(100) NOT NULL,
                    description TEXT DEFAULT '',
                    score INT NOT NULL,
                    sort_order INT DEFAULT 0,
                    FOREIGN KEY (criterion_id) REFERENCES criteria(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS scores (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scorer_name VARCHAR(50) NOT NULL,
                    scorer_group INT NOT NULL,
                    scorer_class VARCHAR(100) DEFAULT '',
                    target_group INT NOT NULL,
                    total_score INT NOT NULL,
                    comment TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_sc (scorer_name, target_group)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS score_details (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    score_id INT NOT NULL,
                    criterion_id INT NOT NULL,
                    option_id INT NOT NULL,
                    score INT NOT NULL,
                    criterion_label VARCHAR(100) DEFAULT '',
                    FOREIGN KEY (score_id) REFERENCES scores(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS criteria_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data TEXT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS classes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

                CREATE TABLE IF NOT EXISTS settings (
                    skey VARCHAR(50) PRIMARY KEY,
                    sval TEXT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("SELECT COUNT(*) FROM admins")
            if cur.fetchone()["COUNT(*)"] == 0:
                cur.execute(
                    "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
                    (config.ADMIN_USERNAME, _hash_password(config.ADMIN_PASSWORD)),
                )

            cur.execute("SELECT COUNT(*) FROM criteria")
            if cur.fetchone()["COUNT(*)"] == 0:
                for cr in DEFAULT_CRITERIA:
                    cur.execute(
                        "INSERT INTO criteria (label, sort_order, class_name) VALUES (%s, %s, '')",
                        (cr["label"], cr["sort_order"]),
                    )
                    cid = cur.lastrowid
                    for opt in cr["options"]:
                        cur.execute(
                            "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (cid, opt["label"], opt["description"], opt["score"], opt["score"]),
                        )

            # 初始化默认评分模板
            cur.execute("SELECT COUNT(*) FROM criteria_templates")
            if cur.fetchone()["COUNT(*)"] == 0:
                import json
                criteria_data = []
                cur.execute("SELECT id, label, sort_order FROM criteria WHERE class_name='' ORDER BY sort_order")
                cr_rows = cur.fetchall()
                for cr_row in cr_rows:
                    cur.execute(
                        "SELECT label, description, score, sort_order FROM criteria_options "
                        "WHERE criterion_id=%s ORDER BY sort_order",
                        (cr_row["id"],),
                    )
                    opts = [
                        {"label": o["label"], "description": o["description"],
                         "score": o["score"], "sort_order": o["sort_order"]}
                        for o in cur.fetchall()
                    ]
                    criteria_data.append({"label": cr_row["label"], "sort_order": cr_row["sort_order"], "options": opts})
                cur.execute(
                    "INSERT INTO criteria_templates (name, data) VALUES (%s, %s)",
                    ("答辩评分标准", json.dumps(criteria_data, ensure_ascii=False)),
                )

            # 初始化测试班级和学生
            cur.execute("SELECT COUNT(*) FROM students WHERE class_name = %s", ("测试班",))
            if cur.fetchone()["COUNT(*)"] == 0:
                cur.execute("INSERT IGNORE INTO classes (name) VALUES (%s)", ("测试班",))
                test_students = [
                    ("张三", 1, "测试班"), ("李四", 1, "测试班"), ("王五", 2, "测试班"),
                    ("赵六", 2, "测试班"), ("孙七", 3, "测试班"), ("周八", 3, "测试班"),
                    ("吴九", 4, "测试班"), ("郑十", 4, "测试班"), ("陈十一", 5, "测试班"),
                    ("林十二", 5, "测试班"), ("黄十三", 6, "测试班"), ("何十四", 6, "测试班"),
                    ("刘十五", 7, "测试班"), ("杨十六", 7, "测试班"), ("朱十七", 8, "测试班"),
                    ("马十八", 8, "测试班"),
                ]
                for name, group, cls in test_students:
                    cur.execute(
                        "INSERT INTO students (name, group_number, class_name) VALUES (%s, %s, %s)",
                        (name, group, cls),
                    )
                # 为测试班复制一份独立的评分表（从全局默认）
                self.clone_criteria("", "测试班")

            # 回填：把已存在于学生表里的班级名登记到 classes 表（兼容旧库）
            cur.execute(
                "INSERT IGNORE INTO classes (name) "
                "SELECT DISTINCT class_name FROM students WHERE class_name != ''"
            )

    # ---------- 学生 ----------
    def get_students(self, class_name: str = "") -> list[dict]:
        with self._get() as cur:
            if class_name:
                cur.execute(
                    "SELECT id, name, group_number, class_name FROM students WHERE class_name = %s ORDER BY group_number, name",
                    (class_name,),
                )
            else:
                cur.execute("SELECT id, name, group_number, class_name FROM students ORDER BY group_number, name")
            return cur.fetchall()

    def get_classes(self) -> list[str]:
        """返回所有班级：classes 表登记的 ∪ 学生表里出现的（去重、按名称排序）。"""
        with self._get() as cur:
            cur.execute("SELECT name FROM classes")
            registered = {r["name"] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT class_name FROM students WHERE class_name != ''")
            from_students = {r["class_name"] for r in cur.fetchall()}
        return sorted(registered | from_students)

    def create_class(self, name: str, template_id: int | None = None):
        """登记一个班级（空班级也能存在），并为其生成一套独立的评分表。"""
        name = (name or "").strip()
        if not name:
            return
        with self._get() as cur:
            cur.execute("INSERT IGNORE INTO classes (name) VALUES (%s)", (name,))
            cur.execute("SELECT COUNT(*) AS cnt FROM criteria WHERE class_name = %s", (name,))
            has = cur.fetchone()["cnt"]
        if has:
            return
        if template_id:
            self.save_criteria(self.load_template(template_id), name)
        else:
            self.clone_criteria("", name)

    def get_class_students_count(self) -> dict:
        with self._get() as cur:
            cur.execute("SELECT class_name, COUNT(*) as cnt FROM students GROUP BY class_name")
            return {r["class_name"]: r["cnt"] for r in cur.fetchall()}

    def import_students(self, data: list[dict], class_name: str = "") -> tuple[int, int]:
        if class_name:
            self.create_class(class_name)
        added = 0
        skipped = 0
        with self._get() as cur:
            for item in data:
                name = str(item.get("name", "")).strip()
                group = int(item.get("group", 0))
                if not name or group <= 0:
                    continue
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM students WHERE name = %s AND class_name = %s",
                    (name, class_name),
                )
                if cur.fetchone()["cnt"]:
                    skipped += 1
                else:
                    cur.execute(
                        "INSERT INTO students (name, group_number, class_name) VALUES (%s, %s, %s)",
                        (name, group, class_name),
                    )
                    added += 1
        return added, skipped

    def add_student(self, name: str, group: int, class_name: str = ""):
        with self._get() as cur:
            cur.execute(
                "INSERT INTO students (name, group_number, class_name) VALUES (%s, %s, %s)",
                (name, group, class_name),
            )

    def delete_student(self, student_id: int):
        with self._get() as cur:
            cur.execute("DELETE FROM students WHERE id = %s", (student_id,))

    def delete_class(self, class_name: str):
        """删除整个班级的所有数据（班级登记、学生、评分、评分明细、该班评分表）。"""
        with self._get() as cur:
            cur.execute(
                "DELETE FROM score_details WHERE score_id IN "
                "(SELECT id FROM (SELECT id FROM scores WHERE scorer_class = %s) AS t)",
                (class_name,),
            )
            cur.execute("DELETE FROM scores WHERE scorer_class = %s", (class_name,))
            cur.execute("DELETE FROM students WHERE class_name = %s", (class_name,))
            # 删该班专属评分表（维度+选项）
            cur.execute(
                "DELETE FROM criteria_options WHERE criterion_id IN "
                "(SELECT id FROM (SELECT id FROM criteria WHERE class_name = %s) AS t)",
                (class_name,),
            )
            cur.execute("DELETE FROM criteria WHERE class_name = %s", (class_name,))
            cur.execute("DELETE FROM classes WHERE name = %s", (class_name,))

    # ---------- 评分标准 ----------
    def get_criteria(self, class_name: str = "") -> list[dict]:
        """返回某班级的评分标准；该班无专属标准时回退到全局默认（class_name=''）。"""
        with self._get() as cur:
            cur.execute("SELECT id, label, sort_order FROM criteria WHERE class_name = %s ORDER BY sort_order, id",
                        (class_name,))
            rows = cur.fetchall()
            if class_name and not rows:
                cur.execute("SELECT id, label, sort_order FROM criteria WHERE class_name = '' ORDER BY sort_order, id")
                rows = cur.fetchall()
        result = []
        for row in rows:
            with self._get() as oc:
                oc.execute(
                    "SELECT id, label, description, score, sort_order "
                    "FROM criteria_options WHERE criterion_id = %s ORDER BY sort_order, id",
                    (row["id"],),
                )
                result.append({
                    "id": row["id"],
                    "label": row["label"],
                    "sort_order": row["sort_order"],
                    "options": oc.fetchall(),
                })
        return result

    def clone_criteria(self, src_class: str, dst_class: str):
        """把 src_class 的维度+选项复制为 dst_class 的一份（dst 已有则先清空）。"""
        if not dst_class:
            return
        with self._get() as cur:
            cur.execute(
                "DELETE FROM criteria_options WHERE criterion_id IN "
                "(SELECT id FROM (SELECT id FROM criteria WHERE class_name = %s) AS t)",
                (dst_class,),
            )
            cur.execute("DELETE FROM criteria WHERE class_name = %s", (dst_class,))
            cur.execute("SELECT id, label, sort_order FROM criteria WHERE class_name = %s ORDER BY sort_order, id",
                        (src_class,))
            src = cur.fetchall()
        for cr in src:
            with self._get() as cur:
                cur.execute("INSERT INTO criteria (label, sort_order, class_name) VALUES (%s, %s, %s)",
                            (cr["label"], cr["sort_order"], dst_class))
                new_id = cur.lastrowid
                cur.execute(
                    "SELECT label, description, score, sort_order FROM criteria_options "
                    "WHERE criterion_id = %s ORDER BY sort_order, id",
                    (cr["id"],),
                )
                opts = cur.fetchall()
                for o in opts:
                    cur.execute(
                        "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (new_id, o["label"], o["description"], o["score"], o["sort_order"]),
                    )

    def save_criteria(self, criteria: list[dict], class_name: str = ""):
        """保存某班级的评分标准，保留已有维度 id（先按 id、再按 label 匹配），增删改限定在该班级。"""
        with self._get() as cur:
            cur.execute("SELECT id, label FROM criteria WHERE class_name = %s", (class_name,))
            existing = {r["id"]: r["label"] for r in cur.fetchall()}
        label_to_id = {}
        for cid, lab in existing.items():
            label_to_id.setdefault(lab, cid)
        kept_ids = set()

        for cr in criteria:
            cid = cr.get("id")
            if not (cid and cid in existing):
                cid = label_to_id.get(cr["label"])
            with self._get() as cur:
                if cid and cid in existing and cid not in kept_ids:
                    cur.execute("UPDATE criteria SET label = %s, sort_order = %s WHERE id = %s",
                                (cr["label"], cr.get("sort_order", 0), cid))
                else:
                    cur.execute("INSERT INTO criteria (label, sort_order, class_name) VALUES (%s, %s, %s)",
                                (cr["label"], cr.get("sort_order", 0), class_name))
                    cid = cur.lastrowid
                kept_ids.add(cid)
                cur.execute("DELETE FROM criteria_options WHERE criterion_id = %s", (cid,))
                for opt in cr.get("options", []):
                    cur.execute(
                        "INSERT INTO criteria_options (criterion_id, label, description, score, sort_order) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (cid, opt["label"], opt.get("description", ""), opt["score"], opt.get("sort_order", opt["score"])),
                    )

        # 删除本次未提交的旧维度（仅限该班级）
        with self._get() as cur:
            for old_id in set(existing) - kept_ids:
                cur.execute("DELETE FROM criteria_options WHERE criterion_id = %s", (old_id,))
                cur.execute("DELETE FROM criteria WHERE id = %s", (old_id,))

    def delete_criterion(self, criterion_id: int):
        with self._get() as cur:
            cur.execute("DELETE FROM criteria_options WHERE criterion_id = %s", (criterion_id,))
            cur.execute("DELETE FROM criteria WHERE id = %s", (criterion_id,))

    # ---------- 模板 ----------
    def save_template(self, name: str, criteria: list[dict]):
        import json
        with self._get() as cur:
            cur.execute(
                "INSERT INTO criteria_templates (name, data) VALUES (%s, %s)",
                (name, json.dumps(criteria, ensure_ascii=False)),
            )

    def get_templates(self) -> list[dict]:
        with self._get() as cur:
            cur.execute(
                "SELECT id, name, created_at FROM criteria_templates ORDER BY created_at DESC"
            )
            return cur.fetchall()

    def load_template(self, template_id: int) -> list[dict]:
        import json
        with self._get() as cur:
            cur.execute("SELECT data FROM criteria_templates WHERE id = %s", (template_id,))
            row = cur.fetchone()
            if row:
                return json.loads(row["data"])
        return []

    def delete_template(self, template_id: int):
        with self._get() as cur:
            cur.execute("DELETE FROM criteria_templates WHERE id = %s", (template_id,))

    # ---------- 评分 ----------
    def check_scored(self, name: str, target_group: int) -> bool:
        with self._get() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM scores WHERE scorer_name = %s AND target_group = %s",
                        (name, target_group))
            return cur.fetchone()["cnt"] > 0

    def get_my_scores(self, name: str) -> list[dict]:
        with self._get() as cur:
            cur.execute(
                "SELECT target_group, total_score, comment, created_at, scorer_class "
                "FROM scores WHERE scorer_name = %s ORDER BY target_group", (name,))
            return cur.fetchall()

    def submit_score(self, name: str, scorer_group: int, target_group: int,
                     selections: list[dict], comment: str, scorer_class: str = "") -> int | str:
        if self.check_scored(name, target_group):
            return "already_scored"

        # 服务端按本班评分表校验，分值全部取自数据库（详见 _validate_selections）。
        status, total, details = _validate_selections(self.get_criteria(scorer_class), selections)
        if status != "ok":
            return status  # "invalid_option" / "incomplete"

        with self._get() as cur:
            cur.execute(
                "INSERT INTO scores (scorer_name, scorer_group, scorer_class, target_group, total_score, comment) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (name, scorer_group, scorer_class, target_group, total, comment),
            )
            score_id = cur.lastrowid
            for cid, oid, score, label in details:
                cur.execute(
                    "INSERT INTO score_details (score_id, criterion_id, option_id, score, criterion_label) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (score_id, cid, oid, score, label),
                )
        return score_id

    def delete_score(self, score_id: int):
        """重置（删除）单条评分及其明细——供后台纠正异常 / 误评分。"""
        with self._get() as cur:
            cur.execute("DELETE FROM score_details WHERE score_id = %s", (score_id,))
            cur.execute("DELETE FROM scores WHERE id = %s", (score_id,))

    def clear_class_scores(self, class_name: str) -> int:
        """清空某班级的全部评分及明细（保留学生名单与评分标准）。返回删除条数。
        常用于换新评分标准后清场重评。空班级名不处理，避免误删。"""
        if not class_name:
            return 0
        with self._get() as cur:
            cur.execute(
                "DELETE FROM score_details WHERE score_id IN (SELECT id FROM scores WHERE scorer_class = %s)",
                (class_name,),
            )
            cur.execute("DELETE FROM scores WHERE scorer_class = %s", (class_name,))
            return cur.rowcount

    # ---------- 结果汇总 ----------
    def get_results(self, class_name: str = "") -> dict:
        with self._get() as cur:
            if class_name:
                cur.execute(
                    "SELECT COALESCE(MAX(group_number), 0) as mx FROM students WHERE class_name = %s",
                    (class_name,),
                )
            else:
                cur.execute("SELECT COALESCE(MAX(group_number), 0) as mx FROM students")
            group_count = (cur.fetchone()["mx"] or 0)

        criteria = self.get_criteria(class_name)
        results = {}
        for g in range(1, group_count + 1):
            with self._get() as cur:
                if class_name:
                    cur.execute(
                        "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                        "sd.criterion_id, sd.score AS criterion_score, "
                        "sd.criterion_label AS criterion_label "
                        "FROM scores s "
                        "LEFT JOIN score_details sd ON sd.score_id = s.id "
                        "WHERE s.target_group = %s AND s.scorer_class = %s "
                        "ORDER BY s.id, sd.criterion_id", (g, class_name))
                else:
                    cur.execute(
                        "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                        "sd.criterion_id, sd.score AS criterion_score, "
                        "sd.criterion_label AS criterion_label "
                        "FROM scores s "
                        "LEFT JOIN score_details sd ON sd.score_id = s.id "
                        "WHERE s.target_group = %s "
                        "ORDER BY s.id, sd.criterion_id", (g,))
                rows = cur.fetchall()

            scorer_map: dict = {}
            for row in rows:
                sn = row["scorer_name"]
                if sn not in scorer_map:
                    scorer_map[sn] = {
                        "scorer_name": sn,
                        "scorer_group": row["scorer_group"],
                        "scorer_class": row["scorer_class"],
                        "total_score": row["total_score"],
                        "comment": row["comment"],
                        "created_at": row["created_at"],
                        "criteria_scores": [],
                    }
                if row["criterion_id"] is not None:
                    scorer_map[sn]["criteria_scores"].append({
                        "criterion_id": row["criterion_id"],
                        "criterion_label": row["criterion_label"],
                        "score": row["criterion_score"],
                    })
            results[g] = {
                "group_number": g,
                "score_count": len(scorer_map),
                "scores": list(scorer_map.values()),
            }

        ranked = []
        for g, data in results.items():
            avg_total = (sum(s["total_score"] for s in data["scores"]) / data["score_count"]) if data["score_count"] > 0 else 0
            ranked.append({"group_number": g, "score_count": data["score_count"],
                           "avg_total": round(avg_total, 2)})
        ranked.sort(key=lambda x: (-x["avg_total"], x["group_number"]))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        return {"group_count": group_count, "criteria": criteria, "ranked": ranked, "groups": results}

    def get_group_detail(self, group_number: int, class_name: str = "") -> dict:
        with self._get() as cur:
            if class_name:
                cur.execute(
                    "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                    "sd.criterion_id, sd.score AS criterion_score, "
                    "sd.criterion_label AS criterion_label "
                    "FROM scores s "
                    "LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "WHERE s.target_group = %s AND s.scorer_class = %s "
                    "ORDER BY s.id, sd.criterion_id", (group_number, class_name))
            else:
                cur.execute(
                    "SELECT s.id, s.scorer_name, s.scorer_group, s.scorer_class, s.total_score, s.comment, s.created_at, "
                    "sd.criterion_id, sd.score AS criterion_score, "
                    "sd.criterion_label AS criterion_label "
                    "FROM scores s "
                    "LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "WHERE s.target_group = %s "
                    "ORDER BY s.id, sd.criterion_id", (group_number,))
            rows = cur.fetchall()
        criteria = self.get_criteria(class_name)
        scorer_map: dict = {}
        for row in rows:
            sid = row["id"]
            if sid not in scorer_map:
                scorer_map[sid] = {
                    "id": sid,
                    "scorer_name": row["scorer_name"],
                    "scorer_group": row["scorer_group"],
                    "scorer_class": row["scorer_class"],
                    "total_score": row["total_score"],
                    "comment": row["comment"],
                    "created_at": row["created_at"],
                    "criteria_scores": [],
                }
            if row["criterion_id"] is not None:
                scorer_map[sid]["criteria_scores"].append({
                    "criterion_id": row["criterion_id"],
                    "criterion_label": row["criterion_label"],
                    "score": row["criterion_score"],
                })
        return {"group_number": group_number, "criteria": criteria, "scores": list(scorer_map.values())}

    # ---------- 管理员认证 ----------
    def verify_admin(self, username: str, password: str) -> bool:
        with self._get() as cur:
            cur.execute("SELECT password_hash FROM admins WHERE username = %s", (username,))
            row = cur.fetchone()
            if row is None:
                return False
            return row["password_hash"] == _hash_password(password)

    def change_password(self, username: str, new_hash: str):
        with self._get() as cur:
            cur.execute("UPDATE admins SET password_hash = %s WHERE username = %s", (new_hash, username))

    # ---------- 全局设置（如：学生评分页当前班级）----------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._get() as cur:
            cur.execute("SELECT sval FROM settings WHERE skey = %s", (key,))
            row = cur.fetchone()
            return row["sval"] if row else default

    def set_setting(self, key: str, value: str):
        with self._get() as cur:
            cur.execute(
                "INSERT INTO settings (skey, sval) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE sval = VALUES(sval)",
                (key, value),
            )

    # ---------- CSV 导出 ----------
    def export_csv(self, class_name: str = "") -> str:
        criteria = self.get_criteria(class_name)
        if not criteria:
            return ""
        with self._get() as cur:
            if class_name:
                cur.execute(
                    "SELECT s.scorer_name, s.scorer_group, s.target_group, s.total_score, s.comment, "
                    "sd.criterion_id, sd.score AS criterion_score "
                    "FROM scores s LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "WHERE s.scorer_class = %s "
                    "ORDER BY s.target_group, s.scorer_name", (class_name,))
            else:
                cur.execute(
                    "SELECT s.scorer_name, s.scorer_group, s.target_group, s.total_score, s.comment, "
                    "sd.criterion_id, sd.score AS criterion_score "
                    "FROM scores s LEFT JOIN score_details sd ON sd.score_id = s.id "
                    "ORDER BY s.target_group, s.scorer_name")
            rows = cur.fetchall()
        return _rows_to_csv(criteria, rows)


# ============================================================
# 工厂函数
# ============================================================
def get_db() -> SQLiteDB | MySQLDB:
    if config.DB_TYPE == "sqlite":
        db = SQLiteDB(config.SQLITE_PATH)
    else:
        db = MySQLDB(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
        )
    return db.connect()


def get_db_conn():
    """每个请求获取独立的数据库连接。

    建表在应用启动时执行一次（见 main.py），这里不再每请求 init_db，
    避免每次 API 调用都跑 CREATE TABLE + 多次 COUNT 检查。
    """
    return get_db()
