"""实战答辩评分系统 — FastAPI 入口"""

import socket
from contextlib import closing
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import auth
from config import config
from db import get_db, get_db_conn

app = FastAPI(title="实战答辩评分系统")

# 挂载静态文件
import os
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 初始化默认数据库连接（启动时建表）
_init_db = get_db()
_init_db.init_db()
_init_db.close()


# ============================================================
# 页面路由
# ============================================================

@app.get("/")
async def student_page(request: Request):
    """学生评分页"""
    return RedirectResponse(url="/static/score.html")


@app.get("/admin/login")
async def admin_login_page(request: Request):
    """管理员登录页"""
    user = await auth.get_logged_in_user(request)
    if user:
        return RedirectResponse(url="/admin")
    return RedirectResponse(url="/static/admin_login.html")


@app.get("/admin")
async def admin_page(request: Request):
    """管理员主页（需登录）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return RedirectResponse(url="/admin/login")
    return RedirectResponse(url="/static/admin.html")


# ============================================================
# 认证 API
# ============================================================

@app.post("/api/admin/login")
async def api_login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not await auth.login(username, password):
        return JSONResponse({"ok": False, "error": "用户名或密码错误"})
    session_id = await auth.register_session(username)
    resp = JSONResponse({"ok": True, "username": username})
    auth._set_session_cookie(resp, session_id)
    return resp


@app.post("/api/admin/logout")
async def api_logout(request: Request):
    return await auth.logout(request)


@app.get("/api/admin/check")
async def api_admin_check(request: Request):
    user = await auth.get_logged_in_user(request)
    if user:
        return {"logged_in": True, "username": user}
    return {"logged_in": False}


# ============================================================
# 学生 API
# ============================================================

@app.get("/api/students")
async def api_get_students(class_name: str = Query("")):
    """获取学生名单（可按班级筛选）"""
    d = get_db_conn()
    try:
        rows = d.get_students(class_name)
        return {"students": rows}
    finally:
        d.close()


@app.get("/api/classes")
async def api_get_classes():
    """获取所有班级列表"""
    d = get_db_conn()
    try:
        classes = d.get_classes()
        counts = d.get_class_students_count()
        return {"classes": [{"name": c, "count": counts.get(c, 0)} for c in classes]}
    finally:
        d.close()


@app.get("/api/active-class")
async def api_get_active_class():
    """获取学生评分页当前应展示的班级（由管理员在后台设定）"""
    d = get_db_conn()
    try:
        return {"class_name": d.get_setting("active_class", "")}
    finally:
        d.close()


@app.post("/api/active-class")
async def api_set_active_class(request: Request):
    """设定学生评分页当前班级（需登录）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    name = (data.get("class_name", "") or "").strip()
    d = get_db_conn()
    try:
        d.set_setting("active_class", name)
        return {"ok": True, "class_name": name}
    finally:
        d.close()


@app.post("/api/classes")
async def api_create_class(request: Request):
    """新建班级（允许空班级，需登录）；建班即为该班生成一套独立评分表"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    name = (data.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "班级名称不能为空"})
    template_id = data.get("template_id")
    d = get_db_conn()
    try:
        d.create_class(name, template_id)
        return {"ok": True, "name": name}
    finally:
        d.close()


@app.post("/api/students/import")
async def api_import_students(request: Request):
    """批量导入学生（支持 class_name）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    students = data.get("students", [])
    class_name = data.get("class_name", "")
    d = get_db_conn()
    try:
        added, skipped = d.import_students(students, class_name)
        return {"ok": True, "added": added, "skipped": skipped}
    finally:
        d.close()


@app.delete("/api/students/{student_id}")
async def api_delete_student(student_id: int, request: Request):
    """删除学生"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        d.delete_student(student_id)
        return {"ok": True}
    finally:
        d.close()


@app.delete("/api/classes/{class_name:path}")
async def api_delete_class(class_name: str, request: Request):
    """删除整个班级（学生、评分、汇总）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        d.delete_class(class_name)
        return {"ok": True}
    finally:
        d.close()


# ============================================================
# 评分标准 API
# ============================================================

@app.get("/api/criteria")
async def api_get_criteria(class_name: str = Query("")):
    """获取评分标准（可按班级；班级无专属标准时回退全局默认）"""
    d = get_db_conn()
    try:
        return {"criteria": d.get_criteria(class_name)}
    finally:
        d.close()


@app.post("/api/criteria")
async def api_save_criteria(request: Request):
    """保存评分标准（可指定 class_name，默认全局）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    d = get_db_conn()
    try:
        d.save_criteria(data.get("criteria", []), data.get("class_name", ""))
        return {"ok": True}
    finally:
        d.close()


@app.delete("/api/criteria/{criterion_id}")
async def api_delete_criterion(criterion_id: int, request: Request):
    """删除评分维度"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        d.delete_criterion(criterion_id)
        return {"ok": True}
    finally:
        d.close()


# ============================================================
# 评分标准模板 API
# ============================================================

