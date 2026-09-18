import os
import sys
import shutil
import tempfile
import unittest
import zipfile
import io
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# 使用独立临时数据库和临时缓存目录测试
temp_dir = tempfile.mkdtemp(prefix="chatlogger_test_")
Config.DB_PATH = os.path.join(temp_dir, "test_chatlogger.db")
Config.LOCAL_CACHE_DIR = os.path.join(temp_dir, "test_cache")

import models
import local_cache
from app import app


class LocalCacheTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models.init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(temp_dir, ignore_errors=True)

    def setUp(self):
        self.user = models.get_or_create_user("ou_test123", "测试用户")
        self.chat_id = "oc_test_chat_001"
        self.chat_name = "项目研发交流群"

    def test_01_models_local_cache(self):
        """测试数据库操作：local_cache 字段增删查改"""
        # 添加群聊，开启本地缓存
        models.add_chat(self.user["id"], self.chat_id, self.chat_name, local_cache=1)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertIsNotNone(chat)
        self.assertEqual(chat["local_cache"], 1)

        # 切换本地缓存为关闭
        models.update_chat_local_cache(self.user["id"], self.chat_id, False)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertEqual(chat["local_cache"], 0)

        # 再次切换为开启
        models.update_chat_local_cache(self.user["id"], self.chat_id, True)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertEqual(chat["local_cache"], 1)

        # 测试排序：新添加的群排在最前面
        chat_id_2 = "oc_test_chat_002"
        models.add_chat(self.user["id"], chat_id_2, "第二个测试群")
        chats = models.get_chats(self.user["id"])
        self.assertEqual(chats[0]["chat_id"], chat_id_2)

        # 当第一个群更新时间最新时，自动浮动到最前面 (updated_at DESC)
        conn = models.get_db()
        conn.execute("UPDATE chats SET updated_at = '2099-01-01 00:00:00' WHERE chat_id = ?", (self.chat_id,))
        conn.commit()
        conn.close()
        chats = models.get_chats(self.user["id"])
        self.assertEqual(chats[0]["chat_id"], self.chat_id)

    def test_02_sanitize_filename(self):
        """测试非法文件名清理（兼容 Windows/Linux）"""
        self.assertEqual(local_cache.sanitize_filename("test/file:name*?.txt"), "test_file_name_.txt")
        self.assertEqual(local_cache.sanitize_filename(""), "file")
        self.assertEqual(local_cache.sanitize_filename("..."), "file")

    def test_03_save_asset_and_deduplication(self):
        """测试附件落盘与重复文件命名"""
        content_a = b"fake image bytes A"
        path_a = local_cache.save_asset(self.chat_id, self.chat_name, content_a, "photo.jpg", "key_001")
        self.assertTrue(path_a.startswith("assets/"))
        self.assertTrue(path_a.endswith("photo.jpg"))

        # 同内容同名保存，应复用路径
        path_a2 = local_cache.save_asset(self.chat_id, self.chat_name, content_a, "photo.jpg", "key_001")
        self.assertEqual(path_a, path_a2)

        # 不同内容同名保存，应增加前缀区分
        content_b = b"fake image bytes B (different)"
        path_b = local_cache.save_asset(self.chat_id, self.chat_name, content_b, "photo.jpg", "key_002")
        self.assertNotEqual(path_a, path_b)
        self.assertTrue("key_002" in path_b or "photo.jpg" in path_b)

    def test_04_format_message_to_markdown(self):
        """测试各类消息格式化为标准 Markdown"""
        # 1. 纯文本消息
        msg_text = {
            "message_id": "om_001",
            "msg_type": "text",
            "body": {"content": json.dumps({"text": "大家早上好！"})},
            "create_time": "1773000000000"
        }
        md_text = local_cache.format_message_to_markdown(msg_text, "张三", "2026-09-09 10:00:00")
        self.assertIn("**张三** &nbsp; `2026-09-09 10:00:00`", md_text)
        self.assertIn("大家早上好！", md_text)
        self.assertTrue(md_text.endswith("---\n"))

        # 2. 图片消息
        msg_img = {
            "message_id": "om_002",
            "msg_type": "image",
            "body": {"content": json.dumps({"image_key": "img_001"})},
            "create_time": "1773000010000"
        }
        md_img = local_cache.format_message_to_markdown(
            msg_img, "李四", "2026-09-09 10:01:00",
            asset_map={"img_001": "assets/img_001.jpg"}
        )
        self.assertIn("![图片](assets/img_001.jpg)", md_img)

        # 3. 附件消息
        msg_file = {
            "message_id": "om_003",
            "msg_type": "file",
            "body": {"content": json.dumps({"file_key": "file_001", "file_name": "系统架构图.pdf"})},
            "create_time": "1773000020000"
        }
        md_file = local_cache.format_message_to_markdown(
            msg_file, "王五", "2026-09-09 10:02:00",
            asset_map={"file_001": "assets/系统架构图.pdf"}
        )
        self.assertIn("[📎 系统架构图.pdf](assets/系统架构图.pdf)", md_file)

        # 4. 富文本消息（含样式、链接、内嵌图片与代码块）
        post_content = {
            "zh_cn": {
                "title": "发布通知",
                "content": [
                    [
                        {"tag": "text", "text": "注意：", "style": ["bold"]},
                        {"tag": "text", "text": "版本已发布至 "},
                        {"tag": "a", "text": "官网地址", "href": "https://example.com"}
                    ],
                    [
                        {"tag": "img", "image_key": "img_post_01"}
                    ],
                    [
                        {"tag": "code_block", "text": "npm run start", "language": "bash"}
                    ]
                ]
            }
        }
        msg_post = {
            "message_id": "om_004",
            "msg_type": "post",
            "body": {"content": json.dumps(post_content)},
            "create_time": "1773000030000"
        }
        md_post = local_cache.format_message_to_markdown(
            msg_post, "赵六", "2026-09-09 10:03:00",
            asset_map={"img_post_01": "assets/img_post_01.jpg"}
        )
        self.assertIn("### 发布通知", md_post)
        self.assertIn("**注意：**", md_post)
        self.assertIn("[官网地址](https://example.com)", md_post)
        self.assertIn("![图片](assets/img_post_01.jpg)", md_post)
        self.assertIn("```bash\nnpm run start\n```", md_post)

    def test_05_append_and_build_zip(self):
        """测试追加消息入库、信息获取及打包 ZIP"""
        block1 = "**测试员A** &nbsp; `2026-09-09 11:00:00`\n\n消息1\n\n---\n"
        block2 = "**测试员B** &nbsp; `2026-09-09 11:01:00`\n\n消息2\n\n---\n"
        local_cache.append_messages_to_cache(self.chat_id, self.chat_name, [block1, block2])

        self.assertTrue(local_cache.has_cache(self.chat_id, self.chat_name))
        info = local_cache.get_cache_info(self.chat_id, self.chat_name)
        self.assertTrue(info["exists"])
        self.assertGreater(info["md_size"], 0)

        raw = local_cache.get_raw_markdown(self.chat_id, self.chat_name)
        self.assertIn("消息1", raw)
        self.assertIn("消息2", raw)

        # 测试打包 ZIP
        mem_file, zip_name = local_cache.build_cache_zip(self.chat_id, self.chat_name)
        self.assertTrue(zip_name.endswith(".zip"))
        with zipfile.ZipFile(mem_file, "r") as zf:
            namelist = zf.namelist()
            self.assertTrue(any(n.endswith(".md") for n in namelist))

    def test_06_flask_routes(self):
        """测试 Flask 相关路由：在线预览、原始Markdown下载、ZIP下载、附件安全访问"""
        client = app.test_client()

        # 模拟登录 session
        with client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        # 1. 测试切换开关 API
        resp = client.post(f"/api/chats/{self.chat_id}/toggle_cache", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["local_cache"])

        resp = client.post(f"/api/chats/{self.chat_id}/toggle_cache", json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["local_cache"])

        # 2. 测试预览页面
        resp = client.get(f"/cache/{self.chat_id}/view")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(self.chat_name, html)
        self.assertIn("marked.min.js", html)
        self.assertIn("lightboxModal", html)

        # 3. 测试原始 Markdown 路由
        resp = client.get(f"/cache/{self.chat_id}/raw")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/markdown", resp.headers.get("Content-Type", ""))
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        # 4. 测试 ZIP 下载路由
        resp = client.get(f"/cache/{self.chat_id}/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/zip")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        # 5. 测试静态附件服务
        # 创建一个测试附件
        local_cache.save_asset(self.chat_id, self.chat_name, b"test-pdf-bytes", "report.pdf", "f001")
        resp = client.get(f"/cache/{self.chat_id}/assets/report.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"test-pdf-bytes")
        resp.close()

        # 6. 测试防目录遍历安全性
        resp = client.get(f"/cache/{self.chat_id}/assets/../../config.py")
        self.assertIn(resp.status_code, (403, 404))
        resp.close()

    def test_07_sync_integration_mock(self):
        """测试从 FeishuClient mock 到 Markdown 写入和预览的完整同步流程"""
        from unittest.mock import MagicMock, patch
        import app as app_module

        # 设置用户 token
        models.update_user_tokens(self.user["id"], "mock_access_token", "mock_refresh_token", 7200, 7200)

        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": self.chat_name}
        mock_client.get_chat_members_safe.return_value = {"ou_user_a": "张三(架构师)", "ou_user_b": "李四(前端)"}
        mock_client.list_all_messages.return_value = [
            {
                "message_id": "om_sync_01",
                "message_position": "1",
                "msg_type": "text",
                "create_time": "1773001000000",
                "sender": {"id": "ou_user_a"},
                "body": {"content": json.dumps({"text": "大家看下新版方案"})},
            },
            {
                "message_id": "om_sync_02",
                "message_position": "2",
                "msg_type": "image",
                "create_time": "1773001010000",
                "sender": {"id": "ou_user_b"},
                "body": {"content": json.dumps({"image_key": "img_sync_01"})},
            },
            {
                "message_id": "om_sync_03",
                "message_position": "3",
                "msg_type": "file",
                "create_time": "1773001020000",
                "sender": {"id": "ou_user_a"},
                "body": {"content": json.dumps({"file_key": "file_sync_01", "file_name": "API设计文档.docx"})},
            }
        ]
        mock_client.create_bitable.return_value = ("base_token_123", "table_id_123", "https://feishu.cn/base/xxx")
        mock_client.batch_create_records.return_value = ["rec_01", "rec_02", "rec_03"]
        mock_client.download_resource.side_effect = [
            (b"fake-image-bytes", "img_sync_01.jpg"),
            (b"fake-doc-bytes", "API设计文档.docx")
        ]
        mock_client.upload_file.return_value = "token_file_abc"
        mock_client.upload_attachment_to_record.return_value = True

        with patch("app.FeishuClient", return_value=mock_client):
            app_module._run_sync(self.user["id"], self.chat_id)

        # 验证同步进度已完成
        prog = app_module._get_progress(self.chat_id)
        self.assertEqual(prog["stage"], "done")
        self.assertTrue(prog["result"]["local_cache"])
        self.assertTrue(prog["result"]["has_cache"])

        # 验证本地文件生成
        raw = local_cache.get_raw_markdown(self.chat_id, self.chat_name)
        self.assertIn("张三(架构师)", raw)
        self.assertIn("大家看下新版方案", raw)
        self.assertIn("![图片](assets/img_sync_01.jpg)", raw)
        self.assertIn("API设计文档.docx", raw)

        # 验证 assets 文件落盘
        chat_dir = local_cache.get_chat_cache_dir(self.chat_id, self.chat_name)
        self.assertTrue(os.path.exists(os.path.join(chat_dir, "assets", "img_sync_01.jpg")))
        self.assertTrue(os.path.exists(os.path.join(chat_dir, "assets", "API设计文档.docx")))

    def test_cache_view_and_static_marked(self):
        """验证本地托管 marked.min.js 以及在线预览页面模板输出"""
        client = app.test_client()

        # 1. 验证静态资源 /static/marked.min.js 能正确返回
        resp_static = client.get("/static/marked.min.js")
        self.assertEqual(resp_static.status_code, 200)
        self.assertGreater(len(resp_static.data), 30000)
        self.assertIn(b"marked", resp_static.data)

        # 2. 模拟添加群聊与本地缓存
        models.add_chat(self.user["id"], self.chat_id, self.chat_name, local_cache=1)
        local_cache.append_messages_to_cache(
            self.chat_id,
            self.chat_name,
            ["### [2026-09-09 15:00:00] 王五:\n测试预览"]
        )

        # 3. 登录并访问 /cache/<chat_id>/view 页面
        with client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
        resp_view = client.get(f"/cache/{self.chat_id}/view")
        self.assertEqual(resp_view.status_code, 200)
        html = resp_view.data.decode("utf-8")

        # 验证引用了本地托管的 marked.min.js 且包含安全渲染脚本与兜底逻辑
        self.assertIn("/static/marked.min.js", html)
        self.assertIn("openLightbox(this.src)", html)
        self.assertIn("renderFallback", html)
        # raw_markdown 在模板中通过 tojson 转义为 Unicode 转义字符串
        self.assertTrue("测试预览" in html or json.dumps("测试预览")[1:-1] in html)

    def test_delete_cache_and_api(self):
        """测试本地缓存物理删除及 API 联动"""
        test_chat_id = "oc_del_test_001"
        test_chat_name = "待删除测试群"
        models.add_chat(self.user["id"], test_chat_id, test_chat_name, local_cache=1)

        # 1. 写入缓存数据与附件
        local_cache.append_messages_to_cache(test_chat_id, test_chat_name, ["### 消息内容"])
        local_cache.save_asset(test_chat_id, test_chat_name, b"image-data", "photo.png", "img_001")
        self.assertTrue(local_cache.has_cache(test_chat_id, test_chat_name))

        # 2. 直接调用 local_cache.delete_cache
        res = local_cache.delete_cache(test_chat_id, test_chat_name)
        self.assertTrue(res)
        self.assertFalse(local_cache.has_cache(test_chat_id, test_chat_name))

        # 3. 重新写入并测试 DELETE API 联动清理
        local_cache.append_messages_to_cache(test_chat_id, test_chat_name, ["### 再次生成"])
        self.assertTrue(local_cache.has_cache(test_chat_id, test_chat_name))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        # 调用 DELETE /api/chats/<id>?delete_cache=true
        resp = client.delete(f"/api/chats/{test_chat_id}?delete_cache=true")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        # 数据库中已删除
        self.assertIsNone(models.get_chat(self.user["id"], test_chat_id))
        # 磁盘上缓存已清理
        self.assertFalse(local_cache.has_cache(test_chat_id, test_chat_name))

    def test_max_attachment_size_config(self):
        """测试附件大小上限环境变量配置及动态异常提示"""
        from feishu import FeishuClient, SizeExceededError
        from unittest.mock import patch, MagicMock

        # 验证默认配置为 20MB
        self.assertEqual(Config.MAX_ATTACHMENT_SIZE_MB, 20)

        # 模拟下载时超限（设置上限为 10MB，资源为 15MB）
        client = FeishuClient("test_access", "test_refresh", 9999999999, 9999999999, user_id=self.user["id"])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(15 * 1024 * 1024)}

        with patch("feishu._request_with_retry", return_value=mock_resp):
            with self.assertRaises(SizeExceededError) as ctx:
                client.download_resource("msg_1", "file_1", max_size_mb=10)
            self.assertIn("10MB", str(ctx.exception))
            self.assertIn("15MB", str(ctx.exception))


    def test_backfill_cache_when_no_new_messages(self):
        """测试在飞书无新消息时，因本地无缓存自动全量补齐历史消息到 Markdown 且不重复写表格"""
        from unittest.mock import MagicMock, patch
        import app as app_module

        bf_chat_id = "oc_bf_test_01"
        bf_chat_name = "补齐测试群"
        models.add_chat(self.user["id"], bf_chat_id, bf_chat_name, local_cache=1)
        models.update_chat_sync_status(self.user["id"], bf_chat_id, 5, 5)
        models.update_chat_table_info(self.user["id"], bf_chat_id, "tbl_token", "tbl_id", "tbl_url", bf_chat_name)

        self.assertFalse(local_cache.has_cache(bf_chat_id, bf_chat_name))

        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": bf_chat_name}
        mock_client.get_chat_members_safe.return_value = {}
        # start_position=5 时无新消息；start_position=0 时返回 5 条历史
        history_msgs = [
            {
                "message_id": f"om_h_{i}",
                "message_position": str(i),
                "msg_type": "text",
                "create_time": f"177000000{i}000",
                "sender": {"id": f"ou_user_{i}"},
                "body": {"content": json.dumps({"text": f"历史消息 {i}"})},
            }
            for i in range(1, 6)
        ]

        def fake_list_all(cid, start_position=0):
            if start_position == 5:
                return []
            if start_position == 0:
                return history_msgs
            return []

        mock_client.list_all_messages.side_effect = fake_list_all

        with patch("app.FeishuClient", return_value=mock_client):
            app_module._run_sync(self.user["id"], bf_chat_id)

        # 验证 Markdown 缓存已建立
        self.assertTrue(local_cache.has_cache(bf_chat_id, bf_chat_name))
        raw_md = local_cache.get_raw_markdown(bf_chat_id, bf_chat_name)
        self.assertIn("历史消息 1", raw_md)
        self.assertIn("历史消息 5", raw_md)
        self.assertIn(bf_chat_name, raw_md)

        # 绝不写入多维表格
        mock_client.batch_create_records.assert_not_called()

        # 数据库状态更新
        chat_row = models.get_chat(self.user["id"], bf_chat_id)
        self.assertEqual(chat_row["last_cached_position"], 5)
        self.assertEqual(chat_row["latest_message_time"], 1770000005000)

        # 清理
        local_cache.delete_cache(bf_chat_id, bf_chat_name)

    def test_backfill_cache_gap_when_cache_was_disabled(self):
        """测试本地缓存曾经关闭导致断层时，同步能自动补齐断层历史消息"""
        from unittest.mock import MagicMock, patch
        import app as app_module

        gap_chat_id = "oc_gap_test_02"
        gap_chat_name = "断层补齐群"
        models.add_chat(self.user["id"], gap_chat_id, gap_chat_name, local_cache=1)
        # 飞书已同步到 10，但本地只缓存到 5
        models.update_chat_sync_status(self.user["id"], gap_chat_id, 10, 10)
        models.update_chat_last_cached_position(self.user["id"], gap_chat_id, 5)
        models.update_chat_table_info(self.user["id"], gap_chat_id, "tbl_token", "tbl_id", "tbl_url", gap_chat_name)

        # 先写入 1-5
        init_blocks = [f"<!-- msg_pos:{i} msg_id:om_{i} -->\n**用户** &nbsp; `2026-09-15 10:00:00`\n\n消息 {i}\n\n---\n" for i in range(1, 6)]
        local_cache.append_messages_to_cache(gap_chat_id, gap_chat_name, init_blocks)
        self.assertTrue(local_cache.has_cache(gap_chat_id, gap_chat_name))

        # 新拉取消息：start_position=10 返回 11-12
        new_msgs = [
            {
                "message_id": f"om_{i}",
                "message_position": str(i),
                "msg_type": "text",
                "create_time": f"177000000{i:02d}000",
                "sender": {"id": "ou_user"},
                "body": {"content": json.dumps({"text": f"消息 {i}"})},
            }
            for i in range(11, 13)
        ]
        # 断层消息：start_position=5 返回 6-12
        gap_msgs = [
            {
                "message_id": f"om_{i}",
                "message_position": str(i),
                "msg_type": "text",
                "create_time": f"177000000{i:02d}000",
                "sender": {"id": "ou_user"},
                "body": {"content": json.dumps({"text": f"消息 {i}"})},
            }
            for i in range(6, 13)
        ]

        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": gap_chat_name}
        mock_client.get_chat_members_safe.return_value = {}
        mock_client.batch_create_records.return_value = ["rec_11", "rec_12"]

        def fake_list(cid, start_position=0):
            if start_position == 10:
                return new_msgs
            if start_position == 5:
                return gap_msgs
            return []

        mock_client.list_all_messages.side_effect = fake_list

        with patch("app.FeishuClient", return_value=mock_client):
            app_module._run_sync(self.user["id"], gap_chat_id)

        # 验证表格只写入了 2 条新消息（11-12）
        mock_client.batch_create_records.assert_called_once()
        records_arg = mock_client.batch_create_records.call_args[0][2]
        self.assertEqual(len(records_arg), 2)

        # 验证 Markdown 包含 1 到 12 全部消息
        raw_md = local_cache.get_raw_markdown(gap_chat_id, gap_chat_name)
        for i in range(1, 13):
            self.assertIn(f"消息 {i}", raw_md)

        # 验证 last_cached_position 更新至 12
        chat_row = models.get_chat(self.user["id"], gap_chat_id)
        self.assertEqual(chat_row["last_cached_position"], 12)

        local_cache.delete_cache(gap_chat_id, gap_chat_name)

    def test_chats_ordering_by_latest_message_time(self):
        """测试主页群聊列表按最新消息发送时间倒序排列"""
        sort_user = models.get_or_create_user("ou_sort_isolation_user", "独立排序测试用户")
        uid = sort_user["id"]
        c1 = "oc_sort_01"
        c2 = "oc_sort_02"
        c3 = "oc_sort_03"
        models.add_chat(uid, c1, "群1")
        models.add_chat(uid, c2, "群2")
        models.add_chat(uid, c3, "群3")

        # 设置不同最新消息时间戳
        models.update_chat_latest_message_time(uid, c1, 1000000)
        models.update_chat_latest_message_time(uid, c2, 3000000)  # 最晚
        models.update_chat_latest_message_time(uid, c3, 2000000)  # 次晚

        chats = models.get_chats(uid)
        ids = [c["chat_id"] for c in chats]
        # c2 (3000000) 应该排第一，c3 (2000000) 第二，c1 (1000000) 第三
        self.assertEqual(ids[:3], [c2, c3, c1])

    def test_ensure_chat_header_and_find_existing_asset(self):
        """测试 Markdown 头部平滑更新与本地已有附件快速命中"""
        t_id = "oc_header_test"
        # 1. 初始为 chat_id
        local_cache.init_chat_cache(t_id, t_id)
        raw = local_cache.get_raw_markdown(t_id, t_id)
        self.assertIn(f"# {t_id} - 聊天记录归档", raw)

        # 2. 获取到真实名字后平滑更新
        real_name = "全新自动驾驶战略群"
        local_cache.ensure_chat_header(t_id, real_name)
        raw_updated = local_cache.get_raw_markdown(t_id, real_name)
        self.assertIn(f"# {real_name} - 聊天记录归档", raw_updated)
        self.assertIn(f"> - **群聊名称**: {real_name}", raw_updated)

        # 3. 查找已有附件（按文件名查找）
        local_cache.save_asset(t_id, real_name, b"test_content", "notice.pdf", "file_k01")
        found = local_cache.find_existing_asset(t_id, real_name, "notice.pdf", "file_k01")
        self.assertIsNotNone(found)
        self.assertEqual(found, "assets/notice.pdf")

        # 4. 根据 file_key 匹配（未提供文件名，按 key 回退命名）
        local_cache.save_asset(t_id, real_name, b"img_bytes", "", "img_k02")
        found_key = local_cache.find_existing_asset(t_id, real_name, "", "img_k02")
        self.assertIsNotNone(found_key)
        self.assertEqual(found_key, "assets/img_k02")

        local_cache.delete_cache(t_id, real_name)

    def test_rename_chat_cache_and_update_names(self):
        """测试修改群名时联动重命名本地缓存文件夹、Markdown文件及数据库记录"""
        t_id = "oc_rename_unit_test"
        old_name = "原始测试群"
        new_name = "全新架构重构群"
        feishu_name = "飞书实际群名称"

        # 1. 创建初始本地缓存
        local_cache.init_chat_cache(t_id, old_name)
        old_dir = local_cache.get_chat_cache_dir(t_id, old_name)
        self.assertTrue(os.path.exists(old_dir))
        self.assertTrue(os.path.exists(os.path.join(old_dir, f"{old_name}.md")))

        # 2. 执行 rename_chat_cache
        res = local_cache.rename_chat_cache(t_id, new_name, old_name)
        self.assertTrue(res.get("renamed"))
        new_dir = local_cache.get_chat_cache_dir(t_id, new_name)
        self.assertTrue(os.path.exists(new_dir))
        self.assertFalse(os.path.exists(old_dir))

        new_md_path = os.path.join(new_dir, f"{new_name}.md")
        self.assertTrue(os.path.exists(new_md_path))

        with open(new_md_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        self.assertIn(f"# {new_name} - 聊天记录归档", md_content)
        self.assertIn(f"> - **群聊名称**: {new_name}", md_content)

        # 3. 测试 models.update_chat_names
        user = models.get_or_create_user("test_rename_open_id", "test_user")
        uid = user["id"]
        models.add_chat(uid, t_id, chat_name=old_name)
        models.save_user_chats_cache(uid, [{"chat_id": t_id, "name": old_name}])

        models.update_chat_names(uid, t_id, chat_name=new_name, feishu_name=feishu_name)
        c = models.get_chat(uid, t_id)
        self.assertIsNotNone(c)
        self.assertEqual(c["chat_name"], new_name)
        self.assertEqual(c["feishu_name"], feishu_name)

        cached_list = models.get_user_chats_cache(uid)
        matched = [x for x in cached_list if x["chat_id"] == t_id]
        self.assertTrue(len(matched) > 0)
        self.assertEqual(matched[0]["chat_name"], feishu_name)

        # 清理
        local_cache.delete_cache(t_id, new_name)

    def test_heal_markdown_speakers(self):
        """测试历史 Markdown 归档中未解析的 open_id 批量自愈替换为真实姓名"""
        t_id = "oc_heal_test"
        name = "解散群测试"

        local_cache.init_chat_cache(t_id, name)
        msg1 = {
            "message_id": "om_01",
            "msg_type": "text",
            "create_time": "1694762844",
            "body": {"content": json.dumps({"text": "测试消息1"})},
            "sender": {"id": "ou_aaa111"}
        }
        msg2 = {
            "message_id": "om_02",
            "msg_type": "text",
            "create_time": "1694762855",
            "body": {"content": json.dumps({"text": "测试消息2"})},
            "sender": {"id": "ou_bbb222"}
        }

        b1 = local_cache.format_message_to_markdown(msg1, "ou_aaa111", "2023-09-15 15:27:24")
        b2 = local_cache.format_message_to_markdown(msg2, "ou_bbb222", "2023-09-15 15:27:35")
        local_cache.append_messages_to_cache(t_id, name, [b1, b2])

        raw_before = local_cache.get_raw_markdown(t_id, name)
        self.assertIn("**ou_aaa111** &nbsp;", raw_before)
        self.assertIn("**ou_bbb222** &nbsp;", raw_before)

        # 执行自愈
        name_map = {"ou_aaa111": "张三(后端)", "ou_bbb222": "李四(测试)"}
        healed_count = local_cache.heal_markdown_speakers(t_id, name, name_map)
        self.assertEqual(healed_count, 2)

        raw_after = local_cache.get_raw_markdown(t_id, name)
        self.assertNotIn("**ou_aaa111** &nbsp;", raw_after)
        self.assertNotIn("**ou_bbb222** &nbsp;", raw_after)
        self.assertIn("**张三(后端)** &nbsp;", raw_after)
        self.assertIn("**李四(测试)** &nbsp;", raw_after)

        local_cache.delete_cache(t_id, name)

    def test_card_footer_links_order(self):
        """测试群聊卡片底部按钮顺序：飞书表格链接必须位于下载 ZIP 链接之前且不重复"""
        # 设置 base_url 测试有飞书表格链接的情况
        test_base_url = "https://example.feishu.cn/base/bascnTest123"
        models.update_chat_table_info(self.user["id"], self.chat_id, "test_token", "test_tbl", test_base_url)

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        base_wrap_idx = html.find(f'id="base-link-wrap-{self.chat_id}"')
        zip_wrap_idx = html.find(f'id="zip-download-wrap-{self.chat_id}"')

        self.assertNotEqual(base_wrap_idx, -1, "base-link-wrap 元素应存在于模板中")
        self.assertNotEqual(zip_wrap_idx, -1, "zip-download-wrap 元素应存在于模板中")
        self.assertLess(base_wrap_idx, zip_wrap_idx, "飞书表格链接容器必须位于下载 ZIP 容器之前")

        # 检查该群聊卡片中飞书表格链接是否唯一（不能出现两个飞书表格链接）
        chat_start = html.find(f'id="chat-{self.chat_id}"')
        chat_end = html.find(f'id="progress-{self.chat_id}"')
        chat_card_html = html[chat_start:chat_end]
        self.assertEqual(chat_card_html.count("飞书表格 ↗"), 1, "群聊卡片中不应存在重复的飞书表格链接")


if __name__ == "__main__":
    unittest.main()