@app.get("/api/templates")
async def api_get_templates():
    """获取所有评分标准模板"""
    d = get_db_conn()
    try:
        return {"templates": d.get_templates()}
    finally:
        d.close()


@app.post("/api/templates")
async def api_save_template(request: Request):
    """保存评分标准为模板"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    d = get_db_conn()
    try:
        new_id = d.save_template(data.get("name", ""), data.get("criteria", []))
        return {"ok": True, "id": new_id}
    finally:
        d.close()


@app.get("/api/templates/{template_id}")
async def api_load_template(template_id: int):
    """加载模板数据"""
    d = get_db_conn()
    try:
        return {"criteria": d.load_template(template_id)}
    finally:
        d.close()


@app.put("/api/templates/{template_id}")
async def api_update_template(template_id: int, request: Request):
    """就地修改并保存现有模板（名称 + 维度）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    d = get_db_conn()
    try:
        d.update_template(template_id, data.get("name", ""), data.get("criteria", []))
        return {"ok": True}
    finally:
        d.close()


@app.delete("/api/templates/{template_id}")
async def api_delete_template(template_id: int, request: Request):
    """删除模板"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        d.delete_template(template_id)
        return {"ok": True}
    finally:
        d.close()


# ============================================================
# 模板下载 / 上传 API
# ============================================================

@app.get("/api/templates/{template_id}/download")
async def api_download_template(template_id: int):
    """下载模板为标准 JSON 文件（精简格式：无 id / 无日期，空 description 不传）"""
    import json
    d = get_db_conn()
    try:
        raw = d.load_template(template_id)
        if not raw:
            return JSONResponse({"ok": False, "error": "模板不存在"}, status_code=404)
        # 先查名称
        conn = d._get()
        cur = conn.execute("SELECT name FROM criteria_templates WHERE id = ?", (template_id,))
        row = cur.fetchone()
        tpl_name = row["name"] if row else f"模板_{template_id}"
    finally:
        d.close()

    # 精简格式：去掉 id、sort_order，去掉空 description
    criteria = []
    for cr in raw:
        opts = []
        for opt in cr.get("options", []):
            o = {"label": opt.get("label", ""), "score": opt.get("score", 0)}
            desc = opt.get("description", "")
            if desc:
                o["description"] = desc
            opts.append(o)
        criteria.append({"label": cr.get("label", ""), "options": opts})

    payload = {"name": tpl_name, "criteria": criteria}
    body = json.dumps(payload, ensure_ascii=False).encode()
    safe_filename = "".join(c if ord(c) < 128 else "_" for c in tpl_name)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={safe_filename}.json; filename*=UTF-8''{quote(tpl_name)}.json"},
    )


@app.post("/api/templates/upload")
async def api_upload_template(request: Request):
    """从 JSON 上传模板（需登录）。id / sort_order 自动生成。"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    name = (data.get("name", "") or "").strip()
    raw = data.get("criteria", [])
    if not name:
        return JSONResponse({"ok": False, "error": "缺少模板名称"})
    if not isinstance(raw, list) or not len(raw):
        return JSONResponse({"ok": False, "error": "缺少评分维度"})

    # 规整化：支持 dimensionLabel / label 两种键名，顺序由列表位置决定；score 默认从小到大
    criteria = []
    for ci, cr in enumerate(raw):
        label = ((cr.get("dimensionLabel") or cr.get("label") or "")).strip()
        if not label:
            return JSONResponse({"ok": False, "error": "所有维度必须填写名称"})
        opts = []
        for oi, opt in enumerate(cr.get("options", [])):
            olabel = ((opt.get("optionLabel") or opt.get("label") or "")).strip()
            if not olabel:
                return JSONResponse({"ok": False, "error": "等级选项不能为空"})
            sc = opt.get("score")
            if sc is None:
                sc = oi + 1
            opts.append({
                "label": olabel,
                "description": opt.get("description", ""),
                "score": int(sc),
            })
        criteria.append({
            "label": label,
            "options": opts,
        })

    d = get_db_conn()
    try:
        new_id = d.save_template(name, criteria)
        return {"ok": True, "id": new_id}
    finally:
        d.close()


# ============================================================
# 班级 ↔ 当前评分模板 绑定 API
# ============================================================

@app.get("/api/class-template")
async def api_get_class_template(class_name: str = Query("")):
    """获取某班级当前绑定的评分模板 id（未绑定或已删则为 None）"""
    d = get_db_conn()
    try:
        raw = d.get_setting("class_tpl::" + class_name, "")
        tpl_id = int(raw) if raw.isdigit() else None
        # 绑定的模板若已被删除，则视为未绑定
        if tpl_id is not None and not d.load_template(tpl_id):
            tpl_id = None
        return {"template_id": tpl_id}
    finally:
        d.close()


@app.post("/api/class-template")
async def api_set_class_template(request: Request):
    """记录某班级当前绑定的评分模板 id（需登录）。template_id 为空=解绑。"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    class_name = (data.get("class_name", "") or "").strip()
    tpl_id = data.get("template_id")
    d = get_db_conn()
    try:
        d.set_setting("class_tpl::" + class_name, str(tpl_id) if tpl_id else "")
        return {"ok": True}
    finally:
        d.close()


# ============================================================
# 评分 API
# ============================================================

@app.post("/api/scores")
async def api_submit_score(request: Request):
    """提交评分"""
    data = await request.json()
    name = data.get("scorer_name", "").strip()
    scorer_group = int(data.get("scorer_group", 0))
    target_group = int(data.get("target_group", 0))
    selections = data.get("selections", [])
    comment = data.get("comment", "")
    scorer_class = data.get("scorer_class", "")

    if not name or scorer_group <= 0 or target_group <= 0:
        return JSONResponse({"ok": False, "error": "参数不完整"})

    d = get_db_conn()
    try:
        result = d.submit_score(name, scorer_group, target_group, selections, comment, scorer_class)
        if result == "already_scored":
            return JSONResponse({"ok": False, "error": "您已评过该组，不可重复评分"})
        elif result == "invalid_option":
            return JSONResponse({"ok": False, "error": "无效的评分选项"})
        elif result == "incomplete":
            return JSONResponse({"ok": False, "error": "请为所有维度选择等级后再提交"})
        else:
            return {"ok": True, "score_id": result}
    finally:
        d.close()


@app.get("/api/scores/check")
async def api_check_score(name: str, group: int):
    """检查是否已评"""
    d = get_db_conn()
    try:
        return {"scored": d.check_scored(name, group)}
    finally:
        d.close()


@app.get("/api/scores/my")
async def api_my_scores(name: str):
    """获取某学生的所有评分"""
    d = get_db_conn()
    try:
        return {"scores": d.get_my_scores(name)}
    finally:
        d.close()


@app.delete("/api/scores/{score_id}")
async def api_delete_score(score_id: int, request: Request):
    """重置（删除）某条评分——后台纠正异常/误评分（需登录）。

    删除后该学生对该组即恢复未评状态，可重新评分。
    """
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        d.delete_score(score_id)
        return {"ok": True}
    finally:
        d.close()


@app.post("/api/scores/clear")
async def api_clear_class_scores(request: Request):
    """清空某班级的全部评分及评语（保留学生名单与评分标准）——换新评分标准后清场重评。需登录。"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    class_name = (data.get("class_name", "") or "").strip()
    if not class_name:
        return JSONResponse({"ok": False, "error": "请指定要清空的班级"})
    d = get_db_conn()
    try:
        deleted = d.clear_class_scores(class_name)
        return {"ok": True, "deleted": deleted}
    finally:
        d.close()


# ============================================================
# 结果汇总 API
# ============================================================

@app.get("/api/results")
async def api_get_results(request: Request, class_name: str = Query("")):
    """获取汇总排名（可按班级筛选）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        return d.get_results(class_name)
    finally:
        d.close()


@app.get("/api/results/group/{group_number}")
async def api_get_group_detail(group_number: int, request: Request, class_name: str = Query("")):
    """获取某组评分明细（可按班级筛选）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        return d.get_group_detail(group_number, class_name)
    finally:
        d.close()


@app.get("/api/results/export")
async def api_export_results(request: Request, class_name: str = Query("")):
    """导出 Excel（.xlsx）：Sheet1 评分明细，Sheet2 各组排名/综合评分/逐条评语（可按班级筛选，需登录）"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    d = get_db_conn()
    try:
        data = d.get_results(class_name)
    finally:
        d.close()

    from export_xlsx import build_results_xlsx
    content = build_results_xlsx(data)
    filename = f"评分结果_{class_name}.xlsx" if class_name else "评分结果.xlsx"
    # 中文文件名用 RFC 5987 编码，避免部分浏览器乱码
    from urllib.parse import quote
    disp = f"attachment; filename=results.xlsx; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp},
    )


# ============================================================
# 管理员设置 API
# ============================================================

@app.post("/api/admin/change-password")
async def api_change_password(request: Request):
    """修改管理员密码"""
    user = await auth.get_logged_in_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    data = await request.json()
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not await auth.login(user, old_pw):
        return JSONResponse({"ok": False, "error": "旧密码错误"})
    import hashlib
    d = get_db_conn()
    try:
        d.change_password(user, hashlib.sha256(new_pw.encode()).hexdigest())
        return {"ok": True}
    finally:
        d.close()


# ============================================================
# 启动
# ============================================================

def get_local_ip():
    """获取本机局域网 IP"""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def main():
    import uvicorn
    db_str = "SQLite" if config.DB_TYPE == "sqlite" else "MySQL"

    # 打印启动信息
    ip = get_local_ip()
    print(f"\n{'='*50}")
    print("  实战答辩评分系统已启动")
    print(f"{'='*50}")
    print(f"  学生评分:  http://{ip}:{config.PORT}")
    print(f"  管理员:    http://{ip}:{config.PORT}/admin/login")
    print(f"  数据库:    {db_str}")
    print(f"{'='*50}\n")

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
